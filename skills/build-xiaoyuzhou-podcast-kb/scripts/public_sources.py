#!/usr/bin/env python3
"""Resolve public Xiaoyuzhou podcasts to verified Apple/RSS histories."""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import http.client
import ipaddress
import json
import os
import random
import re
import socket
import ssl
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit


USER_AGENT = "build-xiaoyuzhou-podcast-kb/2.0 (+public-rss)"
ITUNES_SEARCH = "https://itunes.apple.com/search"
XYZ_WEB = "https://www.xiaoyuzhoufm.com"
MAX_BODY = 64 * 1024 * 1024


class SourceError(RuntimeError):
    def __init__(self, kind: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retryable = retryable


def _host_is_public(host: str) -> bool:
    if host.lower() in {"localhost", "localhost.localdomain"} or host.lower().endswith(".local"):
        return False
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]
        except (OSError, ValueError):
            return False
    return bool(addresses) and all(ip.is_global for ip in addresses)


def validate_public_url(url: str, *, require_https: bool = True) -> None:
    parsed = urlparse(url)
    # Public metadata and audio become the source of truth and may trigger paid
    # ASR work. Refuse clear-text transport instead of silently ingesting data
    # that a network intermediary could alter.
    allowed = {"https"}
    if parsed.scheme not in allowed or not parsed.hostname:
        raise SourceError("unsafe_url", f"不允许的公开 URL: {url[:160]}")
    if parsed.username or parsed.password or not _host_is_public(parsed.hostname):
        raise SourceError("unsafe_url", f"拒绝本地、私网或带账号信息的 URL: {url[:160]}")


