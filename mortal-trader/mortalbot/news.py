"""News: recent headlines from public RSS/Atom feeds (stdlib only, no API key).

Headlines are cheap to send to Claude. Claude can also run its own web
searches when a headline needs digging into (see brain.py).

Everything here is untrusted third-party text: it is sanitised, truncated,
and handed to Claude as data. Code-level guardrails, not the prompt, are what
stop a malicious headline from causing harm.
"""

from __future__ import annotations

import html
import logging
import re
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

log = logging.getLogger("mortalbot.news")
MAX_BYTES = 2_000_000

Fetcher = Callable[[str], str]


@dataclass(frozen=True)
class Headline:
    source: str
    title: str
    summary: str
    published_ms: int
    link: str


def http_fetch(url: str, timeout: float = 15) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "mortalbot/0.2 (+rss reader)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(MAX_BYTES).decode("utf-8", errors="replace")


def _clean(text: str | None, limit: int) -> str:
    if not text:
        return ""
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _parse_time(text: str | None) -> int:
    if not text:
        return 0
    text = text.strip()
    try:
        dt = parsedate_to_datetime(text)  # RSS: RFC 822
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))  # Atom: ISO 8601
        except ValueError:
            return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class UnsafeFeed(ValueError):
    pass


def parse_feed(xml_text: str, source: str) -> list[Headline]:
    # RSS and Atom never need a DTD. Refusing DOCTYPE/ENTITY declarations outright
    # rules out entity-expansion ("billion laughs") and external-entity (XXE) attacks.
    upper = xml_text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise UnsafeFeed(f"{source}: feed declares a DOCTYPE/ENTITY; refusing to parse it")
    root = ET.fromstring(xml_text)
    out = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        fields: dict[str, str] = {}
        for child in el:
            name = _local(child.tag)
            if name == "link" and child.get("href"):
                fields.setdefault("link", child.get("href", ""))
            elif name not in fields:
                text = "".join(child.itertext())  # includes text nested in inline markup
                if text.strip():
                    fields[name] = text
        title = _clean(fields.get("title"), 200)
        if not title:
            continue
        summary = _clean(fields.get("description") or fields.get("summary"), 280)
        when = _parse_time(fields.get("pubDate") or fields.get("published") or fields.get("updated"))
        out.append(Headline(source, title, summary, when, (fields.get("link") or "").strip()))
    return out


def fetch_headlines(feeds: Sequence[str], *, now_ms: int, max_items: int = 25, max_age_hours: int = 48,
                    fetch: Fetcher = http_fetch) -> list[Headline]:
    seen: set[str] = set()
    items: list[Headline] = []
    for url in feeds:
        source = urlparse(url).netloc.removeprefix("www.")
        try:
            items += parse_feed(fetch(url), source)
        except Exception as e:  # one broken feed must never blind the bot
            log.warning("news feed %s failed: %s", source, e)
    cutoff = now_ms - max_age_hours * 3_600_000
    fresh = []
    for h in sorted(items, key=lambda h: h.published_ms, reverse=True):
        key = re.sub(r"\W+", "", h.title.lower())
        if h.published_ms and h.published_ms < cutoff or key in seen:
            continue
        seen.add(key)
        fresh.append(h)
    return fresh[:max_items]


def render(headlines: Sequence[Headline], now_ms: int) -> str:
    if not headlines:
        return "(no headlines available right now)"
    lines = []
    for h in headlines:
        age = f"{(now_ms - h.published_ms) / 3_600_000:.0f}h ago" if h.published_ms else "time unknown"
        line = f"- [{h.source}, {age}] {h.title}"
        if h.summary and h.summary.lower() != h.title.lower():
            line += f" -- {h.summary}"
        lines.append(line)
    return "\n".join(lines)
