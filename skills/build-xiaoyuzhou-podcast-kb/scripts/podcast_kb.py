#!/usr/bin/env python3
"""Build a resumable, source-first Xiaoyuzhou podcast transcript knowledge base."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import fcntl
import hashlib
import html
import ipaddress
import json
import os
import random
import re
import socket
import sys
import tempfile
import time
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from public_sources import (
    SourceError,
    download_public_file,
    fetch_xiaoyuzhou_episode,
    refresh_rss_podcast,
    resolve_public_podcast,
)


SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULTS_PATH = SKILL_DIR / "config" / "defaults.json"
INDEX_FIELDS = [
    "episode_id", "rss_guid", "title", "published_at", "duration", "audio_url",
    "xiaoyuzhou_url", "source", "transcript_path", "status", "asr_model",
    "processed_at", "error",
]
CN_TZ = dt.timezone(dt.timedelta(hours=8))


class PipelineError(RuntimeError):
    def __init__(self, kind: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retryable = retryable

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": self.message, "retryable": self.retryable}


def now_iso() -> str:
    return dt.datetime.now(CN_TZ).isoformat(timespec="seconds")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise PipelineError("unsafe_config_permissions", f"配置文件必须是当前用户拥有的普通文件，不能是符号链接: {path}")
    if path.stat().st_mode & 0o077:
        raise PipelineError("unsafe_config_permissions", f"配置文件权限过宽，请执行 chmod 600 '{path}'")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config() -> dict[str, Any]:
    config_path = Path(os.environ.get("PODCAST_KB_ENV", "~/.config/build-xiaoyuzhou-podcast-kb/.env")).expanduser()
    load_dotenv(config_path)
    cfg = json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))
    env_map: dict[str, tuple[str, Callable[[str], Any]]] = {
        "ASR_MODEL": ("asr_model", str),
        "ASR_POLL_SECONDS": ("asr_poll_seconds", int),
        "ASR_MAX_WAIT_SECONDS": ("asr_max_wait_seconds", int),
        "ASR_MAX_RETRIES": ("asr_max_retries", int),
        "MAX_EPISODES_PER_RUN": ("max_episodes_per_run", int),
    }
    for env_key, (cfg_key, cast) in env_map.items():
        if os.environ.get(env_key):
            cfg[cfg_key] = cast(os.environ[env_key])
    cfg["dashscope_base_url"] = os.environ.get(
        "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/api/v1"
    ).rstrip("/")
    cfg["dashscope_base_explicit"] = bool(os.environ.get("DASHSCOPE_BASE_URL"))
    cfg["dashscope_api_key"] = os.environ.get("DASHSCOPE_API_KEY", "")
    cfg["config_path"] = str(config_path)
    return cfg


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


@contextmanager
def kb_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".kb.lock"
    lock_file = open(lock_path, "a+")
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PipelineError("kb_locked", f"另一个进程正在操作知识库: {root}") from exc
        lock_file.seek(0); lock_file.truncate(); lock_file.write(f"pid={os.getpid()} started={now_iso()}\n"); lock_file.flush()
        yield
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def safe_name(value: str, max_len: int = 80) -> str:
    value = unicodedata.normalize("NFKC", value or "untitled")
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" ._")
    return (value[:max_len].rstrip(" ._") or "untitled")


def slug(value: str) -> str:
    return safe_name(value, 72).replace(" ", "-")


def parse_iso_date(value: Optional[str]) -> Optional[dt.date]:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=CN_TZ)
        return parsed.astimezone(CN_TZ).date()
    except ValueError:
        try:
            return dt.date.fromisoformat(value[:10])
        except ValueError:
            return None


def parse_iso_datetime(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def subtract_years(day: dt.date, years: int) -> dt.date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def parse_time_range(text: Optional[str], today: Optional[dt.date] = None) -> dict[str, Any]:
    today = today or dt.datetime.now(CN_TZ).date()
    raw = (text or "全部节目").strip()
    compact = re.sub(r"\s+", "", raw.lower())
    if compact in {"全部", "全部节目", "所有", "all"}:
        return {"label": raw, "since": "0001-01-01", "until": today.isoformat(), "limit": None}
    m = re.fullmatch(r"最近(\d+)期", compact) or re.fullmatch(r"latest(\d+)", compact)
    if m:
        limit = int(m.group(1))
        if limit < 1:
            raise PipelineError("bad_time_range", "最近 N 期中的 N 必须大于 0")
        return {"label": raw, "since": None, "until": None, "limit": limit}
    cn_nums = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    m = re.fullmatch(r"过去([一二两三四五六七八九十]|\d+)年", compact)
    if m:
        years = cn_nums.get(m.group(1), int(m.group(1)) if m.group(1).isdigit() else 0)
        return {"label": raw, "since": subtract_years(today, years).isoformat(), "until": today.isoformat(), "limit": None}
    m = re.fullmatch(r"(?:past|last)(\d+)years?", compact)
    if m:
        years = int(m.group(1))
        return {"label": raw, "since": subtract_years(today, years).isoformat(), "until": today.isoformat(), "limit": None}
    m = re.fullmatch(r"(\d{4})年至今", compact)
    if m:
        return {"label": raw, "since": f"{m.group(1)}-01-01", "until": today.isoformat(), "limit": None}
    m = re.fullmatch(r"(\d{4})年", compact)
    if m:
        return {"label": raw, "since": f"{m.group(1)}-01-01", "until": f"{m.group(1)}-12-31", "limit": None}
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})", compact)
    if m:
        try:
            start, end = dt.date.fromisoformat(m.group(1)), dt.date.fromisoformat(m.group(2))
        except ValueError as exc:
            raise PipelineError("bad_time_range", f"非法日期范围: {raw}") from exc
        if start > end:
            raise PipelineError("bad_time_range", f"开始日期不能晚于结束日期: {raw}")
        return {"label": raw, "since": m.group(1), "until": m.group(2), "limit": None}
    raise PipelineError("bad_time_range", f"无法识别时间范围: {raw}")


def format_duration(seconds: Any) -> str:
    try:
        total = max(0, int(seconds or 0))
    except (TypeError, ValueError):
        return ""
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def timestamp(ms: Any) -> str:
    try:
        sec = max(0, int(ms or 0)) // 1000
    except (TypeError, ValueError):
        sec = 0
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def strip_html(value: Optional[str]) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def episode_number(title: str) -> str:
    for pattern in (r"(?i)\bEP\s*[-#:]?\s*(\d+)\b", r"(?i)\bE\s*[-#:]?\s*(\d+)\b", r"(?i)\bVOL\.?\s*(\d+)\b", r"第\s*(\d+)\s*期"):
        m = re.search(pattern, title or "")
        if m:
            return m.group(1)
    return ""


def validate_resource_id(value: Any, label: str = "episode_id") -> str:
    text = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise PipelineError("invalid_resource_id", f"非法 {label}: {text[:80]}")
    return text


def extract_guest(shownotes: Optional[str]) -> str:
    text = strip_html(shownotes)
    names: list[str] = []
    for m in re.finditer(r"(?:嘉宾|来宾|guest)\s*[:：]\s*([^。；;|\n]{2,50})", text, re.I):
        candidate = m.group(1).strip()
        if candidate not in names:
            names.append(candidate)
    return "、".join(names[:5])


def extract_hotwords(ep: dict[str, Any], podcast: dict[str, Any], limit: int = 200) -> list[str]:
    sources = [podcast.get("title", ""), podcast.get("author", ""), ep.get("title", ""), strip_html(ep.get("shownotes_html"))]
    words: list[str] = []
    def add(word: str) -> None:
        word = re.sub(r"\s+", " ", word).strip(" ，。,:：;；()（）[]【】\"'《》")
        if len(word) < 2 or word in words or word.isdigit():
            return
        if any(ord(char) > 127 for char in word):
            if len(word) > 15:
                return
        elif len(word.split()) > 7 or len(word) > 80:
            return
        words.append(word)
    for source in sources:
        if not source:
            continue
        for quoted in re.findall(r"[《「『【\"]([^》」』】\"]{2,40})[》」』】\"]", source):
            add(quoted)
        for english in re.findall(r"\b(?:[A-Z][A-Za-z0-9.+-]*)(?:\s+[A-Z][A-Za-z0-9.+-]*){0,3}\b", source):
            add(english)
        for label in ("主播", "主持人", "嘉宾", "公司", "品牌"):
            for m in re.finditer(rf"{label}\s*[:：]\s*([^。；;|\n]{{2,40}})", source):
                add(m.group(1))
        if source in sources[:3]:
            for part in re.split(r"[｜|：:，,、/\-—]", source):
                add(part)
    return words[:limit]


def retry_call(fn: Callable[[], Any], retries: int, label: str) -> Any:
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except HTTPError as exc:
            last = exc
            if exc.code not in {408, 409, 425, 429} and exc.code < 500:
                raise PipelineError("http_error", f"{label}: HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            last = exc
        if attempt < retries:
            time.sleep(min(60.0, (2 ** attempt) + random.random()))
    raise PipelineError("network_retry_exhausted", f"{label}: {last}", retryable=True) from last


def validate_remote_url(url: str, *, result_url: bool = False, auth_url: bool = False) -> None:
    parsed = urlparse(url)
    allowed_schemes = {"https"}
    if parsed.scheme not in allowed_schemes or not parsed.hostname or parsed.username or parsed.password:
        raise PipelineError("unsafe_url", f"不允许的远程 URL: {url[:120]}")
    host = parsed.hostname.rstrip(".").lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise PipelineError("unsafe_url", f"拒绝私网/本地地址: {host}")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise PipelineError("unsafe_url", f"拒绝本地主机: {host}")
    if result_url and not (host == "aliyuncs.com" or host.endswith(".aliyuncs.com")):
        raise PipelineError("unsafe_result_url", f"ASR 结果 URL 不在阿里云域名: {host}")
    if auth_url and not (
        host in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"}
        or host.endswith(".maas.aliyuncs.com")
    ):
        raise PipelineError("unsafe_auth_url", f"DashScope 认证请求只允许阿里云官方域名: {host}")
    if address is None:
        try:
            addresses = {ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)}
        except (OSError, ValueError) as exc:
            raise PipelineError("dns_resolution_failed", f"无法安全解析主机 {host}: {exc}", retryable=True) from exc
        if any(not ip.is_global for ip in addresses):
            raise PipelineError("unsafe_url", f"主机解析到私网/本地地址: {host}")


class SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, *, result_url: bool = False, auth_url: bool = False) -> None:
        super().__init__()
        self.result_url = result_url
        self.auth_url = auth_url

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_remote_url(newurl, result_url=self.result_url, auth_url=self.auth_url)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class DashScopeASR:
    def __init__(self, cfg: dict[str, Any]) -> None:
        if not cfg.get("dashscope_api_key"):
            raise PipelineError("asr_key_missing", "缺少 DASHSCOPE_API_KEY；请复制 .env.example 为 .env 并填写。")
        self.key = cfg["dashscope_api_key"]
        self.base = cfg["dashscope_base_url"]
        self.model = cfg["asr_model"]
        self.poll_seconds = cfg["asr_poll_seconds"]
        self.max_wait = cfg["asr_max_wait_seconds"]
        self.retries = cfg["asr_max_retries"]
        self.languages = cfg.get("language_hints", ["zh", "en"])
        self.hotword_weight = cfg.get("hotword_weight", 5)

    def _json_request(self, method: str, url: str, payload: Optional[dict[str, Any]] = None, *, auth: bool = True, retries: Optional[int] = None) -> Any:
        validate_remote_url(url, result_url=not auth, auth_url=auth)
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        # A signed OSS result URL validates request headers as part of its
        # signature. Only send Content-Type when there is actually a JSON body;
        # adding it to a result GET can make an otherwise valid URL return 403.
        headers: dict[str, str] = {}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = f"Bearer {self.key}"
        if method == "POST":
            headers["X-DashScope-Async"] = "enable"
        opener = build_opener(SafeRedirectHandler(result_url=not auth, auth_url=auth))
        def call() -> Any:
            with opener.open(Request(url, data=body, headers=headers, method=method), timeout=60) as resp:
                raw = resp.read(256 * 1024 * 1024 + 1)
                if len(raw) > 256 * 1024 * 1024:
                    raise PipelineError("response_too_large", "ASR JSON 超过 256 MiB 限制")
                return json.loads(raw.decode("utf-8"))
        return retry_call(call, self.retries if retries is None else retries, f"ASR {method}")

    def submit(self, audio_url: str, hotwords: list[str], diarization: bool) -> str:
        parameters: dict[str, Any] = {
            "channel_id": [0],
            "language_hints": self.languages,
            "diarization_enabled": diarization,
        }
        if hotwords and self.model.startswith("qwen-audio-3.0"):
            parameters["vocabulary"] = {word: self.hotword_weight for word in hotwords}
        if self.model.startswith("qwen3-"):
            input_obj: dict[str, Any] = {"file_url": audio_url}
            parameters = {"channel_id": [0], "language": self.languages[0] if self.languages else "zh", "enable_itn": True, "enable_words": False}
        else:
            input_obj = {"file_urls": [audio_url]}
        try:
            data = self._json_request(
            "POST", f"{self.base}/services/audio/asr/transcription",
            {"model": self.model, "input": input_obj, "parameters": parameters},
            retries=0,
            )
        except PipelineError as exc:
            if exc.kind in {"network_retry_exhausted"}:
                raise PipelineError("asr_submit_uncertain", "ASR 提交响应丢失；为避免重复计费，未自动重试。确认任务未创建后使用 --allow-uncertain-resubmit。") from exc
            raise
        task_id = ((data.get("output") or {}).get("task_id") if isinstance(data, dict) else None)
        if not task_id:
            raise PipelineError("asr_submit_uncertain", f"ASR 响应缺少 task_id；为避免重复计费，不会自动重新提交: {json.dumps(data, ensure_ascii=False)[:500]}")
        return str(task_id)

    @staticmethod
    def _result_url(data: dict[str, Any]) -> Optional[str]:
        output = data.get("output") or {}
        result = output.get("result") or {}
        if result.get("transcription_url"):
            return result["transcription_url"]
        for item in output.get("results") or []:
            if item.get("subtask_status") in {None, "SUCCEEDED"} and item.get("transcription_url"):
                return item["transcription_url"]
        return None

    def poll(self, task_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.max_wait
        while time.monotonic() < deadline:
            data = self._json_request("GET", f"{self.base}/tasks/{task_id}")
            status = str(((data.get("output") or {}).get("task_status") or "")).upper()
            if status == "SUCCEEDED":
                result_url = self._result_url(data)
                if not result_url:
                    raise PipelineError("asr_result_url_missing", "ASR 成功但未返回 transcription_url", retryable=True)
                return self._json_request("GET", result_url, auth=False)
            if status == "FAILED":
                raise PipelineError("asr_failed", json.dumps(data.get("output"), ensure_ascii=False)[:1000])
            if status not in {"PENDING", "RUNNING", ""}:
                raise PipelineError("asr_unknown_status", f"未知 ASR 状态: {status}", retryable=True)
            time.sleep(self.poll_seconds)
        raise PipelineError("asr_timeout", f"ASR 任务 {task_id} 超过 {self.max_wait} 秒", retryable=True)


def normalize_asr(data: dict[str, Any]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for transcript in data.get("transcripts") or []:
        sentences = transcript.get("sentences") or []
        if sentences:
            for item in sentences:
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                speaker_id = item.get("speaker_id")
                speaker = f"Speaker {int(speaker_id) + 1}" if speaker_id is not None else "Speaker 1"
                segments.append({
                    "start_ms": int(item.get("begin_time") or 0),
                    "end_ms": int(item.get("end_time") or 0),
                    "speaker": speaker,
                    "text": text,
                })
        else:
            text = str(transcript.get("text") or "").strip()
            if text:
                segments.append({"start_ms": 0, "end_ms": None, "speaker": "Speaker 1", "text": text})
    if not segments:
        raise PipelineError("asr_empty", "ASR 结果没有可用文本", retryable=True)
    return segments


def render_episode(ep: dict[str, Any], podcast: dict[str, Any], source: str, segments: list[dict[str, Any]], model: str) -> str:
    published = parse_iso_date(ep.get("pub_date"))
    lines = [
        f"# {ep.get('title') or ep.get('eid')}", "",
        f"Podcast: {podcast.get('title') or ep.get('podcast_title') or ''}",
        f"Published: {published.isoformat() if published else ''}",
        f"Duration: {format_duration(ep.get('duration_seconds'))}",
        f"URL: {ep.get('xiaoyuzhou_url') or ep.get('audio_url') or ''}",
        f"Transcript source: {source}",
        f"ASR model: {model if source == 'asr' else '-'}", "", "---", "", "## Transcript", "",
    ]
    for seg in segments:
        lines.extend([f"[{timestamp(seg.get('start_ms'))}] {seg.get('speaker') or 'Speaker 1'}:", seg["text"].strip(), ""])
    return "\n".join(lines).rstrip() + "\n"


def episode_filename(ep: dict[str, Any]) -> str:
    date = parse_iso_date(ep.get("pub_date"))
    number = episode_number(ep.get("title") or "")
    num = f"EP{int(number):03d}" if number else "EP"
    eid_tail = str(ep.get("eid") or "unknown")[-8:]
    return f"{date.isoformat() if date else 'unknown-date'}_{num}_{safe_name(ep.get('title') or '', 72)}_{eid_tail}.md"


def csv_safe(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@"}:
        return "'" + value
    return value


def empty_checkpoint(podcast: dict[str, Any], range_spec: dict[str, Any]) -> dict[str, Any]:
    return {"generator": "build-xiaoyuzhou-podcast-kb", "version": 2, "podcast": podcast, "range": range_spec, "coverage": {}, "episodes": {}, "updated_at": now_iso(), "stats": {}}


def validate_checkpoint(data: Any, expected_pid: Optional[str] = None) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("generator") != "build-xiaoyuzhou-podcast-kb" or data.get("version") != 2:
        raise PipelineError("invalid_checkpoint", "checkpoint 不是免登录版 Skill 生成的 v2 状态文件")
    podcast = data.get("podcast")
    if not isinstance(podcast, dict):
        raise PipelineError("invalid_checkpoint", "checkpoint 缺少 podcast")
    pid = validate_resource_id(podcast.get("pid"), "podcast_id")
    if expected_pid and pid != expected_pid:
        raise PipelineError("podcast_mismatch", f"目录属于 PID {pid}，不能写入 {expected_pid}")
    if not isinstance(data.get("episodes"), dict):
        raise PipelineError("invalid_checkpoint", "checkpoint episodes 必须是 object")
    return data


class KnowledgeBase:
    def __init__(self, root: Path, podcast: dict[str, Any], range_spec: dict[str, Any], cfg: dict[str, Any]) -> None:
        self.root, self.podcast, self.range, self.cfg = root, podcast, range_spec, cfg
        if root.is_symlink():
            raise PipelineError("unsafe_output_dir", f"拒绝把知识库写入符号链接目录: {root}")
        existing = [p for p in root.iterdir() if p.name != ".kb.lock"] if root.exists() else []
        if existing and not (root / "state/checkpoint.json").exists():
            raise PipelineError("unsafe_output_dir", f"输出目录非空且不是有效知识库: {root}")
        for rel in ("episodes", "raw/audio", "raw/metadata", "raw/transcripts", "state"):
            target = root / rel
            if target.is_symlink():
                raise PipelineError("unsafe_output_dir", f"拒绝符号链接子目录: {target}")
            target.mkdir(parents=True, exist_ok=True)
            if root.resolve() not in target.resolve().parents:
                raise PipelineError("unsafe_output_dir", f"子目录逃逸知识库根目录: {target}")
        self.checkpoint_path = root / "state/checkpoint.json"
        if self.checkpoint_path.is_symlink():
            raise PipelineError("unsafe_checkpoint", "拒绝符号链接 checkpoint")
        if self.checkpoint_path.exists():
            try:
                loaded = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise PipelineError("invalid_checkpoint", f"checkpoint 无法读取: {exc}") from exc
            self.checkpoint = validate_checkpoint(loaded, validate_resource_id(podcast.get("pid"), "podcast_id"))
        else:
            self.checkpoint = empty_checkpoint(podcast, range_spec)
        self.checkpoint["podcast"] = podcast
        self.checkpoint["range"] = range_spec
        self.reconcile()

    def save_checkpoint(self) -> None:
        self.checkpoint["updated_at"] = now_iso()
        self.checkpoint["stats"] = self.stats()
        atomic_write_json(self.checkpoint_path, self.checkpoint)

    def artifacts_valid(self, record: dict[str, Any]) -> bool:
        artifact_paths = [record.get("transcript_path"), record.get("raw_transcript_path")]
        if record.get("audio_path"):
            artifact_paths.append(record["audio_path"])
        for relative in artifact_paths:
            if not relative or Path(str(relative)).is_absolute():
                return False
            target = (self.root / str(relative)).resolve()
            if self.root.resolve() not in target.parents or not target.is_file() or target.stat().st_size == 0:
                return False
        return True

    def _scoped_artifact(self, relative: Any, directory: str) -> Optional[Path]:
        if not relative:
            return None
        rel = Path(str(relative))
        if rel.is_absolute() or ".." in rel.parts:
            return None
        expected = (self.root / directory).resolve()
        target = self.root / rel
        resolved = target.resolve()
        if expected != resolved.parent and expected not in resolved.parents:
            return None
        return target

    def transcript_artifacts_valid(self, record: dict[str, Any]) -> bool:
        pairs = [
            (record.get("transcript_path"), "episodes"),
            (record.get("raw_transcript_path"), "raw/transcripts"),
        ]
        for relative, directory in pairs:
            target = self._scoped_artifact(relative, directory)
            if target is None or not target.is_file() or target.stat().st_size == 0:
                return False
        return True

    def repair_transcript_from_raw(self, record: dict[str, Any]) -> bool:
        raw_relative = record.get("raw_transcript_path")
        transcript_relative = record.get("transcript_path")
        if not raw_relative or not transcript_relative:
            return False
        raw_path = self._scoped_artifact(raw_relative, "raw/transcripts")
        out_path = self._scoped_artifact(transcript_relative, "episodes")
        if raw_path is None or out_path is None:
            return False
        if not raw_path.is_file() or raw_path.stat().st_size == 0:
            return False
        try:
            raw_data = json.loads(raw_path.read_text(encoding="utf-8"))
            segments = normalize_asr(raw_data)
            ep = record.get("episode") or {}
            atomic_write_text(out_path, render_episode(ep, self.podcast, "asr", segments, record.get("asr_model") or self.cfg.get("asr_model", "")))
            return True
        except (OSError, json.JSONDecodeError, PipelineError):
            return False

    def reconcile(self) -> None:
        for sidecar in (self.root / "raw/metadata").glob("*.json"):
            try:
                record = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if record.get("status") == "completed" and record.get("episode_id"):
                try:
                    eid = validate_resource_id(record["episode_id"])
                    transcript_path = self.root / str(record.get("transcript_path") or "")
                    if (not transcript_path.is_file() or transcript_path.stat().st_size == 0) and self.repair_transcript_from_raw(record):
                        atomic_write_json(sidecar, record)
                    if not self.artifacts_valid(record):
                        continue
                except (PipelineError, OSError):
                    continue
                state = self.checkpoint["episodes"].setdefault(eid, {})
                state.update(record)
        for eid, state in self.checkpoint.get("episodes", {}).items():
            if state.get("status") == "completed":
                transcript_path = self.root / str(state.get("transcript_path") or "")
                if (not transcript_path.is_file() or transcript_path.stat().st_size == 0) and self.repair_transcript_from_raw(state):
                    sidecar = self.root / "raw/metadata" / f"{eid}.json"
                    atomic_write_json(sidecar, state)
                if self.artifacts_valid(state):
                    continue
                error_kind = "audio_missing" if state.get("audio_path") and self.transcript_artifacts_valid(state) else "artifact_missing"
                state.update({
                    "status": "failed",
                    "error": {"kind": error_kind, "message": "completed 状态的逐字稿/raw/audio 产物缺失或为空", "retryable": True},
                    "processed_at": now_iso(),
                })
        self.save_checkpoint()

    def remember_discovered(self, episodes: Iterable[dict[str, Any]]) -> None:
        for ep in episodes:
            try:
                eid = validate_resource_id(ep.get("eid"))
            except PipelineError as exc:
                key = f"invalid-{uuid.uuid4().hex}"
                self.checkpoint["episodes"][key] = {"status": "failed", "attempts": 0, "episode": ep, "error": exc.as_dict(), "processed_at": now_iso()}
                continue
            state = self.checkpoint["episodes"].setdefault(eid, {})
            state.setdefault("status", "pending")
            state.setdefault("attempts", 0)
            state["episode"] = ep
        self.save_checkpoint()

    def _set_failed(self, ep: dict[str, Any], exc: Exception) -> None:
        eid = ep["eid"]
        state = self.checkpoint["episodes"].setdefault(eid, {"episode": ep, "attempts": 0})
        info = exc.as_dict() if isinstance(exc, PipelineError) else {"kind": "unexpected", "message": str(exc), "retryable": False}
        state.update({"status": "failed", "error": info, "processed_at": now_iso()})
        # A terminal provider result must be resubmitted on retry. Preserve the
        # task id only for interruptions/timeouts/transport failures so resume
        # can keep polling without creating a duplicate billable task.
        if info.get("kind") in {"asr_failed", "asr_empty"}:
            state.pop("asr_task_id", None)
        self.save_checkpoint()

    def process(self, ep: dict[str, Any], keep_audio: bool = False, force: bool = False, allow_uncertain_resubmit: bool = False) -> bool:
        try:
            eid = validate_resource_id(ep.get("eid"))
            state = self.checkpoint["episodes"].setdefault(eid, {"episode": ep, "attempts": 0})
            if state.get("status") == "completed" and not force:
                return False
            if (state.get("error") or {}).get("kind") == "audio_missing" and state.get("transcript_path") and state.get("raw_transcript_path"):
                if not self.transcript_artifacts_valid(state):
                    raise PipelineError("artifact_missing", "逐字稿或原始 ASR 结果也缺失，不能只补音频")
                audio_path = self.download_audio(ep)
                state["audio_path"] = str(audio_path.relative_to(self.root))
                state.update({"status": "completed", "error": "", "processed_at": now_iso()})
                if not self.artifacts_valid(state):
                    raise PipelineError("artifact_missing", "补下载后产物校验失败")
                atomic_write_json(self.root / "raw/metadata" / f"{eid}.json", state)
                self.save_checkpoint()
                self.write_outputs()
                return True
            try:
                attempts = int(state.get("attempts", 0)) + 1
            except (TypeError, ValueError):
                attempts = 1
            state.update({"status": "processing", "episode": ep, "attempts": attempts, "error": None})
            self.save_checkpoint()
            if not ep.get("audio_url"):
                raise PipelineError("audio_url_missing", "公开 Episode 没有音频 URL")
            asr = DashScopeASR(self.cfg)
            duration = int(ep.get("duration_seconds") or 0)
            diarization = 0 < duration <= 7200
            hotwords = extract_hotwords(ep, self.podcast, self.cfg.get("hotword_limit", 200))
            task_id = state.get("asr_task_id")
            if not task_id:
                if state.get("asr_submit_intent") and not allow_uncertain_resubmit:
                    raise PipelineError("asr_submission_uncertain", "之前的 ASR 提交结果不确定；确认云端无任务后使用 --allow-uncertain-resubmit。")
                if allow_uncertain_resubmit:
                    state.pop("asr_submit_intent", None)
                validate_remote_url(ep["audio_url"])
                state["asr_submit_intent"] = {"id": uuid.uuid4().hex, "started_at": now_iso()}
                self.save_checkpoint()
                try:
                    task_id = asr.submit(ep["audio_url"], hotwords, diarization)
                except PipelineError as exc:
                    if exc.kind != "asr_submit_uncertain":
                        state.pop("asr_submit_intent", None)
                        self.save_checkpoint()
                    raise
                state.update({"asr_task_id": task_id, "asr_model": asr.model, "diarization_enabled": diarization, "hotwords": hotwords})
                state.pop("asr_submit_intent", None)
                self.save_checkpoint()
            raw_data = asr.poll(task_id)
            source, model = "asr", asr.model
            segments = normalize_asr(raw_data)

            transcript_raw_path = self.root / "raw/transcripts" / f"{eid}.{source}.json"
            atomic_write_json(transcript_raw_path, raw_data)
            out_path = self.root / "episodes" / episode_filename(ep)
            atomic_write_text(out_path, render_episode(ep, self.podcast, source, segments, model))
            record = {
                "episode_id": eid,
                "rss_guid": ep.get("rss_guid") or "",
                "title": ep.get("title") or "",
                "published_at": parse_iso_date(ep.get("pub_date")).isoformat() if parse_iso_date(ep.get("pub_date")) else "",
                "duration": format_duration(ep.get("duration_seconds")),
                "audio_url": ep.get("audio_url") or "",
                "xiaoyuzhou_url": ep.get("xiaoyuzhou_url") or "",
                "source": ep.get("source") or "rss",
                "transcript_path": str(out_path.relative_to(self.root)),
                "raw_transcript_path": str(transcript_raw_path.relative_to(self.root)),
                "status": "completed",
                "asr_model": model,
                "processed_at": now_iso(),
                "error": "",
                "episode": ep,
                "attempts": state["attempts"],
            }
            if keep_audio:
                audio_path = self.download_audio(ep)
                record["audio_path"] = str(audio_path.relative_to(self.root))
            atomic_write_json(self.root / "raw/metadata" / f"{eid}.json", record)
            state.update(record)
            state.pop("asr_task_id", None)
            self.save_checkpoint()
            try:
                self.write_outputs()
            except Exception as exc:
                print(f"[warning] 衍生索引重建失败，可从 checkpoint 重建: {exc}", file=sys.stderr)
            return True
        except Exception as exc:
            try:
                self._set_failed(ep, exc)
                self.write_outputs()
            except Exception as state_exc:
                print(f"[warning] 失败状态写入不完整: {state_exc}", file=sys.stderr)
            print(f"[failed] {ep.get('title')}: {exc}", file=sys.stderr)
            return False

    def download_audio(self, ep: dict[str, Any]) -> Path:
        validate_resource_id(ep.get("eid"))
        validate_remote_url(ep["audio_url"])
        parsed = urlparse(ep["audio_url"])
        ext = Path(parsed.path).suffix.lower()
        if ext not in {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"}:
            ext = ".audio"
        target = self.root / "raw/audio" / f"{ep['eid']}{ext}"
        if target.is_symlink():
            raise PipelineError("unsafe_audio_path", f"拒绝音频符号链接: {target}")
        if target.exists() and target.stat().st_size > 0:
            return target
        tmp = target.parent / f".{target.name}.{uuid.uuid4().hex}.part"
        try:
            download_public_file(ep["audio_url"], tmp)
            os.replace(tmp, target)
        except SourceError as exc:
            raise _translate_source_error(exc) from exc
        finally:
            tmp.unlink(missing_ok=True)
        return target

    def stats(self) -> dict[str, int]:
        states = list((self.checkpoint.get("episodes") or {}).values())
        coverage = self.checkpoint.get("coverage") or {}
        return {
            "discovered": int(coverage.get("discovered_total", len(states))),
            "in_range": int(coverage.get("in_range_total", len(states))),
            "completed": sum(s.get("status") == "completed" for s in states),
            "asr": sum(s.get("status") == "completed" for s in states),
            "failed": sum(s.get("status") == "failed" for s in states),
            "pending": sum(s.get("status") in {"pending", "processing"} for s in states),
        }

    def write_outputs(self) -> None:
        records: list[dict[str, Any]] = []
        for state in self.checkpoint.get("episodes", {}).values():
            if state.get("status") == "completed":
                records.append({key: csv_safe(state.get(key, "")) for key in INDEX_FIELDS})
            elif state.get("status") == "failed":
                ep = state.get("episode") or {}
                err = state.get("error") or {}
                records.append({
                    "episode_id": ep.get("eid", ""), "rss_guid": ep.get("rss_guid", ""),
                    "title": ep.get("title", ""), "published_at": parse_iso_date(ep.get("pub_date")).isoformat() if parse_iso_date(ep.get("pub_date")) else "",
                    "duration": format_duration(ep.get("duration_seconds")), "audio_url": ep.get("audio_url", ""),
                    "xiaoyuzhou_url": ep.get("xiaoyuzhou_url", ""), "source": ep.get("source", "rss"), "transcript_path": "",
                    "status": "failed", "asr_model": state.get("asr_model", ""), "processed_at": state.get("processed_at", ""),
                    "error": err.get("message", str(err)),
                })
        records.sort(key=lambda r: (r.get("published_at", ""), r.get("episode_id", "")))
        fd, tmp_name = tempfile.mkstemp(prefix=".index.", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=INDEX_FIELDS)
                writer.writeheader(); writer.writerows([{k: csv_safe(v) for k, v in row.items()} for row in records])
                f.flush(); os.fsync(f.fileno())
            os.replace(tmp_name, self.root / "index.csv")
        except Exception:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        metadata = {"podcast": self.podcast, "range": self.range, "coverage": self.checkpoint.get("coverage", {}), "stats": self.stats(), "updated_at": now_iso(), "source_layer": "episodes/"}
        atomic_write_json(self.root / "metadata.json", metadata)
        stats = metadata["stats"]
        readme = (
            f"# {self.podcast.get('title') or self.podcast.get('pid')}\n\n"
            "这是逐字稿 Source Layer；原文不包含 AI 总结。\n\n"
            f"- history_source = {metadata['coverage'].get('history_source', 'unknown')}\n"
            f"- history_complete = {str(bool(metadata['coverage'].get('history_complete'))).lower()}\n"
            f"- 完整性说明：{metadata['coverage'].get('history_reason', '')}\n"
            f"- 发现总节目：{stats['discovered']}\n- 范围内：{stats['in_range']}\n- 完成：{stats['completed']}\n"
            f"- ASR：{stats['asr']}\n- 失败：{stats['failed']}\n- 更新时间：{metadata['updated_at']}\n"
        )
        atomic_write_text(self.root / "README.md", readme)


def parse_resource_id(value: str, kind: str) -> Optional[str]:
    pattern = rf"xiaoyuzhoufm\.com/{kind}/([A-Za-z0-9_-]+)"
    m = re.search(pattern, value or "")
    return m.group(1) if m else None


def _translate_source_error(exc: SourceError) -> PipelineError:
    return PipelineError(exc.kind, exc.message, retryable=exc.retryable)


def filter_episodes(all_episodes: list[dict[str, Any]], range_spec: dict[str, Any]) -> list[dict[str, Any]]:
    if range_spec.get("limit"):
        return all_episodes[: int(range_spec["limit"])]
    return [
        ep for ep in all_episodes
        if (day := parse_iso_date(ep.get("pub_date")))
        and range_spec["since"] <= day.isoformat() <= range_spec["until"]
    ]


def source_snapshot_tokens(episodes: Iterable[dict[str, Any]]) -> list[str]:
    tokens: set[str] = set()
    for ep in episodes:
        if ep.get("eid"):
            tokens.add(f"eid:{ep['eid']}")
        if ep.get("media_fingerprint"):
            title_key = unicodedata.normalize("NFKC", str(ep.get("title") or "")).casefold().strip()
            fingerprint_hash = hashlib.sha256(str(ep["media_fingerprint"]).encode("utf-8", errors="replace")).hexdigest()
            title_hash = hashlib.sha256(title_key.encode("utf-8", errors="replace")).hexdigest()
            published = str(ep.get("pub_date") or ep.get("published_at") or "")
            tokens.add(f"media-date:{fingerprint_hash}:{published}")
            tokens.add(f"media-title:{fingerprint_hash}:{title_hash}")
            tokens.add(f"media-fp:{fingerprint_hash}")
    return sorted(tokens)


def source_was_seen(ep: dict[str, Any], snapshot: set[str], ambiguous_fingerprints: Optional[set[str]] = None) -> bool:
    eid = str(ep.get("eid") or "")
    if eid and (eid in snapshot or f"eid:{eid}" in snapshot):
        return True
    fingerprint = ep.get("media_fingerprint")
    if fingerprint:
        fingerprint_hash = hashlib.sha256(str(fingerprint).encode("utf-8", errors="replace")).hexdigest()
        if fingerprint_hash in (ambiguous_fingerprints or set()):
            return False
        title_key = unicodedata.normalize("NFKC", str(ep.get("title") or "")).casefold().strip()
        title_hash = hashlib.sha256(title_key.encode("utf-8", errors="replace")).hexdigest()
        published = str(ep.get("pub_date") or ep.get("published_at") or "")
        return (
            f"media-date:{fingerprint_hash}:{published}" in snapshot
            and f"media-title:{fingerprint_hash}:{title_hash}" in snapshot
        )
    return False


def source_identity_ambiguous(ep: dict[str, Any], snapshot: set[str], ambiguous_fingerprints: set[str]) -> bool:
    eid = str(ep.get("eid") or "")
    if eid and (eid in snapshot or f"eid:{eid}" in snapshot):
        return False
    fingerprint = ep.get("media_fingerprint")
    if not fingerprint:
        return False
    digest = hashlib.sha256(str(fingerprint).encode("utf-8", errors="replace")).hexdigest()
    if f"media-fp:{digest}" not in snapshot:
        return False
    return digest in ambiguous_fingerprints or not source_was_seen(ep, snapshot, ambiguous_fingerprints)


def resolve_podcast(value: str, range_spec: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], list[str]]:
    try:
        podcast, all_episodes, coverage = resolve_public_podcast(value)
    except SourceError as exc:
        raise _translate_source_error(exc) from exc
    return podcast, filter_episodes(all_episodes, range_spec), coverage, source_snapshot_tokens(all_episodes)


def choose_kb_root(output_root: Path, podcast: dict[str, Any]) -> Path:
    if output_root.is_symlink():
        raise PipelineError("unsafe_output_dir", f"拒绝符号链接输出目录: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)
    pid = validate_resource_id(podcast.get("pid"), "podcast_id")
    for checkpoint in output_root.glob("*/state/checkpoint.json"):
        try:
            if checkpoint.is_symlink():
                continue
            data = validate_checkpoint(json.loads(checkpoint.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, PipelineError):
            continue
        if (data.get("podcast") or {}).get("pid") == pid:
            return checkpoint.parent.parent
    return output_root / f"{slug(podcast.get('title') or pid)}--{pid[-8:]}"


def print_stats(stats: dict[str, int], *, in_range: Optional[int] = None, skipped: int = 0) -> None:
    result = {"discovered": stats.get("discovered", 0), "in_range": in_range if in_range is not None else stats.get("discovered", 0), **stats, "skipped": skipped}
    print(json.dumps(result, ensure_ascii=False, indent=2))


def print_coverage(coverage: dict[str, Any]) -> None:
    print(json.dumps({"coverage": coverage}, ensure_ascii=False, indent=2))


def enforce_batch_limit(episodes: list[dict[str, Any]], args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    limit = int(getattr(args, "max_episodes", None) or cfg.get("max_episodes_per_run", 5))
    if len(episodes) <= limit:
        return
    known_seconds = sum(int(ep.get("duration_seconds") or 0) for ep in episodes)
    unknown = sum(not int(ep.get("duration_seconds") or 0) for ep in episodes)
    raise PipelineError(
        "batch_confirmation_required",
        f"本次将提交 {len(episodes)} 期付费 ASR，已知总时长约 {known_seconds / 3600:.1f} 小时，未知时长 {unknown} 期。"
        f"请先确认费用，再使用 --max-episodes {len(episodes)} 重跑。",
    )


def cmd_build(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    range_spec = parse_time_range(args.time_range)
    podcast, episodes, coverage, source_snapshot_ids = resolve_podcast(args.podcast, range_spec)
    enforce_batch_limit(episodes, args, cfg)
    root = choose_kb_root(Path(args.output_root).expanduser().resolve(), podcast)
    with kb_lock(root):
        kb = KnowledgeBase(root, podcast, range_spec, cfg)
        discovered_total = coverage.get("merged_episode_count", coverage.get("rss_episode_count", coverage.get("public_episode_count", len(episodes))))
        kb.checkpoint["coverage"] = {**coverage, "discovered_total": discovered_total, "in_range_total": len(episodes)}
        kb.checkpoint["source_snapshot_ids"] = source_snapshot_ids
        kb.remember_discovered(episodes)
        skipped = 0
        for idx, ep in enumerate(episodes, 1):
            eid = ep.get("eid")
            before = kb.checkpoint["episodes"].get(str(eid), {}).get("status")
            print(f"[{idx}/{len(episodes)}] {ep.get('title')}", file=sys.stderr)
            kb.process(ep, keep_audio=args.keep_audio, force=args.force, allow_uncertain_resubmit=args.allow_uncertain_resubmit)
            if before == "completed" and not args.force:
                skipped += 1
        kb.write_outputs(); kb.save_checkpoint()
    print(f"knowledge_base={root}")
    print_coverage(kb.checkpoint.get("coverage") or {})
    stats = kb.stats()
    print_stats(stats, in_range=len(episodes), skipped=skipped)
    return 0 if kb.stats()["failed"] == 0 else 2


def open_existing(args: argparse.Namespace, cfg: dict[str, Any]) -> KnowledgeBase:
    root = Path(args.kb_dir).expanduser().resolve()
    cp = root / "state/checkpoint.json"
    if not cp.exists():
        raise PipelineError("checkpoint_missing", f"找不到 {cp}")
    try:
        data = validate_checkpoint(json.loads(cp.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError("invalid_checkpoint", f"checkpoint 无法读取: {exc}") from exc
    return KnowledgeBase(root, data["podcast"], data.get("range") or parse_time_range("全部节目"), cfg)


def cmd_update(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    root = Path(args.kb_dir).expanduser().resolve()
    with kb_lock(root):
        kb = open_existing(args, cfg)
        original = kb.range
        today = dt.datetime.now(CN_TZ).date().isoformat()
        try:
            if kb.podcast.get("feed_url"):
                all_scanned, refreshed_coverage = refresh_rss_podcast(kb.podcast)
            else:
                refreshed_podcast, all_scanned, refreshed_coverage = resolve_public_podcast(kb.podcast.get("xiaoyuzhou_url") or kb.podcast.get("title"))
                kb.podcast.update(refreshed_podcast)
                kb.checkpoint["podcast"] = kb.podcast
        except SourceError as exc:
            raise _translate_source_error(exc) from exc
        update_range = {**original, "until": today}
        snapshot = set(kb.checkpoint.get("source_snapshot_ids") or [])
        fingerprint_counts: dict[str, int] = {}
        for ep in all_scanned:
            if ep.get("media_fingerprint"):
                digest = hashlib.sha256(str(ep["media_fingerprint"]).encode("utf-8", errors="replace")).hexdigest()
                fingerprint_counts[digest] = fingerprint_counts.get(digest, 0) + 1
        ambiguous_fingerprints = {digest for digest, count in fingerprint_counts.items() if count > 1}
        identity_ambiguities = [ep for ep in all_scanned if source_identity_ambiguous(ep, snapshot, ambiguous_fingerprints)]
        if identity_ambiguities and not getattr(args, "accept_ambiguous_as_new", False):
            titles = "；".join(str(ep.get("title") or ep.get("eid"))[:80] for ep in identity_ambiguities[:5])
            raise PipelineError(
                "source_identity_ambiguous",
                f"发现 {len(identity_ambiguities)} 期复用了旧媒体地址但身份信息已变化，已在付费 ASR 前停止：{titles}。"
                "确认它们确实是新节目后，使用 --accept-ambiguous-as-new 重跑。",
            )
        if original.get("limit"):
            known_ids = set(kb.checkpoint["episodes"])
            # New builds remember the full source snapshot even when only the
            # latest N were transcribed. This detects same-time and backfilled
            # RSS items without treating every older historical item as new.
            # Legacy checkpoints without a snapshot scan once; the paid batch
            # gate prevents an accidental large submission.
            scanned = all_scanned if not snapshot else [ep for ep in all_scanned if ep.get("eid") in known_ids or not source_was_seen(ep, snapshot, ambiguous_fingerprints)]
        else:
            in_range = filter_episodes(all_scanned, update_range)
            scanned = in_range if not snapshot else [ep for ep in in_range if ep.get("eid") in kb.checkpoint["episodes"] or not source_was_seen(ep, snapshot, ambiguous_fingerprints)]
        known = set(kb.checkpoint["episodes"])
        new = [ep for ep in scanned if ep.get("eid") not in known]
        resume = [ep for ep in scanned if ep.get("eid") in known and kb.checkpoint["episodes"][ep["eid"]].get("status") in {"pending", "processing"}]
        # Refresh failed/pending metadata, but never rewrite completed source files.
        for ep in scanned:
            if ep.get("eid") in known and kb.checkpoint["episodes"][ep["eid"]].get("status") != "completed":
                kb.checkpoint["episodes"][ep["eid"]]["episode"] = ep
        kb.range = {**update_range, "label": f"{original.get('label', '')} + incremental updates"}
        kb.checkpoint["range"] = kb.range
        kb.remember_discovered(new)
        coverage = kb.checkpoint.setdefault("coverage", {})
        coverage.update(refreshed_coverage)
        coverage["discovered_total"] = len(all_scanned)
        coverage["in_range_total"] = len(scanned)
        queue = list(reversed(new)) + resume
        enforce_batch_limit(queue, args, cfg)
        for idx, ep in enumerate(queue, 1):
            print(f"[{idx}/{len(queue)}] {ep.get('title')}", file=sys.stderr)
            kb.process(ep, keep_audio=args.keep_audio, allow_uncertain_resubmit=args.allow_uncertain_resubmit)
        previous_snapshot = set(kb.checkpoint.get("source_snapshot_ids") or [])
        current_snapshot = set(source_snapshot_tokens(all_scanned))
        # RSS feeds can be temporarily truncated. Never forget IDs already
        # observed, otherwise old history may look "new" when the feed heals.
        kb.checkpoint["source_snapshot_ids"] = sorted(previous_snapshot | current_snapshot)
        kb.write_outputs(); kb.save_checkpoint()
    print(f"new_episodes={len(new)}")
    print(f"resumed_episodes={len(resume)}")
    print_coverage(kb.checkpoint.get("coverage") or {})
    print_stats(kb.stats(), in_range=len(new) + len(resume))
    return 0 if kb.stats()["failed"] == 0 else 2


def cmd_retry(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    root = Path(args.kb_dir).expanduser().resolve()
    with kb_lock(root):
        kb = open_existing(args, cfg)
        failed = [s.get("episode") for s in kb.checkpoint["episodes"].values() if s.get("status") == "failed" and s.get("episode")]
        enforce_batch_limit(failed, args, cfg)
        for idx, ep in enumerate(failed, 1):
            print(f"[{idx}/{len(failed)}] {ep.get('title')}", file=sys.stderr)
            kb.process(ep, keep_audio=args.keep_audio, force=True, allow_uncertain_resubmit=args.allow_uncertain_resubmit)
        kb.write_outputs(); kb.save_checkpoint()
    print_coverage(kb.checkpoint.get("coverage") or {})
    print_stats(kb.stats(), in_range=len(failed))
    return 0 if kb.stats()["failed"] == 0 else 2


def cmd_status(args: argparse.Namespace) -> int:
    root = Path(args.kb_dir).expanduser().resolve()
    cp = root / "state/checkpoint.json"
    if not cp.exists():
        raise PipelineError("checkpoint_missing", f"找不到 {cp}")
    try:
        data = validate_checkpoint(json.loads(cp.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError("invalid_checkpoint", f"checkpoint 无法读取: {exc}") from exc
    states = list((data.get("episodes") or {}).values())
    completed = sorted((s for s in states if s.get("status") == "completed"), key=lambda s: s.get("published_at", ""))
    failed = [s for s in states if s.get("status") == "failed"]
    processing = [s for s in states if s.get("status") == "processing"]
    pending = [s for s in states if s.get("status") == "pending"]
    processed = sorted((s for s in states if s.get("processed_at")), key=lambda s: s.get("processed_at", ""))
    def brief(s):
        ep = s.get("episode") or {}
        return {"episode_id": s.get("episode_id") or ep.get("eid"), "title": s.get("title") or ep.get("title"), "published_at": s.get("published_at") or (parse_iso_date(ep.get("pub_date")).isoformat() if parse_iso_date(ep.get("pub_date")) else "")}
    print(json.dumps({
        "podcast": data.get("podcast"), "range": data.get("range"), "coverage": data.get("coverage"), "stats": data.get("stats"),
        "last_processed": brief(processed[-1]) if processed else None,
        "oldest_completed": brief(completed[0]) if completed else None,
        "newest_completed": brief(completed[-1]) if completed else None,
        "next_pending": brief(pending[0]) if pending else None,
        "processing": [{"episode_id": (s.get("episode") or {}).get("eid"), "title": (s.get("episode") or {}).get("title")} for s in processing],
        "failed": [{"episode_id": (s.get("episode") or {}).get("eid"), "title": (s.get("episode") or {}).get("title"), "error": (s.get("error") or {}).get("message")} for s in failed],
        "updated_at": data.get("updated_at"),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_single(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    eid = parse_resource_id(args.episode_url, "episode")
    if not eid:
        raise PipelineError("bad_episode_url", "请输入有效的小宇宙 episode URL")
    try:
        podcast, ep = fetch_xiaoyuzhou_episode(eid)
    except SourceError as exc:
        raise _translate_source_error(exc) from exc
    root = choose_kb_root(Path(args.output_root).expanduser().resolve(), podcast)
    with kb_lock(root):
        kb = KnowledgeBase(root, podcast, {"label": "single", "since": None, "until": None, "limit": 1}, cfg)
        kb.checkpoint["coverage"] = {"history_source": "xiaoyuzhou_public", "history_complete": False, "history_reason": "单集模式不判断播客历史完整性", "discovered_total": 1, "in_range_total": 1}
        kb.remember_discovered([ep]); kb.process(ep, keep_audio=args.keep_audio, force=args.force, allow_uncertain_resubmit=args.allow_uncertain_resubmit)
        kb.write_outputs(); kb.save_checkpoint()
    print(f"knowledge_base={root}"); print_stats(kb.stats(), in_range=1)
    return 0 if kb.stats()["failed"] == 0 else 2


def cmd_doctor(cfg: dict[str, Any]) -> int:
    checks: dict[str, Any] = {
        "python": sys.version.split()[0],
        "python_ok": sys.version_info >= (3, 9),
        "login_required": False,
        "public_sources_ok": True,
    }
    checks["dashscope_key_set"] = bool(cfg.get("dashscope_api_key"))
    checks["config_path"] = cfg.get("config_path")
    checks["dashscope_base_url"] = cfg.get("dashscope_base_url")
    checks["dashscope_base_explicit"] = cfg.get("dashscope_base_explicit", False)
    checks["asr_model"] = cfg.get("asr_model")
    checks["ready"] = checks["python_ok"] and checks["public_sources_ok"] and checks["dashscope_key_set"] and checks["dashscope_base_explicit"]
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if checks["ready"] else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--podcast", required=True, help="Podcast name, URL, or PID URL")
    build.add_argument("--time-range", default="全部节目")
    build.add_argument("--output-root", default="podcast-knowledge-base")
    build.add_argument("--keep-audio", action="store_true")
    build.add_argument("--force", action="store_true")
    build.add_argument("--allow-uncertain-resubmit", action="store_true", help="仅在确认云端没有先前任务后使用")
    build.add_argument("--max-episodes", type=int, help="用户确认费用后设置本次允许提交的最大集数")
    single = sub.add_parser("single")
    single.add_argument("--episode-url", required=True)
    single.add_argument("--output-root", default="podcast-knowledge-base")
    single.add_argument("--keep-audio", action="store_true")
    single.add_argument("--force", action="store_true")
    single.add_argument("--allow-uncertain-resubmit", action="store_true")
    for name in ("update", "retry"):
        sp = sub.add_parser(name); sp.add_argument("--kb-dir", required=True); sp.add_argument("--keep-audio", action="store_true"); sp.add_argument("--allow-uncertain-resubmit", action="store_true"); sp.add_argument("--max-episodes", type=int)
        if name == "update":
            sp.add_argument("--accept-ambiguous-as-new", action="store_true", help="确认媒体地址复用项确为新节目后使用")
    status = sub.add_parser("status"); status.add_argument("--kb-dir", required=True)
    sub.add_parser("doctor")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config()
        if args.command == "build": return cmd_build(args, cfg)
        if args.command == "single": return cmd_single(args, cfg)
        if args.command == "update": return cmd_update(args, cfg)
        if args.command == "retry": return cmd_retry(args, cfg)
        if args.command == "status": return cmd_status(args)
        if args.command == "doctor": return cmd_doctor(cfg)
        return 1
    except PipelineError as exc:
        print(json.dumps({"error": exc.kind, "message": exc.message, "retryable": exc.retryable}, ensure_ascii=False), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"error": "interrupted", "message": "已保留 checkpoint，可重跑相同命令继续"}, ensure_ascii=False), file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