def _global_ips(host: str, port: int) -> list[str]:
    try:
        values = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            values = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
        except OSError as exc:
            raise SourceError("dns_resolution_failed", f"无法解析公开主机 {host}: {exc}", retryable=True) from exc
    if not values or any(not ipaddress.ip_address(value).is_global for value in values):
        raise SourceError("unsafe_url", f"主机不是纯公网地址: {host}")
    return values


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, pinned_ip: str, port: int, timeout: int) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout, self.source_address)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, pinned_ip: str, port: int, timeout: int) -> None:
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        raw = socket.create_connection((self._pinned_ip, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def _fetch_once(url: str, *, timeout: int, max_bytes: int) -> bytes:
    current = url
    for _ in range(6):
        validate_public_url(current)
        parsed = urlparse(current)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        ips = _global_ips(host, port)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        last: Optional[Exception] = None
        for pinned_ip in ips:
            connection = None
            try:
                cls = _PinnedHTTPSConnection if parsed.scheme == "https" else _PinnedHTTPConnection
                connection = cls(host, pinned_ip, port, timeout)
                connection.request("GET", path, headers={
                    "Host": host if parsed.port is None else f"{host}:{port}",
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json, application/rss+xml, application/xml, text/xml, text/html;q=0.9, */*;q=0.1",
                    "Connection": "close",
                })
                peer_ip = ipaddress.ip_address(connection.sock.getpeername()[0])
                if not peer_ip.is_global or peer_ip != ipaddress.ip_address(pinned_ip):
                    raise SourceError("unsafe_peer", f"实际连接地址未经验证: {peer_ip}")
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location:
                        raise SourceError("redirect_missing_location", f"重定向缺少 Location: {current}")
                    current = urljoin(current, location)
                    break
                if response.status in {408, 425, 429} or response.status >= 500:
                    raise SourceError("fetch_http_retryable", f"公开地址返回 HTTP {response.status}: {current}", retryable=True)
                if response.status < 200 or response.status >= 300:
                    raise SourceError("fetch_http_error", f"公开地址返回 HTTP {response.status}: {current}")
                length = int(response.getheader("Content-Length") or 0)
                if length > max_bytes:
                    raise SourceError("response_too_large", f"响应超过 {max_bytes} 字节: {current}")
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise SourceError("response_too_large", f"响应超过 {max_bytes} 字节: {current}")
                return body
            except SourceError:
                raise
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                last = exc
            finally:
                if connection is not None:
                    connection.close()
        else:
            raise SourceError("fetch_failed", f"连接公开主机失败: {current}: {last}", retryable=True) from last
        # A redirect selected a new URL and broke the IP loop.
        continue
    raise SourceError("too_many_redirects", f"公开地址重定向次数过多: {url}")


def fetch_bytes(url: str, *, timeout: int = 30, max_bytes: int = MAX_BODY) -> bytes:
    last: Optional[Exception] = None
    for attempt in range(4):
        try:
            return _fetch_once(url, timeout=timeout, max_bytes=max_bytes)
        except SourceError as exc:
            last = exc
            if not exc.retryable:
                raise
        if attempt < 3:
            time.sleep(min(10.0, (2 ** attempt) + random.random()))
    raise SourceError("fetch_failed", f"读取公开地址重试耗尽: {url}: {last}", retryable=True) from last


def download_public_file(url: str, target: Path, *, timeout: int = 120, max_bytes: int = 2 * 1024 * 1024 * 1024) -> int:
    last: Optional[Exception] = None
    for attempt in range(4):
        current = url
        try:
            for _ in range(6):
                validate_public_url(current)
                parsed = urlparse(current)
                host = parsed.hostname or ""
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                redirect_to: Optional[str] = None
                for pinned_ip in _global_ips(host, port):
                    connection = None
                    try:
                        cls = _PinnedHTTPSConnection if parsed.scheme == "https" else _PinnedHTTPConnection
                        connection = cls(host, pinned_ip, port, timeout)
                        host_header = host if parsed.port is None else f"{host}:{port}"
                        connection.request("GET", path, headers={"Host": host_header, "User-Agent": USER_AGENT, "Accept": "audio/*, */*;q=0.1", "Connection": "close"})
                        peer_ip = ipaddress.ip_address(connection.sock.getpeername()[0])
                        if not peer_ip.is_global or peer_ip != ipaddress.ip_address(pinned_ip):
                            raise SourceError("unsafe_peer", f"实际连接地址未经验证: {peer_ip}")
                        response = connection.getresponse()
                        if response.status in {301, 302, 303, 307, 308}:
                            location = response.getheader("Location")
                            if not location:
                                raise SourceError("redirect_missing_location", f"重定向缺少 Location: {current}")
                            redirect_to = urljoin(current, location)
                            break
                        if response.status in {408, 425, 429} or response.status >= 500:
                            raise SourceError("download_http_retryable", f"音频返回 HTTP {response.status}", retryable=True)
                        if response.status < 200 or response.status >= 300:
                            raise SourceError("download_http_error", f"音频返回 HTTP {response.status}")
                        length = int(response.getheader("Content-Length") or 0)
                        if length > max_bytes:
                            raise SourceError("audio_too_large", f"音频超过 {max_bytes} 字节")
                        copied = 0
                        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                        if hasattr(os, "O_NOFOLLOW"):
                            flags |= os.O_NOFOLLOW
                        fd = os.open(target, flags, 0o600)
                        with os.fdopen(fd, "wb") as output:
                            while True:
                                chunk = response.read(1024 * 1024)
                                if not chunk:
                                    break
                                copied += len(chunk)
                                if copied > max_bytes:
                                    raise SourceError("audio_too_large", f"音频超过 {max_bytes} 字节")
                                output.write(chunk)
                            output.flush()
                            os.fsync(output.fileno())
                        return copied
                    except SourceError:
                        raise
                    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                        last = exc
                    finally:
                        if connection is not None:
                            connection.close()
                if redirect_to:
                    current = redirect_to
                    continue
                raise SourceError("download_failed", f"连接音频主机失败: {current}: {last}", retryable=True)
            raise SourceError("too_many_redirects", f"音频重定向次数过多: {url}")
        except SourceError as exc:
            target.unlink(missing_ok=True)
            last = exc
            if not exc.retryable:
                raise
        if attempt < 3:
            time.sleep(min(10.0, (2 ** attempt) + random.random()))
    raise SourceError("download_failed", f"音频下载重试耗尽: {last}", retryable=True) from last


def fetch_json(url: str) -> dict[str, Any]:
    try:
        value = json.loads(fetch_bytes(url).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceError("invalid_json", f"公开接口返回的不是有效 JSON: {url}") from exc
    if not isinstance(value, dict):
        raise SourceError("invalid_json", f"公开接口 JSON 顶层不是 object: {url}")
    return value


def text(value: Any) -> str:
    value = re.sub(r"<[^>]+>", " ", str(value or ""))
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\x1b]", "", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def norm(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", text(value).casefold())


def similarity(left: Any, right: Any) -> float:
    a, b = norm(left), norm(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def parse_date(value: Any) -> str:
    raw = text(value)
    if not raw:
        return ""
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
    return parsed.astimezone(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")


def duration_seconds(value: Any) -> int:
    raw = text(value)
    if not raw:
        return 0
    if raw.isdigit():
        return int(raw)
    parts = raw.split(":")
    if all(part.isdigit() for part in parts) and 1 <= len(parts) <= 3:
        nums = [int(part) for part in parts]
        if len(nums) == 3:
            return nums[0] * 3600 + nums[1] * 60 + nums[2]
        if len(nums) == 2:
            return nums[0] * 60 + nums[1]
    return 0


def media_fingerprint(audio_url: str) -> str:
    parts = urlsplit(audio_url)
    # Only retain query keys whose purpose is clearly media identity. Unknown
    # parameters are commonly cloud signatures, expirations, or tracking data
    # and must not make a new episode ID when they rotate.
    identity_keys = {"id", "file", "filename", "media", "media_id", "audio", "audio_id", "episode", "episode_id", "eid", "guid", "key", "object"}
    identity_query = sorted(
        (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() in identity_keys
    )
    query = urlencode(identity_query, doseq=True)
    return f"{parts.path or '/'}?{query}" if query else (parts.path or "/")


def stable_episode_id(guid: str, audio_url: str, title: str, published_at: str) -> str:
    if guid:
        seed = guid
    elif audio_url:
        fingerprint = media_fingerprint(audio_url)
        # A feed publisher may rotate signed queries, move the same file to a
        # different CDN host, correct its title, or fix its publication date.
        # Inside one podcast, the stable media path is the least volatile
        # public fingerprint. Root-only paths have no useful identity, so they
        # fall back to title and publication time.
        seed = f"{fingerprint}\n{title}\n{published_at}"
    else:
        seed = f"{title}\n{published_at}"
    return "rss-" + hashlib.sha256(seed.encode("utf-8", errors="replace")).hexdigest()[:24]


def _child_text(node: ET.Element, names: list[str]) -> str:
    for name in names:
        child = node.find(name)
        if child is not None and child.text:
            return child.text.strip()
    return ""


@dataclass
class FeedData:
    url: str
    title: str
    author: str
    description: str
    image_url: str
    episodes: list[dict[str, Any]]
    invalid_item_count: int = 0


def parse_feed(body: bytes, feed_url: str) -> FeedData:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise SourceError("rss_invalid_xml", f"RSS XML 无法解析: {exc}") from exc
    channel = root.find("channel")
    if channel is None:
        raise SourceError("rss_not_supported", "当前只支持包含 channel/item 的公开 Podcast RSS 2.0")
    itunes = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
    content = "{http://purl.org/rss/1.0/modules/content/}"
    image_url = _child_text(channel, [f"{itunes}image"])
    image_node = channel.find(f"{itunes}image")
    if image_node is not None:
        image_url = image_node.attrib.get("href", image_url)
    if not image_url:
        image_url = _child_text(channel, ["image/url"])
    episodes: list[dict[str, Any]] = []
    invalid_item_count = 0
    seen: dict[str, int] = {}
    collision_bases: set[str] = set()
    for item in channel.findall("item"):
        title_value = _child_text(item, ["title"])
        pub_value = parse_date(_child_text(item, ["pubDate", "{http://purl.org/dc/elements/1.1/}date"]))
        guid = _child_text(item, ["guid"])
        enclosure = item.find("enclosure")
        audio_url = enclosure.attrib.get("url", "").strip() if enclosure is not None else ""
        if not audio_url:
            media = item.find("{http://search.yahoo.com/mrss/}content")
            audio_url = media.attrib.get("url", "").strip() if media is not None else ""
        if not title_value or not pub_value or not audio_url:
            invalid_item_count += 1
            continue
        fingerprint = media_fingerprint(audio_url)
        eid = stable_episode_id(guid, audio_url, title_value, pub_value)
        base_eid = eid
        if base_eid in collision_bases and not guid:
            collision_seed = f"{base_eid}\n{fingerprint}\n{title_value}\n{pub_value}"
            eid = "rss-" + hashlib.sha256(collision_seed.encode("utf-8", errors="replace")).hexdigest()[:24]
        elif eid in seen:
            # A shared download endpoint can distinguish files only by query.
            # Re-key both the previous and current no-GUID items with their
            # own content attributes, making the result independent of RSS
            # order. A duplicate real GUID is invalid publisher data.
            previous_index = seen[eid]
            previous = episodes[previous_index]
            if guid or previous.get("rss_guid"):
                invalid_item_count += 1
                continue
            previous_seed = f"{base_eid}\n{previous.get('media_fingerprint')}\n{previous.get('title')}\n{previous.get('pub_date')}"
            previous_eid = "rss-" + hashlib.sha256(previous_seed.encode("utf-8", errors="replace")).hexdigest()[:24]
            previous["eid"] = previous_eid
            previous["episode_id"] = previous_eid
            del seen[base_eid]
            seen[previous_eid] = previous_index
            collision_bases.add(base_eid)
            collision_seed = f"{eid}\n{fingerprint}\n{title_value}\n{pub_value}"
            eid = "rss-" + hashlib.sha256(collision_seed.encode("utf-8", errors="replace")).hexdigest()[:24]
            if eid in seen:
                invalid_item_count += 1
                continue
        seen[eid] = len(episodes)
        description = _child_text(item, [f"{content}encoded", "description", f"{itunes}summary"])
        link = _child_text(item, ["link"])
        episodes.append({
            "eid": eid,
            "episode_id": eid,
            "rss_guid": guid,
            "title": text(title_value),
            "pub_date": pub_value,
            "published_at": pub_value,
            "duration_seconds": duration_seconds(_child_text(item, [f"{itunes}duration"])),
            "description": text(description),
            "shownotes_html": description,
            "audio_url": audio_url,
            "media_fingerprint": fingerprint,
            "xiaoyuzhou_url": link if "xiaoyuzhoufm.com/episode/" in link else "",
            "source": "rss",
        })
    episodes.sort(key=lambda ep: ep["pub_date"], reverse=True)
    return FeedData(
        url=feed_url,
        title=text(_child_text(channel, ["title"])),
        author=text(_child_text(channel, [f"{itunes}author", "managingEditor"])),
        description=text(_child_text(channel, [f"{itunes}summary", "description"])),
        image_url=image_url,
        episodes=episodes,
        invalid_item_count=invalid_item_count,
    )


def fetch_feed(feed_url: str) -> FeedData:
    return parse_feed(fetch_bytes(feed_url), feed_url)


def secure_feed_url(feed_url: str) -> str:
    """Return an HTTPS-only version of an advertised feed URL.

    Apple still exposes some old podcast feeds as HTTP even when the same
    endpoint supports HTTPS. We may try that exact transport upgrade, but we
    never fetch the clear-text URL or change its host/path/query.
    """
    parsed = urlsplit(str(feed_url or "").strip())
    if parsed.scheme == "https":
        return parsed.geturl()
    if parsed.scheme == "http" and parsed.hostname and not parsed.username and not parsed.password:
        return parsed._replace(scheme="https").geturl()
    return str(feed_url or "").strip()


def _deep_find_podcast(value: Any, pid: str) -> Optional[dict[str, Any]]:
    if isinstance(value, dict):
        if str(value.get("pid") or "") == pid and value.get("title"):
            return value
        for child in value.values():
            found = _deep_find_podcast(child, pid)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _deep_find_podcast(child, pid)
            if found:
                return found
    return None


def fetch_xiaoyuzhou_podcast(pid: str) -> dict[str, Any]:
    url = f"{XYZ_WEB}/podcast/{pid}"
    raw = fetch_bytes(url, max_bytes=16 * 1024 * 1024).decode("utf-8", errors="replace")
    match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', raw, re.S | re.I)
    candidates: list[Any] = []
    if match:
        try:
            candidates.append(json.loads(html.unescape(match.group(1))))
        except json.JSONDecodeError:
            pass
    for pattern in (r'"podcast"\s*:\s*(\{)', r'"pid"\s*:\s*"' + re.escape(pid) + r'"'):
        if re.search(pattern, raw):
            candidates.append(raw)
            break
    podcast = None
    for candidate in candidates:
        if isinstance(candidate, (dict, list)):
            podcast = _deep_find_podcast(candidate, pid)
            if podcast:
                break
    if not podcast:
        # The public page can still provide enough matching metadata through OG tags.
        def meta(prop: str) -> str:
            m = re.search(r'<meta[^>]+(?:property|name)=["\']' + re.escape(prop) + r'["\'][^>]+content=["\']([^"\']*)', raw, re.I)
            return html.unescape(m.group(1)).strip() if m else ""
        title_value = re.sub(r"\s*[|｜-]\s*小宇宙.*$", "", meta("og:title"))
        if not title_value:
            raise SourceError("xiaoyuzhou_public_parse_failed", "小宇宙公开页面已变化，无法取得播客信息", retryable=True)
        podcast = {"pid": pid, "title": title_value, "description": meta("og:description"), "image": {"picUrl": meta("og:image")}}
    episodes = podcast.get("episodes") if isinstance(podcast.get("episodes"), list) else []
    author = podcast.get("author") or podcast.get("podcaster") or podcast.get("nickname") or ""
    if isinstance(author, dict):
        author = author.get("nickname") or author.get("name") or ""
    podcasters = podcast.get("podcasters") if isinstance(podcast.get("podcasters"), list) else []
    named_podcasters = [text(item.get("nickname") or item.get("name")) for item in podcasters if isinstance(item, dict)]
    named_podcasters = [name for name in named_podcasters if name]
    if (not author or text(author) in {"佚名", "匿名"}) and named_podcasters:
        author = "、".join(named_podcasters)
    image_value = podcast.get("image") or podcast.get("imageUrl") or podcast.get("picUrl") or ""
    if isinstance(image_value, dict):
        image_value = image_value.get("picUrl") or image_value.get("url") or ""
    raw_count = podcast.get("episodeCount") if podcast.get("episodeCount") is not None else podcast.get("episode_count")
    return {
        "pid": pid,
        "title": text(podcast.get("title")),
        "author": text(author),
        "description": text(podcast.get("description") or podcast.get("brief")),
        "image_url": str(image_value or ""),
        "episode_count": int(raw_count or 0),
        "episode_count_verified": raw_count is not None,
        "recent_titles": [text(ep.get("title")) for ep in episodes if isinstance(ep, dict) and ep.get("title")],
        "recent_episodes_raw": episodes,
        "xiaoyuzhou_url": url,
    }


def _deep_find_episode(value: Any, eid: str) -> Optional[dict[str, Any]]:
    if isinstance(value, dict):
        if str(value.get("eid") or value.get("episodeId") or "") == eid and value.get("title"):
            return value
        for child in value.values():
            found = _deep_find_episode(child, eid)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _deep_find_episode(child, eid)
            if found:
                return found
    return None


def fetch_xiaoyuzhou_episode(eid: str) -> tuple[dict[str, Any], dict[str, Any]]:
    url = f"{XYZ_WEB}/episode/{eid}"
    raw = fetch_bytes(url, max_bytes=16 * 1024 * 1024).decode("utf-8", errors="replace")
    match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', raw, re.S | re.I)
    data: Any = None
    if match:
        try:
            data = json.loads(html.unescape(match.group(1)))
        except json.JSONDecodeError:
            pass
    episode = _deep_find_episode(data, eid) if data is not None else None
    if not episode:
        raise SourceError("xiaoyuzhou_public_parse_failed", "小宇宙公开单集页面已变化，无法取得单集信息", retryable=True)
    media = episode.get("media") or {}
    source = media.get("source") if isinstance(media, dict) else {}
    audio_url = ""
    if isinstance(source, dict):
        audio_url = str(source.get("url") or source.get("src") or "")
    audio_url = audio_url or str(episode.get("audioUrl") or episode.get("audio_url") or "")
    podcast_raw = episode.get("podcast") if isinstance(episode.get("podcast"), dict) else {}
    pid = str(episode.get("pid") or podcast_raw.get("pid") or "")
    title_value = text(episode.get("title"))
    published = parse_date(episode.get("pubDate") or episode.get("pub_date") or episode.get("publishedAt"))
    normalized = {
        "eid": eid,
        "episode_id": eid,
        "rss_guid": "",
        "pid": pid,
        "podcast_title": text(podcast_raw.get("title")),
        "title": title_value,
        "pub_date": published,
        "published_at": published,
        "duration_seconds": int(episode.get("duration") or episode.get("duration_seconds") or 0),
        "description": text(episode.get("shownotes") or episode.get("description")),
        "shownotes_html": episode.get("shownotes") or episode.get("description") or "",
        "audio_url": audio_url,
        "xiaoyuzhou_url": url,
        "source": "xiaoyuzhou_public",
    }
    if not normalized["audio_url"]:
        raise SourceError("audio_url_missing", "小宇宙公开单集页面没有公开音频地址")
    podcast = {
        "pid": pid or f"single-{eid}",
        "title": normalized["podcast_title"] or "single-episode",
        "xiaoyuzhou_url": f"{XYZ_WEB}/podcast/{pid}" if pid else "",
        "history_source": "xiaoyuzhou_public",
        "history_complete": False,
        "history_reason": "单集模式不判断播客历史完整性",
    }
    return podcast, normalized


def itunes_search(term: str, limit: int = 20) -> list[dict[str, Any]]:
    query = urlencode({"term": term, "media": "podcast", "entity": "podcast", "limit": min(max(limit, 1), 50), "country": "CN"})
    data = fetch_json(f"{ITUNES_SEARCH}?{query}")
    return [item for item in data.get("results", []) if isinstance(item, dict) and item.get("feedUrl")]


def _episode_overlap(left_titles: list[str], right_titles: list[str]) -> float:
    left = [norm(v) for v in left_titles if norm(v)]
    right = [norm(v) for v in right_titles if norm(v)]
    if not left:
        return 0.0
    matched = 0
    for title_value in left[:15]:
        if any(SequenceMatcher(None, title_value, other).ratio() >= 0.88 for other in right[:40]):
            matched += 1
    return matched / min(len(left), 15)


def _candidate_score(meta: dict[str, Any], candidate: dict[str, Any], feed: FeedData) -> dict[str, Any]:
    parts = {
        "title": similarity(meta.get("title"), candidate.get("collectionName") or feed.title),
        "author": similarity(meta.get("author"), candidate.get("artistName") or feed.author) if meta.get("author") else 0.0,
        "description": similarity(meta.get("description"), feed.description) if meta.get("description") else 0.0,
        "episode_overlap": _episode_overlap(meta.get("recent_titles") or [], [ep["title"] for ep in feed.episodes]),
    }
    available_weight = 0.5
    weighted = 0.5 * parts["title"]
    if meta.get("author"):
        weighted += 0.2 * parts["author"]
        available_weight += 0.2
    if meta.get("description"):
        weighted += 0.1 * parts["description"]
        available_weight += 0.1
    if meta.get("recent_titles"):
        weighted += 0.2 * parts["episode_overlap"]
        available_weight += 0.2
    score = weighted / available_weight if available_weight else 0.0
    return {"score": round(score, 4), "parts": parts}


def normalize_public_recent(meta: dict[str, Any]) -> list[dict[str, Any]]:
    episodes: list[dict[str, Any]] = []
    for item in meta.get("recent_episodes_raw") or []:
        if not isinstance(item, dict):
            continue
        eid = str(item.get("eid") or "")
        title_value = text(item.get("title"))
        published = parse_date(item.get("pubDate") or item.get("pub_date"))
        media = item.get("media") if isinstance(item.get("media"), dict) else {}
        source_obj = media.get("source") if isinstance(media.get("source"), dict) else {}
        enclosure = item.get("enclosure") if isinstance(item.get("enclosure"), dict) else {}
        audio_url = str(source_obj.get("url") or enclosure.get("url") or item.get("audioUrl") or "")
        if not eid or not title_value or not published or not audio_url:
            continue
        episodes.append({
            "eid": eid,
            "episode_id": eid,
            "rss_guid": "",
            "pid": meta.get("pid"),
            "podcast_title": meta.get("title"),
            "title": title_value,
            "pub_date": published,
            "published_at": published,
            "duration_seconds": int(item.get("duration") or item.get("duration_seconds") or 0),
            "description": text(item.get("description") or item.get("shownotes")),
            "shownotes_html": item.get("shownotes") or item.get("description") or "",
            "audio_url": audio_url,
            "media_fingerprint": media_fingerprint(audio_url),
            "xiaoyuzhou_url": f"{XYZ_WEB}/episode/{eid}",
            "source": "xiaoyuzhou_public",
        })
    episodes.sort(key=lambda ep: ep["pub_date"], reverse=True)
    return episodes


def _episode_date(value: Any) -> Optional[dt.date]:
    parsed = parse_date(value)
    if not parsed:
        return None
    try:
        return dt.datetime.fromisoformat(parsed).date()
    except ValueError:
        return None


def _episode_match_strength(left: dict[str, Any], right: dict[str, Any]) -> int:
    left_title, right_title = norm(left.get("title")), norm(right.get("title"))
    left_date, right_date = _episode_date(left.get("pub_date")), _episode_date(right.get("pub_date"))
    # Count-gap certification must be stricter than show-level fuzzy matching:
    # adjacent numbered episodes often differ by only one character.
    title_and_date = bool(left_title and left_title == right_title and left_date and left_date == right_date)
    if title_and_date:
        return 1
    return 0


def _same_episode(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return _episode_match_strength(left, right) > 0


def merge_verified_public_gap(
    feed_episodes: list[dict[str, Any]], meta: dict[str, Any], retained: Optional[list[dict[str, Any]]] = None
) -> tuple[list[dict[str, Any]], int, bool]:
    """Fill an exact, independently counted RSS gap from public recent items.

    A partial or ambiguous gap is left untouched. This prevents a similarly
    named public episode from silently making an incomplete RSS look complete.
    """
    current = normalize_public_recent(meta)
    candidates: list[dict[str, Any]] = []
    seen_eids: set[str] = set()
    for episode in [*current, *(retained or [])]:
        if not isinstance(episode, dict) or not episode.get("eid") or not episode.get("title") or not episode.get("pub_date") or not episode.get("audio_url"):
            continue
        eid = str(episode["eid"])
        if eid in seen_eids:
            continue
        seen_eids.add(eid)
        candidates.append(dict(episode))

    # Match as a one-to-one relationship. Media URLs are deliberately not
    # identity proof here because publishers may reuse a shared download path.
    # If two public items both resemble the same RSS item, neither is trusted
    # as matched; the exact-gap check
    # below then refuses to certify or silently fill the ambiguous catalog.
    preferred: list[list[int]] = []
    for public_episode in candidates:
        strengths = [_episode_match_strength(public_episode, episode) for episode in feed_episodes]
        maximum = max(strengths, default=0)
        preferred.append([index for index, strength in enumerate(strengths) if strength == maximum and maximum > 0])
    reverse_counts: dict[int, int] = {}
    for choices in preferred:
        for index in choices:
            reverse_counts[index] = reverse_counts.get(index, 0) + 1
    safely_matched = {
        index for index, choices in enumerate(preferred)
        if len(choices) == 1 and reverse_counts.get(choices[0]) == 1
    }
    missing = [episode for index, episode in enumerate(candidates) if index not in safely_matched]

    expected = int(meta.get("episode_count") or 0)
    deficit = expected - len(feed_episodes)
    exact_gap = bool(meta.get("episode_count_verified") and deficit > 0 and len(missing) == deficit)
    if exact_gap:
        supplements = missing
        gap_verified = True
    else:
        retained_eids = {str(ep.get("eid")) for ep in (retained or []) if isinstance(ep, dict) and ep.get("eid")}
        # Previously verified supplements are part of the durable source
        # inventory. Preserve them when they leave Xiaoyuzhou's recent window;
        # ambiguous evidence makes coverage incomplete, never silently complete.
        supplements = [ep for ep in missing if str(ep.get("eid")) in retained_eids]
        gap_verified = deficit <= 0 and not missing
    merged = [*feed_episodes, *supplements]
    merged.sort(key=lambda ep: ep["pub_date"], reverse=True)
    return merged, len(supplements), gap_verified


def _public_fallback(meta: dict[str, Any], reason: str) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    episodes = normalize_public_recent(meta)
    if not episodes:
        raise SourceError("rss_not_found", reason)
    expected = int(meta.get("episode_count") or 0)
    history_reason = f"{reason}；当前仅取得小宇宙公开页面最近 {len(episodes)} 期，无法确认完整历史"
    podcast = {
        "pid": meta.get("pid"),
        "title": meta.get("title"),
        "author": meta.get("author"),
        "description": meta.get("description"),
        "image_url": meta.get("image_url"),
        "xiaoyuzhou_url": meta.get("xiaoyuzhou_url"),
        "feed_url": "",
        "history_source": "xiaoyuzhou_public",
        "history_complete": False,
        "history_reason": history_reason,
        "expected_episode_count": expected,
    }
    coverage = {
        "history_source": "xiaoyuzhou_public",
        "history_complete": False,
        "history_reason": history_reason,
        "public_episode_count": len(episodes),
        "expected_episode_count": expected,
        "oldest_episode": episodes[-1]["pub_date"][:10],
        "newest_episode": episodes[0]["pub_date"][:10],
    }
    return podcast, episodes, coverage


def resolve_public_podcast(value: str) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    match = re.search(r"xiaoyuzhoufm\.com/podcast/([A-Za-z0-9_-]+)", value)
    xyz_meta: dict[str, Any]
    if match:
        xyz_meta = fetch_xiaoyuzhou_podcast(match.group(1))
    else:
        xyz_meta = {"pid": "", "title": text(value), "author": "", "description": "", "image_url": "", "episode_count": 0, "recent_titles": [], "xiaoyuzhou_url": ""}
    results = itunes_search(xyz_meta["title"], 20)
    if not results:
        if match:
            return _public_fallback(xyz_meta, "Apple Podcasts 没有找到对应 RSS")
        raise SourceError("rss_not_found", "Apple Podcasts 没有找到对应 RSS")
    ranked: list[dict[str, Any]] = []
    for candidate in sorted(results, key=lambda item: similarity(xyz_meta["title"], item.get("collectionName")), reverse=True)[:5]:
        try:
            feed = fetch_feed(secure_feed_url(str(candidate["feedUrl"])))
        except SourceError as exc:
            ranked.append({"candidate": candidate, "feed_error": exc.message, "score": 0.0})
            continue
        scored = _candidate_score(xyz_meta, candidate, feed)
        ranked.append({"candidate": candidate, "feed": feed, **scored})
    ranked.sort(key=lambda item: item.get("score", 0.0), reverse=True)
    viable = [item for item in ranked if item.get("feed")]
    if not viable:
        if match:
            return _public_fallback(xyz_meta, "Apple Podcasts 候选 RSS 均无法读取")
        raise SourceError("rss_not_found", "Apple Podcasts 没有找到可读取的对应 RSS")
    if not match:
        exact = [item for item in viable if similarity(xyz_meta["title"], item["candidate"].get("collectionName")) == 1.0]
        if len(exact) != 1:
            preview = [{
                "title": item["candidate"].get("collectionName"),
                "author": item["candidate"].get("artistName"),
                "feed_url": item["candidate"].get("feedUrl"),
            } for item in viable[:5]]
            raise SourceError("podcast_ambiguous", "仅凭名称无法高置信确认播客，请提供小宇宙 Podcast URL: " + json.dumps(preview, ensure_ascii=False))
        viable = exact
    best = viable[0]
    second_score = viable[1]["score"] if len(viable) > 1 else 0.0
    min_score = 0.78 if match else 0.92
    if best["score"] < min_score or (len(viable) > 1 and best["score"] - second_score < 0.08):
        preview = [{
            "title": item["candidate"].get("collectionName"),
            "author": item["candidate"].get("artistName"),
            "feed_url": item["candidate"].get("feedUrl"),
            "score": item.get("score", 0.0),
        } for item in viable[:5]]
        raise SourceError("podcast_ambiguous", "找到多个可能的 RSS，无法高置信确认: " + json.dumps(preview, ensure_ascii=False))
    candidate, feed = best["candidate"], best["feed"]
    expected = int(xyz_meta.get("episode_count") or 0)
    episodes, supplemented_count, gap_verified = merge_verified_public_gap(feed.episodes, xyz_meta)
    recent_overlap = _episode_overlap(xyz_meta.get("recent_titles") or [], [ep["title"] for ep in episodes])
    has_independent_evidence = bool(match and xyz_meta.get("episode_count_verified") and xyz_meta.get("recent_titles"))
    secure_feed = urlparse(feed.url).scheme == "https"
    complete = bool(
        has_independent_evidence
        and secure_feed
        and not feed.invalid_item_count
        and expected
        and len(episodes) >= expected
        and gap_verified
        and recent_overlap >= 0.6
    )
    if complete:
        supplement = f"，并从小宇宙公开页补齐 {supplemented_count} 期" if supplemented_count else ""
        count_result = f"合计 {len(episodes)} 期，与公开目录记录的 {expected} 期一致" if supplemented_count else f"不少于公开目录记录的 {expected} 期"
        reason = f"RSS 有 {len(feed.episodes)} 期{supplement}，{count_result}且匹配验证通过"
    elif not has_independent_evidence:
        reason = "缺少独立的小宇宙节目总数或最近节目证据，无法证明 RSS 覆盖完整历史"
    elif not secure_feed:
        reason = "RSS 使用未加密 HTTP，不能把可被篡改的传输标记为完整历史"
    elif feed.invalid_item_count:
        reason = f"RSS 有 {feed.invalid_item_count} 个无效条目，无法证明历史完整"
    elif len(episodes) < expected:
        reason = f"RSS 只有 {len(feed.episodes)} 期，少于公开目录记录的 {expected} 期，且无法从小宇宙公开页明确补齐"
    else:
        reason = "RSS 与小宇宙最近节目重合度不足，不能确认是完整对应历史"
    stable_pid = xyz_meta.get("pid") or f"apple-{candidate.get('collectionId')}"
    podcast = {
        "pid": stable_pid,
        "title": feed.title or candidate.get("collectionName") or xyz_meta.get("title"),
        "author": feed.author or candidate.get("artistName") or xyz_meta.get("author"),
        "description": feed.description or xyz_meta.get("description"),
        "image_url": feed.image_url or candidate.get("artworkUrl600") or xyz_meta.get("image_url"),
        "xiaoyuzhou_url": xyz_meta.get("xiaoyuzhou_url", ""),
        "apple_id": candidate.get("collectionId"),
        "feed_url": feed.url,
        "history_source": "rss+xiaoyuzhou_public" if supplemented_count else "rss",
        "history_complete": complete,
        "history_reason": reason,
        "match_confidence": best["score"],
        "recent_episode_overlap": round(recent_overlap, 4),
        "match_breakdown": best["parts"],
        "expected_episode_count": expected,
        "feed_invalid_item_count": feed.invalid_item_count,
        "public_supplement_episodes": [ep for ep in episodes if ep.get("source") == "xiaoyuzhou_public"],
    }
    for ep in episodes:
        ep["podcast_title"] = podcast["title"]
        ep["pid"] = stable_pid
    coverage = {
        "history_source": "rss+xiaoyuzhou_public" if supplemented_count else "rss",
        "history_complete": complete,
        "history_reason": reason,
        "rss_episode_count": len(feed.episodes),
        "public_supplement_count": supplemented_count,
        "merged_episode_count": len(episodes),
        "expected_episode_count": expected,
        "oldest_episode": episodes[-1]["pub_date"][:10] if episodes else None,
        "newest_episode": episodes[0]["pub_date"][:10] if episodes else None,
        "match_confidence": best["score"],
        "feed_invalid_item_count": feed.invalid_item_count,
    }
    return podcast, episodes, coverage


def refresh_rss_podcast(podcast: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    feed_url = str(podcast.get("feed_url") or "")
    if not feed_url:
        raise SourceError("feed_url_missing", "知识库没有保存 RSS 地址，无法增量更新")
    upgraded_feed_url = secure_feed_url(feed_url)
    feed = fetch_feed(upgraded_feed_url)
    # Persist the safe transport upgrade through the shared podcast object so
    # legacy checkpoints do not retry their old HTTP URL on every update.
    podcast["feed_url"] = feed.url
    xiaoyuzhou_url = str(podcast.get("xiaoyuzhou_url") or "")
    pid_match = re.search(r"xiaoyuzhoufm\.com/podcast/([A-Za-z0-9_-]+)", xiaoyuzhou_url)
    try:
        current_meta = fetch_xiaoyuzhou_podcast(pid_match.group(1)) if pid_match else {}
    except SourceError as exc:
        current_meta = {}
        evidence_error = exc.message
    else:
        evidence_error = ""
    expected = int(current_meta.get("episode_count") or 0)
    retained = podcast.get("public_supplement_episodes") if isinstance(podcast.get("public_supplement_episodes"), list) else []
    episodes, supplemented_count, gap_verified = merge_verified_public_gap(feed.episodes, current_meta, retained)
    podcast["public_supplement_episodes"] = [ep for ep in episodes if ep.get("source") == "xiaoyuzhou_public"]
    overlap = _episode_overlap(current_meta.get("recent_titles") or [], [ep["title"] for ep in episodes])
    has_evidence = bool(current_meta.get("episode_count_verified") and current_meta.get("recent_titles"))
    complete = bool(has_evidence and urlparse(feed.url).scheme == "https" and not feed.invalid_item_count and expected and len(episodes) >= expected and gap_verified and overlap >= 0.6)
    if evidence_error:
        reason = f"RSS 更新成功，但小宇宙独立完整性证据暂时不可用: {evidence_error}"
    elif complete:
        supplement = f"，并从小宇宙公开页补齐 {supplemented_count} 期" if supplemented_count else ""
        reason = f"RSS 有 {len(feed.episodes)} 期{supplement}，合计 {len(episodes)} 期，与当前小宇宙公开目录的 {expected} 期一致且最近节目吻合"
    elif not has_evidence:
        reason = "更新时缺少独立的小宇宙节目总数或最近节目证据"
    elif feed.invalid_item_count:
        reason = f"RSS 有 {feed.invalid_item_count} 个无效条目"
    elif len(episodes) < expected:
        reason = f"RSS 只有 {len(feed.episodes)} 期，少于当前小宇宙公开目录的 {expected} 期，且无法从公开页明确补齐"
    else:
        reason = "更新后的 RSS 与小宇宙最近节目重合度不足"
    for ep in episodes:
        ep["podcast_title"] = podcast.get("title") or feed.title
        ep["pid"] = podcast.get("pid")
    return episodes, {
        "history_source": "rss+xiaoyuzhou_public" if supplemented_count else "rss",
        "history_complete": complete,
        "history_reason": reason,
        "rss_episode_count": len(feed.episodes),
        "public_supplement_count": supplemented_count,
        "merged_episode_count": len(episodes),
        "expected_episode_count": expected,
        "oldest_episode": episodes[-1]["pub_date"][:10] if episodes else None,
        "newest_episode": episodes[0]["pub_date"][:10] if episodes else None,
        "match_confidence": round(overlap, 4),
        "feed_invalid_item_count": feed.invalid_item_count,
    }
