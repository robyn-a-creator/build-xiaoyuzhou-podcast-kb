import csv
import datetime as dt
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "podcast_kb.py"
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("podcast_kb", SCRIPT)
kbmod = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(kbmod)


def episode(eid="rss-e1", title="EP1 测试", date="2026-08-01T08:00:00+08:00"):
    return {
        "eid": eid,
        "episode_id": eid,
        "rss_guid": f"guid-{eid}",
        "pid": "apple-1",
        "podcast_title": "测试播客",
        "title": title,
        "shownotes_html": "<p>嘉宾：李明</p><p>讨论 OpenAI 和 DeepSeek</p>",
        "duration_seconds": 3600,
        "pub_date": date,
        "audio_url": f"https://93.184.216.34/{eid}.mp3",
        "xiaoyuzhou_url": "",
        "source": "rss",
    }


class FakeASR:
    submit_calls = []

    def __init__(self, cfg):
        self.model = cfg["asr_model"]

    def submit(self, audio_url, hotwords, diarization):
        self.submit_calls.append((audio_url, tuple(hotwords), diarization))
        return "task-1"

    def poll(self, task_id):
        return {"transcripts": [{"sentences": [
            {"begin_time": 1000, "end_time": 2000, "speaker_id": 0, "text": "你好"},
            {"begin_time": 2100, "end_time": 3000, "speaker_id": 1, "text": "世界"},
        ]}]}


class PodcastKBTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "asr_model": "qwen-audio-3.0-asr-flash-filetrans",
            "dashscope_api_key": "fake",
            "dashscope_base_url": "https://example.aliyuncs.com/api/v1",
            "asr_poll_seconds": 0,
            "asr_max_wait_seconds": 1,
            "asr_max_retries": 0,
            "hotword_limit": 200,
            "hotword_weight": 5,
        }
        FakeASR.submit_calls = []

    def make_kb(self, root):
        podcast = {
            "pid": "apple-1", "title": "测试播客", "author": "主播李明",
            "feed_url": "https://example.com/feed.xml", "history_source": "rss",
            "history_complete": True,
        }
        return kbmod.KnowledgeBase(Path(root), podcast, kbmod.parse_time_range("全部节目", dt.date(2026, 8, 26)), self.cfg)

    def test_time_ranges_are_calendar_based_and_inclusive(self):
        spec = kbmod.parse_time_range("过去四年", dt.date(2026, 8, 26))
        self.assertEqual(spec["since"], "2022-08-26")
        self.assertEqual(spec["until"], "2026-08-26")
        self.assertEqual(kbmod.parse_time_range("2024年", dt.date(2026, 8, 26))["until"], "2024-12-31")
        self.assertEqual(kbmod.parse_time_range("最近50期")["limit"], 50)
        self.assertEqual(kbmod.parse_time_range("过去一年", dt.date(2024, 2, 29))["since"], "2023-02-28")
        for bad in ("最近0期", "2026-08-02..2026-08-01", "2026-13-01..2026-14-01"):
            with self.assertRaises(kbmod.PipelineError):
                kbmod.parse_time_range(bad)

    def test_public_audio_always_uses_asr_with_speakers_and_hotwords(self):
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = self.make_kb(tmp)
            knowledge.remember_discovered([episode()])
            with patch.object(kbmod, "DashScopeASR", FakeASR):
                self.assertTrue(knowledge.process(episode()))
            state = knowledge.checkpoint["episodes"]["rss-e1"]
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["source"], "rss")
            self.assertIn("OpenAI", FakeASR.submit_calls[0][1])
            rendered = (Path(tmp) / state["transcript_path"]).read_text(encoding="utf-8")
            self.assertIn("Speaker 1", rendered)
            self.assertIn("Speaker 2", rendered)
            self.assertFalse(any((Path(tmp) / "raw/audio").iterdir()))

    def test_one_failure_does_not_block_next_episode(self):
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = self.make_kb(tmp)
            items = [episode("rss-bad", "坏节目"), episode("rss-good", "好节目")]
            knowledge.remember_discovered(items)

            class SelectiveASR(FakeASR):
                def submit(self, audio_url, hotwords, diarization):
                    if "rss-bad" in audio_url:
                        raise kbmod.PipelineError("asr_failed", "provider failed")
                    return "task-good"

            with patch.object(kbmod, "DashScopeASR", SelectiveASR):
                self.assertFalse(knowledge.process(items[0]))
                self.assertTrue(knowledge.process(items[1]))
            self.assertEqual(knowledge.stats()["failed"], 1)
            self.assertEqual(knowledge.stats()["completed"], 1)

    def test_completed_sidecar_repairs_checkpoint_and_resume_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = self.make_kb(tmp)
            knowledge.remember_discovered([episode()])
            with patch.object(kbmod, "DashScopeASR", FakeASR):
                self.assertTrue(knowledge.process(episode()))
                calls = len(FakeASR.submit_calls)
                reopened = self.make_kb(tmp)
                self.assertFalse(reopened.process(episode()))
            self.assertEqual(len(FakeASR.submit_calls), calls)

    def test_missing_markdown_is_rebuilt_from_raw_without_new_asr(self):
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = self.make_kb(tmp)
            knowledge.remember_discovered([episode()])
            with patch.object(kbmod, "DashScopeASR", FakeASR):
                self.assertTrue(knowledge.process(episode()))
            (Path(tmp) / knowledge.checkpoint["episodes"]["rss-e1"]["transcript_path"]).unlink()
            reopened = self.make_kb(tmp)
            self.assertEqual(reopened.checkpoint["episodes"]["rss-e1"]["status"], "completed")
            with patch.object(kbmod, "DashScopeASR", side_effect=AssertionError("must not resubmit")):
                self.assertFalse(reopened.process(episode()))

    def test_raw_repair_cannot_write_outside_knowledge_base(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            knowledge = self.make_kb(tmp)
            raw = Path(tmp) / "raw/transcripts/rss-e1.asr.json"
            raw.write_text(json.dumps(FakeASR(self.cfg).poll("task")), encoding="utf-8")
            escaped = Path(outside) / "escaped.md"
            record = {
                "raw_transcript_path": "raw/transcripts/rss-e1.asr.json",
                "transcript_path": str(escaped),
                "episode": episode(),
            }
            self.assertFalse(knowledge.repair_transcript_from_raw(record))
            self.assertFalse(escaped.exists())

    def test_missing_raw_and_audio_is_not_misclassified_as_audio_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = self.make_kb(tmp)
            knowledge.remember_discovered([episode()])
            with patch.object(kbmod, "DashScopeASR", FakeASR):
                self.assertTrue(knowledge.process(episode(), keep_audio=False))
            state = knowledge.checkpoint["episodes"]["rss-e1"]
            state["audio_path"] = "raw/audio/rss-e1.mp3"
            (Path(tmp) / state["raw_transcript_path"]).unlink()
            knowledge.reconcile()
            self.assertEqual(knowledge.checkpoint["episodes"]["rss-e1"]["error"]["kind"], "artifact_missing")

    def test_uncertain_submit_is_not_repeated_without_explicit_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = self.make_kb(tmp)
            knowledge.remember_discovered([episode()])

            class Uncertain(FakeASR):
                calls = 0
                def submit(self, *args):
                    self.__class__.calls += 1
                    raise kbmod.PipelineError("asr_submit_uncertain", "lost response")

            with patch.object(kbmod, "DashScopeASR", Uncertain):
                self.assertFalse(knowledge.process(episode()))
                self.assertFalse(knowledge.process(episode(), force=True))
            self.assertEqual(Uncertain.calls, 1)

    def test_index_and_metadata_include_required_public_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = self.make_kb(tmp)
            knowledge.checkpoint["coverage"] = {
                "history_source": "rss", "history_complete": True,
                "history_reason": "count checked", "discovered_total": 1, "in_range_total": 1,
            }
            knowledge.remember_discovered([episode()])
            with patch.object(kbmod, "DashScopeASR", FakeASR):
                knowledge.process(episode())
            with (Path(tmp) / "index.csv").open(encoding="utf-8") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(list(rows[0]), kbmod.INDEX_FIELDS)
            self.assertEqual(rows[0]["rss_guid"], "guid-rss-e1")
            metadata = json.loads((Path(tmp) / "metadata.json").read_text(encoding="utf-8"))
            self.assertTrue(metadata["coverage"]["history_complete"])
            self.assertIn("history_complete", (Path(tmp) / "README.md").read_text(encoding="utf-8"))

    def test_four_year_filter_is_by_date_not_episode_count(self):
        range_spec = kbmod.parse_time_range("过去四年", dt.date(2026, 8, 26))
        items = [
            episode("rss-old", date="2022-08-25T08:00:00+08:00"),
            episode("rss-start", date="2022-08-26T00:00:00+08:00"),
            episode("rss-end", date="2026-08-26T23:59:00+08:00"),
            episode("rss-future", date="2026-08-27T00:00:00+08:00"),
        ]
        self.assertEqual([ep["eid"] for ep in kbmod.filter_episodes(items, range_spec)], ["rss-start", "rss-end"])

    def test_hotwords_follow_provider_length_limits(self):
        ep = episode()
        ep["shownotes_html"] += "<p>品牌：这是一个超过十五个汉字所以不能提交为热词的超长名称</p>"
        words = kbmod.extract_hotwords(ep, {"title": "测试播客", "author": "李明"})
        self.assertIn("OpenAI", words)
        self.assertTrue(all(len(word) <= 15 for word in words if any(ord(ch) > 127 for ch in word)))
        self.assertTrue(all(len(word.split()) <= 7 for word in words if word.isascii()))

    def test_update_deduplicates_by_stable_episode_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = self.make_kb(tmp)
            initial = [episode("rss-e1")]
            knowledge.checkpoint["coverage"] = {"discovered_total": 1, "in_range_total": 1}
            knowledge.remember_discovered(initial)
            with patch.object(kbmod, "DashScopeASR", FakeASR):
                knowledge.process(initial[0])
            args = type("Args", (), {"kb_dir": tmp, "keep_audio": False, "allow_uncertain_resubmit": False})()
            refreshed = [episode("rss-e2", date="2026-08-02T08:00:00+08:00"), *initial]
            coverage = {"history_source": "rss", "history_complete": True, "history_reason": "checked", "rss_episode_count": 2}
            with patch.object(kbmod, "refresh_rss_podcast", return_value=(refreshed, coverage)), patch.object(kbmod, "DashScopeASR", FakeASR):
                self.assertEqual(kbmod.cmd_update(args, self.cfg), 0)
            checkpoint = json.loads((Path(tmp) / "state/checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["stats"]["completed"], 2)

    def test_source_snapshot_media_alias_survives_new_guid(self):
        old = episode("rss-old-id")
        old["media_fingerprint"] = "/audio/e.mp3?id=42"
        snapshot = set(kbmod.source_snapshot_tokens([old]))
        corrected = episode("rss-new-guid-id")
        corrected["media_fingerprint"] = old["media_fingerprint"]
        self.assertTrue(kbmod.source_was_seen(corrected, snapshot))

    def test_public_supplement_becoming_rss_is_not_seen_as_new_paid_work(self):
        public = episode("xiaoyuzhou-e3")
        public["source"] = "xiaoyuzhou_public"
        public["media_fingerprint"] = "/audio/e3.mp3"
        snapshot = set(kbmod.source_snapshot_tokens([public]))
        later_rss = {**public, "eid": "rss-publisher-guid", "episode_id": "rss-publisher-guid", "source": "rss", "rss_guid": "publisher-guid"}
        self.assertTrue(kbmod.source_was_seen(later_rss, snapshot))
        self.assertFalse(kbmod.source_identity_ambiguous(later_rss, snapshot, set()))

    def test_ambiguous_reused_fingerprint_never_uses_media_alias(self):
        old = episode("rss-old")
        old["media_fingerprint"] = "/download?id=A"
        snapshot = set(kbmod.source_snapshot_tokens([old]))
        same_title_next_day = episode("rss-new", date="2026-08-27T08:00:00+08:00")
        same_title_next_day["media_fingerprint"] = old["media_fingerprint"]
        digest = hashlib.sha256(old["media_fingerprint"].encode()).hexdigest()
        self.assertFalse(kbmod.source_was_seen(same_title_next_day, snapshot, {digest}))
        self.assertTrue(kbmod.source_identity_ambiguous(same_title_next_day, snapshot, {digest}))

    def test_guid_plus_title_and_date_correction_stops_before_asr(self):
        old = episode("rss-old", title="Episode 10", date="2026-08-01T08:00:00+08:00")
        old["media_fingerprint"] = "/audio/e.mp3"
        snapshot = set(kbmod.source_snapshot_tokens([old]))
        corrected = episode("rss-new-guid", title="Episode 10 corrected", date="2026-08-02T08:00:00+08:00")
        corrected["media_fingerprint"] = old["media_fingerprint"]
        self.assertFalse(kbmod.source_was_seen(corrected, snapshot))
        self.assertTrue(kbmod.source_identity_ambiguous(corrected, snapshot, set()))

    def test_date_range_update_does_not_resubmit_when_guid_is_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = self.make_kb(tmp)
            old = episode("rss-no-guid")
            old["rss_guid"] = ""
            old["media_fingerprint"] = "/audio/e.mp3?id=42"
            knowledge.checkpoint["source_snapshot_ids"] = kbmod.source_snapshot_tokens([old])
            knowledge.remember_discovered([old])
            with patch.object(kbmod, "DashScopeASR", FakeASR):
                knowledge.process(old)
            corrected = {**old, "eid": "rss-new-guid", "episode_id": "rss-new-guid", "rss_guid": "publisher-guid"}
            args = type("Args", (), {"kb_dir": tmp, "keep_audio": False, "allow_uncertain_resubmit": False, "max_episodes": 5})()
            coverage = {"history_source": "rss", "history_complete": True, "history_reason": "checked", "rss_episode_count": 1}
            FakeASR.submit_calls = []
            with patch.object(kbmod, "refresh_rss_podcast", return_value=([corrected], coverage)), patch.object(kbmod, "DashScopeASR", FakeASR):
                self.assertEqual(kbmod.cmd_update(args, self.cfg), 0)
            self.assertEqual(FakeASR.submit_calls, [])

    def test_symlink_output_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real"
            real.mkdir()
            link = Path(tmp) / "link"
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaises(kbmod.PipelineError):
                self.make_kb(link)

    def test_paid_batch_requires_explicit_limit_confirmation(self):
        args = type("Args", (), {"max_episodes": None})()
        with self.assertRaises(kbmod.PipelineError) as caught:
            kbmod.enforce_batch_limit([episode(f"rss-e{i}") for i in range(6)], args, {"max_episodes_per_run": 5})
        self.assertEqual(caught.exception.kind, "batch_confirmation_required")

    def test_update_extends_original_until_to_today(self):
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = self.make_kb(tmp)
            knowledge.range = {"label": "过去四年", "since": "2022-08-01", "until": "2026-08-01", "limit": None}
            knowledge.checkpoint["range"] = knowledge.range
            old = episode("rss-old", date="2026-08-01T08:00:00+08:00")
            knowledge.remember_discovered([old])
            with patch.object(kbmod, "DashScopeASR", FakeASR):
                knowledge.process(old)
            new = episode("rss-new", date="2026-08-20T08:00:00+08:00")
            args = type("Args", (), {"kb_dir": tmp, "keep_audio": False, "allow_uncertain_resubmit": False, "max_episodes": 5})()
            coverage = {"history_source": "rss", "history_complete": True, "history_reason": "checked", "rss_episode_count": 2}
            with patch.object(kbmod, "refresh_rss_podcast", return_value=([new, old], coverage)), patch.object(kbmod, "DashScopeASR", FakeASR):
                self.assertEqual(kbmod.cmd_update(args, self.cfg), 0)
            saved = json.loads((Path(tmp) / "state/checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["episodes"]["rss-new"]["status"], "completed")

    def test_latest_n_update_scans_all_newer_episodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = self.make_kb(tmp)
            knowledge.range = {"label": "最近2期", "since": None, "until": None, "limit": 2}
            knowledge.checkpoint["range"] = knowledge.range
            first = episode("rss-e1", date="2026-08-01T08:00:00+08:00")
            knowledge.remember_discovered([first])
            with patch.object(kbmod, "DashScopeASR", FakeASR):
                knowledge.process(first)
            refreshed = [episode(f"rss-e{i}", date=f"2026-08-0{i}T08:00:00+08:00") for i in range(6, 1, -1)] + [first]
            args = type("Args", (), {"kb_dir": tmp, "keep_audio": False, "allow_uncertain_resubmit": False, "max_episodes": 5})()
            coverage = {"history_source": "rss", "history_complete": True, "history_reason": "checked", "rss_episode_count": 6}
            with patch.object(kbmod, "refresh_rss_podcast", return_value=(refreshed, coverage)), patch.object(kbmod, "DashScopeASR", FakeASR):
                self.assertEqual(kbmod.cmd_update(args, self.cfg), 0)
            saved = json.loads((Path(tmp) / "state/checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(set(saved["episodes"]), {f"rss-e{i}" for i in range(1, 7)})

    def test_latest_n_update_includes_later_episode_on_same_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = self.make_kb(tmp)
            knowledge.range = {"label": "最近1期", "since": None, "until": None, "limit": 1}
            knowledge.checkpoint["range"] = knowledge.range
            morning = episode("rss-morning", date="2026-08-26T08:00:00+08:00")
            evening = episode("rss-evening", date="2026-08-26T20:00:00+08:00")
            knowledge.remember_discovered([morning])
            with patch.object(kbmod, "DashScopeASR", FakeASR):
                knowledge.process(morning)
            args = type("Args", (), {"kb_dir": tmp, "keep_audio": False, "allow_uncertain_resubmit": False, "max_episodes": 5})()
            coverage = {"history_source": "rss", "history_complete": True, "history_reason": "checked", "rss_episode_count": 2}
            with patch.object(kbmod, "refresh_rss_podcast", return_value=([evening, morning], coverage)), patch.object(kbmod, "DashScopeASR", FakeASR):
                self.assertEqual(kbmod.cmd_update(args, self.cfg), 0)
            saved = json.loads((Path(tmp) / "state/checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["episodes"]["rss-evening"]["status"], "completed")

    def test_latest_n_snapshot_detects_equal_time_and_backfilled_new_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = self.make_kb(tmp)
            knowledge.range = {"label": "最近1期", "since": None, "until": None, "limit": 1}
            knowledge.checkpoint["range"] = knowledge.range
            current = episode("rss-current", date="2026-08-26T08:00:00+08:00")
            old_at_build = episode("rss-old-at-build", date="2025-01-01T08:00:00+08:00")
            equal_time = episode("rss-equal-new", date="2026-08-26T08:00:00+08:00")
            backfilled = episode("rss-backfilled-new", date="2024-01-01T08:00:00+08:00")
            knowledge.checkpoint["source_snapshot_ids"] = [current["eid"], old_at_build["eid"]]
            knowledge.remember_discovered([current])
            with patch.object(kbmod, "DashScopeASR", FakeASR):
                knowledge.process(current)
            args = type("Args", (), {"kb_dir": tmp, "keep_audio": False, "allow_uncertain_resubmit": False, "max_episodes": 5})()
            coverage = {"history_source": "rss", "history_complete": True, "history_reason": "checked", "rss_episode_count": 4}
            scanned = [equal_time, current, old_at_build, backfilled]
            with patch.object(kbmod, "refresh_rss_podcast", return_value=(scanned, coverage)), patch.object(kbmod, "DashScopeASR", FakeASR):
                self.assertEqual(kbmod.cmd_update(args, self.cfg), 0)
            saved = json.loads((Path(tmp) / "state/checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(set(saved["episodes"]), {current["eid"], equal_time["eid"], backfilled["eid"]})

    def test_latest_n_snapshot_does_not_forget_ids_during_truncated_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = self.make_kb(tmp)
            knowledge.range = {"label": "最近1期", "since": None, "until": None, "limit": 1}
            knowledge.checkpoint["range"] = knowledge.range
            current = episode("rss-current", date="2026-08-26T08:00:00+08:00")
            old = episode("rss-old", date="2025-01-01T08:00:00+08:00")
            knowledge.checkpoint["source_snapshot_ids"] = [current["eid"], old["eid"]]
            knowledge.remember_discovered([current])
            with patch.object(kbmod, "DashScopeASR", FakeASR):
                knowledge.process(current)
            args = type("Args", (), {"kb_dir": tmp, "keep_audio": False, "allow_uncertain_resubmit": False, "max_episodes": 5})()
            incomplete = {"history_source": "rss", "history_complete": False, "history_reason": "temporarily truncated", "rss_episode_count": 1}
            with patch.object(kbmod, "refresh_rss_podcast", return_value=([current], incomplete)), patch.object(kbmod, "DashScopeASR", FakeASR):
                self.assertEqual(kbmod.cmd_update(args, self.cfg), 0)
            healed = {"history_source": "rss", "history_complete": True, "history_reason": "healed", "rss_episode_count": 2}
            FakeASR.submit_calls = []
            with patch.object(kbmod, "refresh_rss_podcast", return_value=([current, old], healed)), patch.object(kbmod, "DashScopeASR", FakeASR):
                self.assertEqual(kbmod.cmd_update(args, self.cfg), 0)
            saved = json.loads((Path(tmp) / "state/checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(set(saved["episodes"]), {current["eid"]})
            self.assertEqual(FakeASR.submit_calls, [])


if __name__ == "__main__":
    unittest.main()
