"""Article body extraction.

trafilatura separates article text from navigation, ads and comments and is
the strongest off-the-shelf extractor for German news sites. A feed-supplied
body (WordPress full-content feeds) is preferred when it is substantial,
saving the article fetch entirely.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import trafilatura

MIN_BODY_CHARS = 300  # shorter than this is a teaser, not a body

_TAGS = re.compile(r"<[^>]+>")

# htmldate (inside trafilatura): prefer the original publication date over a
# modified date, and fall back to URL/body scanning - the club CMSes carry no
# feed dates at all, so the page is the only date source (observed 2026-10-18:
# published_at was NULL for 100% of club articles, flooding editions with
# months-old backlog).
_DATE_PARAMS = {"extensive_search": True, "original_date": True}

# Plausibility bounds for a NEWS publish date: the body-text fallback
# otherwise latches onto any date on the page - observed live 2026-10-18 on
# rasta-vechta.de, where signing announcements came back dated with the
# player's BIRTH date (1996-05-02 etc.). Out-of-bounds candidates are
# rejected inside htmldate, which then still finds a plausible visible date
# when the page has one, and returns none (-> stale) when it does not.
MAX_AGE_DAYS = 365
MAX_FUTURE_DAYS = 2


def _date_params(now: Optional[datetime] = None) -> dict:
    now = now or datetime.now()
    return {
        **_DATE_PARAMS,
        "min_date": now - timedelta(days=MAX_AGE_DAYS),
        "max_date": now + timedelta(days=MAX_FUTURE_DAYS),
    }


@dataclass
class Extracted:
    title: Optional[str]
    text: str
    published_at: Optional[datetime] = None  # naive, date precision


def from_html(
    html: str | bytes,
    url: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[Extracted]:
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    doc = trafilatura.bare_extraction(
        html, url=url, include_comments=False, include_tables=False,
        with_metadata=True, date_extraction_params=_date_params(now),
    )
    if doc is None or not doc.text or len(doc.text) < MIN_BODY_CHARS:
        return None
    published = datetime.fromisoformat(doc.date) if doc.date else None
    return Extracted(title=doc.title, text=doc.text, published_at=published)


def from_feed_body(value: str) -> Optional[str]:
    """Strip markup from a feed-supplied body; accept it only if substantial."""
    text = _TAGS.sub(" ", value)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) >= MIN_BODY_CHARS else None
