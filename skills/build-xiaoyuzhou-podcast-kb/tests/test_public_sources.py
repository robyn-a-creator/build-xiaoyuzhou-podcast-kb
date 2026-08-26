import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import public_sources as src


RSS = '''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>测试播客</title><itunes:author>李明</itunes:author><description>人工智能访谈</description>
<item><title>EP2 DeepSeek</title><guid>g2</guid><pubDate>Wed, 26 Aug 2026 00:00:00 GMT</pubDate><itunes:duration>01:02:03</itunes:duration><content:encoded>嘉宾：王红</content:encoded><enclosure url="https://example.com/e2.mp3" length="2" /></item>
<item><title>EP1 OpenAI</title><guid>g1</guid><pubDate>Fri, 26 Aug 2022 00:00:00 GMT</pubDate><itunes:duration>3600</itunes:duration><enclosure url="https://example.com/e1.mp3" length="1" /></item>
</channel></rss>'''.encode("utf-8")


class PublicSourceTests(unittest.TestCase):
    def test_parse_rss_normalizes_and_sorts(self):
        feed = src.parse_feed(RSS, "https://example.com/feed.xml")
        self.assertEqual(feed.title, "测试播客")
        self.assertEqual(len(feed.episodes), 2)
        self.assertEqual(feed.episodes[0]["rss_guid"], "g2")
        self.assertEqual(feed.episodes[0]["duration_seconds"], 3723)
        self.assertTrue(feed.episodes[0]["eid"].startswith("rss-"))

    def test_stable_id_uses_guid(self):
        one = src.stable_episode_id("g", "https://a/1", "t", "2026")
        two = src.stable_episode_id("g", "https://b/2", "changed", "2025")
        self.assertEqual(one, two)

    def test_matching_uses_all_available_dimensions(self):
        feed = src.parse_feed(RSS, "https://example.com/feed.xml")
        meta = {"title": "测试播客", "author": "李明", "description": "人工智能访谈", "recent_titles": ["EP2 DeepSeek"]}
        candidate = {"collectionName": "测试播客", "artistName": "李明"}
        score = src._candidate_score(meta, candidate, feed)
        self.assertGreater(score["score"], 0.95)
        self.assertEqual(score["parts"]["episode_overlap"], 1.0)

    def test_completeness_true_only_after_count_and_overlap_check(self):
        feed = src.parse_feed(RSS, "https://example.com/feed.xml")
        xyz = {"pid": "p1", "title": "测试播客", "author": "李明", "description": "人工智能访谈", "image_url": "", "episode_count": 2, "episode_count_verified": True, "recent_titles": ["EP2 DeepSeek"], "xiaoyuzhou_url": "https://www.xiaoyuzhoufm.com/podcast/p1"}
        candidate = {"collectionName": "测试播客", "artistName": "李明", "collectionId": 1, "feedUrl": feed.url, "trackCount": 2}
        with patch.object(src, "fetch_xiaoyuzhou_podcast", return_value=xyz), patch.object(src, "itunes_search", return_value=[candidate]), patch.object(src, "fetch_feed", return_value=feed):
            podcast, episodes, coverage = src.resolve_public_podcast(xyz["xiaoyuzhou_url"])
        self.assertTrue(coverage["history_complete"])
        self.assertEqual(podcast["history_source"], "rss")
        self.assertEqual(len(episodes), 2)

        xyz["episode_count"] = 3
        with patch.object(src, "fetch_xiaoyuzhou_podcast", return_value=xyz), patch.object(src, "itunes_search", return_value=[candidate]), patch.object(src, "fetch_feed", return_value=feed):
            _, _, incomplete = src.resolve_public_podcast(xyz["xiaoyuzhou_url"])
        self.assertFalse(incomplete["history_complete"])
        self.assertIn("少于", incomplete["history_reason"])

    def test_close_candidates_are_rejected(self):
        feed = src.parse_feed(RSS, "https://example.com/feed.xml")
        xyz = {"pid": "p1", "title": "测试播客", "author": "李明", "description": "人工智能访谈", "image_url": "", "episode_count": 2, "episode_count_verified": True, "recent_titles": ["EP2 DeepSeek"], "xiaoyuzhou_url": "https://www.xiaoyuzhoufm.com/podcast/p1"}
        candidates = [
            {"collectionName": "测试播客", "artistName": "李明", "collectionId": 1, "feedUrl": "https://example.com/1.xml"},
            {"collectionName": "测试播客", "artistName": "李明", "collectionId": 2, "feedUrl": "https://example.com/2.xml"},
        ]
        with patch.object(src, "fetch_xiaoyuzhou_podcast", return_value=xyz), patch.object(src, "itunes_search", return_value=candidates), patch.object(src, "fetch_feed", return_value=feed):
            with self.assertRaises(src.SourceError) as caught:
                src.resolve_public_podcast(xyz["xiaoyuzhou_url"])
        self.assertEqual(caught.exception.kind, "podcast_ambiguous")

    def test_public_page_fallback_is_explicitly_incomplete(self):
        xyz = {
            "pid": "p1", "title": "未上架节目", "author": "主播", "description": "", "image_url": "",
            "episode_count": 20, "recent_titles": ["最近一期"], "xiaoyuzhou_url": "https://www.xiaoyuzhoufm.com/podcast/p1",
            "recent_episodes_raw": [{
                "eid": "e1", "title": "最近一期", "pubDate": "2026-08-20T00:00:00Z", "duration": 100,
                "media": {"source": {"url": "https://example.com/e1.m4a"}},
            }],
        }
        with patch.object(src, "fetch_xiaoyuzhou_podcast", return_value=xyz), patch.object(src, "itunes_search", return_value=[]):
            podcast, episodes, coverage = src.resolve_public_podcast(xyz["xiaoyuzhou_url"])
        self.assertEqual(podcast["history_source"], "xiaoyuzhou_public")
        self.assertFalse(coverage["history_complete"])
        self.assertEqual(len(episodes), 1)
        self.assertIn("最近 1 期", coverage["history_reason"])

    def test_no_guid_ignores_rotating_audio_query(self):
        one = src.stable_episode_id("", "https://cdn.example/e.mp3?token=one", "同一期", "2026-08-26T08:00:00+08:00")
        two = src.stable_episode_id("", "https://cdn.example/e.mp3?token=two", "同一期", "2026-08-26T08:00:00+08:00")
        self.assertEqual(one, two)

    def test_no_guid_ignores_title_correction_when_audio_is_same(self):
        one = src.stable_episode_id("", "https://cdn.example/e.mp3?token=one", "原标题", "2026-08-26T08:00:00+08:00")
        two = src.stable_episode_id("", "https://cdn.example/e.mp3?token=two", "更正后的标题", "2026-08-26T08:00:00+08:00")
        self.assertNotEqual(one, two)
        self.assertEqual(src.media_fingerprint("https://cdn.example/e.mp3?token=one"), src.media_fingerprint("https://cdn.example/e.mp3?token=two"))

    def test_no_guid_survives_cdn_and_publication_date_correction(self):
        one = src.stable_episode_id("", "https://old-cdn.example/audio/e.mp3?token=one", "原标题", "2026-08-25T08:00:00+08:00")
        two = src.stable_episode_id("", "https://new-cdn.example/audio/e.mp3?token=two", "更正标题", "2026-08-26T08:00:00+08:00")
        self.assertNotEqual(one, two)
        self.assertEqual(src.media_fingerprint("https://old-cdn.example/audio/e.mp3?token=one"), src.media_fingerprint("https://new-cdn.example/audio/e.mp3?token=two"))

    def test_no_guid_keeps_identity_query_but_drops_signed_query(self):
        first = src.stable_episode_id("", "https://cdn.example/download?id=A&token=old", "A", "2026-08-25T08:00:00+08:00")
        rotated = src.stable_episode_id("", "https://new.example/download?token=new&id=A", "A", "2026-08-25T08:00:00+08:00")
        other = src.stable_episode_id("", "https://cdn.example/download?id=B&token=old", "B", "2026-08-25T08:00:00+08:00")
        self.assertEqual(first, rotated)
        self.assertNotEqual(first, other)

    def test_shared_download_endpoint_does_not_silently_drop_episode(self):
        extra = b'''<item><title>EP3</title><pubDate>Thu, 27 Aug 2026 00:00:00 GMT</pubDate><enclosure url="https://example.com/download?id=B" /></item>'''
        changed = RSS.replace(b"</channel>", extra + b"</channel>").replace(b"https://example.com/e2.mp3", b"https://example.com/download?id=A")
        feed = src.parse_feed(changed, "https://example.com/feed.xml")
        self.assertEqual(len(feed.episodes), 3)
        self.assertEqual(len({ep["eid"] for ep in feed.episodes}), 3)

    def test_cloud_signature_rotation_is_not_identity(self):
        one = src.stable_episode_id("", "https://cdn.example/a.mp3?X-Goog-Date=1&X-Goog-Signature=old", "A", "2026-08-26T08:00:00+08:00")
        two = src.stable_episode_id("", "https://cdn.example/a.mp3?X-Goog-Date=2&X-Goog-Signature=new", "A", "2026-08-26T08:00:00+08:00")
        self.assertEqual(one, two)

    def test_reused_identical_media_url_gets_order_independent_ids(self):
        template = '''<?xml version="1.0"?><rss version="2.0"><channel><title>X</title>{items}</channel></rss>'''
        a = '<item><title>Old</title><pubDate>Wed, 26 Aug 2026 00:00:00 GMT</pubDate><enclosure url="https://example.com/download?id=A" /></item>'
        b = '<item><title>New</title><pubDate>Thu, 27 Aug 2026 00:00:00 GMT</pubDate><enclosure url="https://example.com/download?id=A" /></item>'
        first = src.parse_feed(template.format(items=a + b).encode(), "https://example.com/feed.xml")
        second = src.parse_feed(template.format(items=b + a).encode(), "https://example.com/feed.xml")
        self.assertEqual({ep["eid"] for ep in first.episodes}, {ep["eid"] for ep in second.episodes})
        self.assertEqual(len(first.episodes), 2)

    def test_clear_text_public_urls_are_rejected(self):
        with self.assertRaises(src.SourceError):
            src.validate_public_url("http://example.com/feed.xml")

    def test_apple_http_feed_is_only_fetched_via_exact_https_upgrade(self):
        feed = src.parse_feed(RSS, "https://example.com/feed.xml?show=1")
        xyz = {"pid": "p1", "title": "测试播客", "author": "李明", "description": "人工智能访谈", "image_url": "", "episode_count": 2, "episode_count_verified": True, "recent_titles": ["EP2 DeepSeek"], "xiaoyuzhou_url": "https://www.xiaoyuzhoufm.com/podcast/p1"}
        candidate = {"collectionName": "测试播客", "artistName": "李明", "collectionId": 1, "feedUrl": "http://example.com/feed.xml?show=1", "trackCount": 2}
        with patch.object(src, "fetch_xiaoyuzhou_podcast", return_value=xyz), patch.object(src, "itunes_search", return_value=[candidate]), patch.object(src, "fetch_feed", return_value=feed) as fetch:
            podcast, _, coverage = src.resolve_public_podcast(xyz["xiaoyuzhou_url"])
        fetch.assert_called_once_with("https://example.com/feed.xml?show=1")
        self.assertEqual(podcast["feed_url"], "https://example.com/feed.xml?show=1")
        self.assertTrue(coverage["history_complete"])

    def test_exact_public_gap_is_merged_without_duplicate(self):
        feed = src.parse_feed(RSS, "https://example.com/feed.xml")
        xyz = {
            "pid": "p1", "title": "测试播客", "author": "李明", "description": "人工智能访谈", "image_url": "",
            "episode_count": 3, "episode_count_verified": True, "recent_titles": ["EP3 新节目", "EP2 DeepSeek"],
            "xiaoyuzhou_url": "https://www.xiaoyuzhoufm.com/podcast/p1",
            "recent_episodes_raw": [
                {"eid": "x3", "title": "EP3 新节目", "pubDate": "2026-08-27T00:00:00Z", "duration": 90, "media": {"source": {"url": "https://example.com/e3.mp3"}}},
                {"eid": "x2", "title": "EP2 DeepSeek", "pubDate": "2026-08-26T00:00:00Z", "duration": 3723, "media": {"source": {"url": "https://example.com/e2.mp3?token=new"}}},
            ],
        }
        candidate = {"collectionName": "测试播客", "artistName": "李明", "collectionId": 1, "feedUrl": feed.url, "trackCount": 2}
        with patch.object(src, "fetch_xiaoyuzhou_podcast", return_value=xyz), patch.object(src, "itunes_search", return_value=[candidate]), patch.object(src, "fetch_feed", return_value=feed):
            podcast, episodes, coverage = src.resolve_public_podcast(xyz["xiaoyuzhou_url"])
        self.assertEqual(len(episodes), 3)
        self.assertEqual(sum(ep["title"] == "EP2 DeepSeek" for ep in episodes), 1)
        self.assertEqual(coverage["public_supplement_count"], 1)
        self.assertEqual(coverage["merged_episode_count"], 3)
        self.assertEqual(podcast["history_source"], "rss+xiaoyuzhou_public")
        self.assertTrue(coverage["history_complete"])

    def test_ambiguous_public_gap_is_not_merged(self):
        feed = src.parse_feed(RSS, "https://example.com/feed.xml")
        xyz = {
            "episode_count": 3, "episode_count_verified": True,
            "recent_episodes_raw": [
                {"eid": "x3", "title": "候选 A", "pubDate": "2026-08-27T00:00:00Z", "media": {"source": {"url": "https://example.com/a.mp3"}}},
                {"eid": "x4", "title": "候选 B", "pubDate": "2026-08-28T00:00:00Z", "media": {"source": {"url": "https://example.com/b.mp3"}}},
            ],
        }
        episodes, added, verified = src.merge_verified_public_gap(feed.episodes, xyz)
        self.assertEqual(added, 0)
        self.assertFalse(verified)
        self.assertEqual(len(episodes), 2)

    def test_many_public_candidates_cannot_match_one_rss_item_and_fake_completeness(self):
        rss = [
            {"eid": "rss-d1", "title": "科技日报第1期：人工智能产业观察", "pub_date": "2026-08-20T08:00:00+08:00", "audio_url": "https://rss.example.com/d1.mp3", "source": "rss"},
            {"eid": "rss-b", "title": "对谈 Alice：创业经验", "pub_date": "2026-08-21T08:00:00+08:00", "audio_url": "https://rss.example.com/b.mp3", "source": "rss"},
        ]
        xyz = {
            "episode_count": 3, "episode_count_verified": True,
            "recent_episodes_raw": [
                {"eid": "x-d1", "title": "科技日报第1期：人工智能产业观察", "pubDate": "2026-08-20T08:00:00+08:00", "media": {"source": {"url": "https://public.example.com/d1.mp3"}}},
                {"eid": "x-d3", "title": "科技日报第3期：人工智能产业观察", "pubDate": "2026-08-22T08:00:00+08:00", "media": {"source": {"url": "https://public.example.com/d3.mp3"}}},
                {"eid": "x-b", "title": "Alice 创业复盘（修订版）", "pubDate": "2026-08-21T08:00:00+08:00", "media": {"source": {"url": "https://public.example.com/b-new.mp3"}}},
            ],
        }
        episodes, added, verified = src.merge_verified_public_gap(rss, xyz)
        self.assertEqual(added, 0)
        self.assertFalse(verified)
        self.assertEqual({ep["eid"] for ep in episodes}, {"rss-d1", "rss-b"})

    def test_similar_numbered_missing_episode_is_not_hidden_by_fuzzy_title_match(self):
        rss = [
            {"eid": "rss-d1", "title": "科技日报第1期：人工智能产业观察与创业公司融资分析", "pub_date": "2026-08-20T08:00:00+08:00", "audio_url": "https://rss.example.com/d1.mp3", "source": "rss"},
            {"eid": "rss-b", "title": "对谈 Alice：创业经验", "pub_date": "2026-08-21T08:00:00+08:00", "audio_url": "https://rss.example.com/b.mp3", "source": "rss"},
        ]
        xyz = {
            "episode_count": 3, "episode_count_verified": True,
            "recent_episodes_raw": [
                {"eid": "x-d3", "title": "科技日报第3期：人工智能产业观察与创业公司融资分析", "pubDate": "2026-08-22T08:00:00+08:00", "media": {"source": {"url": "https://public.example.com/d3.mp3"}}},
                {"eid": "x-b", "title": "Alice 创业复盘（修订版）", "pubDate": "2026-08-21T08:00:00+08:00", "media": {"source": {"url": "https://public.example.com/b-new.mp3"}}},
            ],
        }
        self.assertEqual(src._episode_match_strength(src.normalize_public_recent(xyz)[0], rss[0]), 0)
        episodes, added, verified = src.merge_verified_public_gap(rss, xyz)
        self.assertEqual(added, 0)
        self.assertFalse(verified)
        self.assertEqual({ep["eid"] for ep in episodes}, {"rss-d1", "rss-b"})

    def test_shared_media_path_cannot_hide_a_real_missing_episode(self):
        rss = [
            {"eid": "rss-0", "title": "正常节目", "pub_date": "2026-08-19T08:00:00+08:00", "audio_url": "https://rss.example.com/normal.mp3", "source": "rss"},
            {"eid": "rss-1", "title": "共享下载第1期", "pub_date": "2026-08-20T08:00:00+08:00", "audio_url": "https://rss.example.com/shared.mp3", "source": "rss"},
            {"eid": "rss-2", "title": "Alice 原标题", "pub_date": "2026-08-21T08:00:00+08:00", "audio_url": "https://rss.example.com/alice.mp3", "source": "rss"},
        ]
        xyz = {
            "episode_count": 4, "episode_count_verified": True,
            "recent_episodes_raw": [
                {"eid": "x0", "title": "正常节目", "pubDate": "2026-08-19T08:00:00+08:00", "media": {"source": {"url": "https://public.example.com/normal.mp3"}}},
                {"eid": "x3", "title": "共享下载第3期", "pubDate": "2026-08-22T08:00:00+08:00", "media": {"source": {"url": "https://public.example.com/shared.mp3"}}},
                {"eid": "x2", "title": "Alice 修订标题", "pubDate": "2026-08-21T08:00:00+08:00", "media": {"source": {"url": "https://public.example.com/alice-new.mp3"}}},
            ],
        }
        normalized = src.normalize_public_recent(xyz)
        missing = next(ep for ep in normalized if ep["eid"] == "x3")
        self.assertEqual(src.media_fingerprint(missing["audio_url"]), src.media_fingerprint(rss[1]["audio_url"]))
        self.assertEqual(src._episode_match_strength(missing, rss[1]), 0)
        episodes, added, verified = src.merge_verified_public_gap(rss, xyz)
        self.assertEqual(added, 0)
        self.assertFalse(verified)
        self.assertEqual({ep["eid"] for ep in episodes}, {"rss-0", "rss-1", "rss-2"})

    def test_refresh_retains_verified_supplement_after_it_leaves_recent_window(self):
        base = src.parse_feed(RSS, "https://example.com/feed.xml")
        first_meta = {
            "episode_count": 3, "episode_count_verified": True,
            "recent_episodes_raw": [{"eid": "x3", "title": "EP3 补齐", "pubDate": "2026-08-27T00:00:00Z", "media": {"source": {"url": "https://example.com/e3.mp3"}}}],
        }
        first, added, verified = src.merge_verified_public_gap(base.episodes, first_meta)
        self.assertEqual((added, verified), (1, True))
        retained = [ep for ep in first if ep["source"] == "xiaoyuzhou_public"]
        rss4 = {"eid": "rss-4", "episode_id": "rss-4", "rss_guid": "g4", "title": "EP4 RSS 新节目", "pub_date": "2026-08-28T08:00:00+08:00", "published_at": "2026-08-28T08:00:00+08:00", "duration_seconds": 60, "description": "", "shownotes_html": "", "audio_url": "https://example.com/e4.mp3", "media_fingerprint": "/e4.mp3", "xiaoyuzhou_url": "", "source": "rss"}
        second_meta = {
            "episode_count": 4, "episode_count_verified": True,
            "recent_episodes_raw": [{"eid": "x4", "title": "EP4 RSS 新节目", "pubDate": "2026-08-28T08:00:00+08:00", "media": {"source": {"url": "https://example.com/e4.mp3"}}}],
        }
        second, added_again, verified_again = src.merge_verified_public_gap([rss4, *base.episodes], second_meta, retained)
        self.assertEqual((added_again, verified_again), (1, True))
        self.assertIn("x3", {ep["eid"] for ep in second})
        self.assertEqual(len(second), 4)

    def test_refresh_merges_exact_public_gap(self):
        feed = src.parse_feed(RSS, "https://example.com/feed.xml")
        podcast = {"title": "测试播客", "pid": "p1", "feed_url": feed.url, "xiaoyuzhou_url": "https://www.xiaoyuzhoufm.com/podcast/p1"}
        xyz = {
            "pid": "p1", "title": "测试播客", "episode_count": 3, "episode_count_verified": True,
            "recent_titles": ["EP3 新节目", "EP2 DeepSeek"],
            "recent_episodes_raw": [{"eid": "x3", "title": "EP3 新节目", "pubDate": "2026-08-27T00:00:00Z", "duration": 90, "media": {"source": {"url": "https://example.com/e3.mp3"}}}],
        }
        with patch.object(src, "fetch_feed", return_value=feed), patch.object(src, "fetch_xiaoyuzhou_podcast", return_value=xyz):
            episodes, coverage = src.refresh_rss_podcast(podcast)
        self.assertEqual(len(episodes), 3)
        self.assertEqual(coverage["public_supplement_count"], 1)
        self.assertTrue(coverage["history_complete"])

    def test_refresh_upgrades_and_persists_legacy_http_feed(self):
        feed = src.parse_feed(RSS, "https://example.com/feed.xml")
        podcast = {"title": "测试播客", "pid": "p1", "feed_url": "http://example.com/feed.xml", "xiaoyuzhou_url": ""}
        with patch.object(src, "fetch_feed", return_value=feed) as fetch:
            episodes, coverage = src.refresh_rss_podcast(podcast)
        fetch.assert_called_once_with("https://example.com/feed.xml")
        self.assertEqual(podcast["feed_url"], "https://example.com/feed.xml")
        self.assertEqual(len(episodes), 2)
        self.assertFalse(coverage["history_complete"])

    def test_public_supplement_has_media_fingerprint_for_future_rss_identity(self):
        xyz = {
            "pid": "p1", "title": "测试播客",
            "recent_episodes_raw": [{"eid": "x3", "title": "EP3 新节目", "pubDate": "2026-08-27T00:00:00Z", "media": {"source": {"url": "https://old.example.com/audio/e3.mp3?token=old"}}}],
        }
        public_episode = src.normalize_public_recent(xyz)[0]
        self.assertEqual(public_episode["media_fingerprint"], "/audio/e3.mp3")

    def test_refresh_keeps_rss_when_xiaoyuzhou_evidence_is_temporarily_down(self):
        feed = src.parse_feed(RSS, "https://example.com/feed.xml")
        podcast = {"title": "测试播客", "pid": "p1", "feed_url": feed.url, "xiaoyuzhou_url": "https://www.xiaoyuzhoufm.com/podcast/p1"}
        failure = src.SourceError("fetch_failed", "公开页暂时不可用", retryable=True)
        with patch.object(src, "fetch_feed", return_value=feed), patch.object(src, "fetch_xiaoyuzhou_podcast", side_effect=failure):
            episodes, coverage = src.refresh_rss_podcast(podcast)
        self.assertEqual(len(episodes), 2)
        self.assertFalse(coverage["history_complete"])
        self.assertIn("暂时不可用", coverage["history_reason"])

    def test_invalid_date_is_not_counted_as_episode(self):
        bad = RSS.replace(b"Wed, 26 Aug 2026 00:00:00 GMT", b"2026-13-01 garbage")
        feed = src.parse_feed(bad, "https://example.com/feed.xml")
        self.assertEqual(len(feed.episodes), 1)
        self.assertEqual(feed.invalid_item_count, 1)


if __name__ == "__main__":
    unittest.main()
