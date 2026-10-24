"""Discovery: turn a source's entry point into a bounded list of article URLs.

Three methods, matching the registry:
  rss          - feedparser over the feed; WordPress feeds may carry full bodies
  sitemap      - Google news sitemap (urlset or sitemapindex, one level deep)
  html_listing - server-rendered listing page; links matched by pattern

Discovery never fetches article pages; that is collect.py's job.
"""
from __future__ import annotations

import calendar
import html as html_mod
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

import feedparser

from .fetch import Fetcher, FetchResult
from .registry import Source

_HREF = re.compile(r"""<a\s[^>]*?href=["']([^"']+)["']""", re.IGNORECASE)
# listing-family pages that must never be stored as articles: the listing
# itself, its pagination, and index/archive variants (observed live
# 2026-10-13: bonn newsarchiv?currentPage=, oldenburg news/index.html,
# wuerzburg seite?tx_news_pi1)
_PAGINATION_QUERY = re.compile(r"(currentPage|tx_news|[?&]page=|[?&]seite=)", re.IGNORECASE)
_LISTING_PATH = re.compile(r"(/index\.html$|/(news|newsarchiv|archiv)/?$|/seite$|/seite/\d+/?$)", re.IGNORECASE)
_SITEMAP_NS = {
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "news": "http://www.google.com/schemas/sitemap-news/0.9",
}


@dataclass
class Item:
    url: str
    title: Optional[str] = None
    published_at: Optional[datetime] = None  # naive UTC
    feed_body: Optional[str] = None  # full text when the feed carries it


@dataclass
class Discovery:
    items: list[Item] = field(default_factory=list)
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    not_modified: bool = False
    status: str = "ok"  # ok | not_modified | robots_denied | http_<code> | error:<msg>


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utc_naive(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        return _utc_naive(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
    except ValueError:
        return None


def _status_of(result: FetchResult) -> str:
    if result.robots_denied:
        return "robots_denied"
    if result.not_modified:
        return "not_modified"
    if result.status == 200:
        return "ok"
    return result.error or f"http_{result.status}"


def discover(
    source: Source,
    fetcher: Fetcher,
    etag: Optional[str] = None,
    last_modified: Optional[str] = None,
    days_back: int = 3,
    now: Optional[datetime] = None,
) -> Discovery:
    entry = source.entry_point
    if not entry:
        return Discovery(status="error:no_entry_point")
    result = fetcher.get(entry, etag=etag, last_modified=last_modified)
    status = _status_of(result)
    if status != "ok":
        return Discovery(
            status=status,
            not_modified=result.not_modified,
            etag=result.etag,
            last_modified=result.last_modified,
        )

    if source.method == "rss":
        items = _from_rss(result.content)
    elif source.method == "sitemap":
        items = _from_sitemap(result.content, fetcher, days_back, now)
    elif source.method == "html_listing":
        items = _from_listing(result.content, entry, source)
    else:
        return Discovery(status=f"error:unsupported_method:{source.method}")

    return Discovery(
        items=items,
        etag=result.etag,
        last_modified=result.last_modified,
        status="ok",
    )


def _from_rss(content: bytes) -> list[Item]:
    parsed = feedparser.parse(content)
    items: list[Item] = []
    for entry in parsed.entries:
        link = getattr(entry, "link", None)
        if not link:
            continue
        published = None
        for attr in ("published_parsed", "updated_parsed"):
            t = getattr(entry, attr, None)
            if t:  # feedparser normalizes struct_time to UTC
                published = datetime.fromtimestamp(
                    calendar.timegm(t), tz=timezone.utc
                ).replace(tzinfo=None)
                break
        body = None
        if getattr(entry, "content", None):
            body = max((c.get("value", "") for c in entry.content), key=len) or None
        items.append(
            Item(
                url=link,
                title=getattr(entry, "title", None),
                published_at=published,
                feed_body=body,
            )
        )
    return items


def _from_sitemap(
    content: bytes,
    fetcher: Fetcher,
    days_back: int,
    now: Optional[datetime],
    _depth: int = 0,
) -> list[Item]:
    cutoff = (now or _utcnow()) - timedelta(days=days_back)
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return []

    tag = root.tag.split("}")[-1]
    items: list[Item] = []

    if tag == "sitemapindex" and _depth == 0:
        # One level deep, newest sub-sitemaps first, bounded.
        locs = [e.text.strip() for e in root.findall("sm:sitemap/sm:loc", _SITEMAP_NS) if e.text]
        for loc in locs[:3]:
            sub = fetcher.get(loc)
            if sub.status == 200 and sub.content:
                items.extend(_from_sitemap(sub.content, fetcher, days_back, now, _depth=1))
        return items

    for url_el in root.findall("sm:url", _SITEMAP_NS):
        loc = url_el.findtext("sm:loc", default="", namespaces=_SITEMAP_NS).strip()
        if not loc:
            continue
        title = url_el.findtext("news:news/news:title", default=None, namespaces=_SITEMAP_NS)
        date_text = url_el.findtext(
            "news:news/news:publication_date", default=None, namespaces=_SITEMAP_NS
        ) or url_el.findtext("sm:lastmod", default=None, namespaces=_SITEMAP_NS)
        published = _parse_iso(date_text) if date_text else None
        if published is not None and published < cutoff:
            continue
        items.append(Item(url=loc, title=title, published_at=published))
    return items


def _from_listing(content: bytes, base_url: str, source: Source) -> list[Item]:
    html = content.decode("utf-8", errors="replace")
    pattern = re.compile(source.article_url_pattern) if source.article_url_pattern else None
    seen: set[str] = set()
    items: list[Item] = []
    entry_path = urlsplit(base_url).path.rstrip("/")
    for href in _HREF.findall(html):
        url = urljoin(base_url, html_mod.unescape(href))
        if pattern:
            if not pattern.search(url):
                continue
        else:
            if not url.startswith(base_url.rstrip("/")):
                continue
            parts = urlsplit(url)
            path = parts.path.rstrip("/")
            if (path == entry_path                      # the listing itself
                    or _PAGINATION_QUERY.search(parts.query or "")
                    or _LISTING_PATH.search(parts.path)):
                continue
        if url in seen:
            continue
        seen.add(url)
        items.append(Item(url=url))
    return items
