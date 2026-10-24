"""Basketball relevance filter for mixed-sport sources.

Regional broadcaster feeds and sitemaps carry every sport; this filter
decides, from title (and teaser when available), whether an item is worth
fetching. It errs toward false positives: a wrongly fetched article is a
wasted request, a wrongly skipped one is a missed story. Club, league and
aggregator sources bypass the filter entirely (always relevant).

The keyword list is a collection heuristic only; article-to-team tagging
happens later against the teams table aliases.
"""
from __future__ import annotations

import re

from .registry import Source

GENERAL = [
    "basketball", "bbl", "euroleague", "eurocup",
    "champions league basketball", "fiba",
]

# Distinctive club tokens; deliberately not bare city names, which are
# dominated by football in regional sport feeds.
CLUBS = [
    "fc bayern basketball", "alba", "albatrosse",
    "ratiopharm ulm", "bamberg baskets", "würzburg baskets", "wuerzburg baskets",
    "niners", "science city jena", "mhp riesen", "skyliners",
    "towers", "ewe baskets", "basketball löwen", "basketball loewen",
    "rasta vechta", "telekom baskets", "gladiators trier",
    "phoenix hagen", "seawolves", "syntainics", "mbc",
]

_PATTERN = re.compile(
    # closing \b so "alba" cannot match inside "Albanien" (observed
    # 2026-10-17); optional genitive-s keeps "Albas" matching. Inflected
    # club forms get their own tokens (e.g. "albatrosse") rather than
    # prefix matching.
    r"\b(" + "|".join(re.escape(k) for k in GENERAL + CLUBS) + r")s?\b",
    re.IGNORECASE,
)

ALWAYS_RELEVANT_TYPES = {"league", "club", "aggregator"}

# Ticket pre-sale and promo announcements read as team news but age badly on
# a website ("begins tomorrow at 10:00 AM"); filtered by title for every
# source type, clubs included (owner review problem 5, 2026-10-14). Only
# unambiguous sale/promo words - a bare "Ticket" can be legitimate news.
_PROMO = re.compile(
    r"\b(vorverkauf|dauerkarten?|ticketverkauf|ticketshop|gewinnspiel|verlosung)\b",
    re.IGNORECASE,
)


def is_promo(*texts: str | None) -> bool:
    return bool(_PROMO.search(" ".join(t for t in texts if t)))


def is_candidate(source: Source, *texts: str | None) -> bool:
    if source.type in ALWAYS_RELEVANT_TYPES:
        return True
    blob = " ".join(t for t in texts if t)
    if not blob:
        return True  # nothing to judge by; let the body decide later
    return bool(_PATTERN.search(blob))
