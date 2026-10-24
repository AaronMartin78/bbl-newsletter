"""Identifier and hash conventions for the BBL newsletter pipeline.

These functions are the single implementation of the conventions documented
in schema.sql. Everything is deterministic: same input, same id, so every
pipeline stage is replay-safe.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that never identify content, only track the visitor.
_TRACKING_PARAMS = re.compile(
    r"^(utm_\w+|fbclid|gclid|msclkid|igshid|mc_cid|mc_eid|_requestid|ref|wt_mc)$",
    re.IGNORECASE,
)

_WS = re.compile(r"\s+")


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonicalize_url(url: str) -> str:
    """Normalize a URL so the same article always yields the same article_id.

    Rules (documented in schema.sql): https scheme, lowercase host, default
    ports stripped, fragment dropped, tracking parameters dropped, remaining
    query parameters sorted, trailing slash stripped except on the root path.
    """
    parts = urlsplit(url.strip())
    scheme = "https"
    host = parts.hostname.lower() if parts.hostname else ""
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _TRACKING_PARAMS.match(k)
        )
    )
    return urlunsplit((scheme, host, path, query, ""))


def article_id(url: str) -> str:
    """sha256 of the canonical URL."""
    return sha256_hex(canonicalize_url(url))


def content_hash(body_text: str) -> str:
    """sha256 of the extracted body text, whitespace-normalized.

    Whitespace runs collapse to one space and the text is stripped, so
    markup-only changes (reflowed paragraphs, trailing newlines) do not
    mint a new revision; any change to the words does.
    """
    return sha256_hex(_WS.sub(" ", body_text).strip())


def input_hash(content_hashes: list[str]) -> str:
    """sha256 over the sorted member content hashes of a story."""
    return sha256_hex("\n".join(sorted(content_hashes)))


def story_id(date_iso: str, article_ids: list[str]) -> str:
    """'<date>-<12 hex>' from the sorted member article ids."""
    return f"{date_iso}-{sha256_hex(chr(10).join(sorted(article_ids)))[:12]}"


def game_story_id(game_uid: str) -> str:
    """Stable story identity for a game: one story per game, whatever day
    its articles arrive, so a late recap joins the existing composite
    instead of starting a new story (observed 2026-10-21: the newscenter
    recap collected two days after tipoff split the game into items)."""
    return f"game-{game_uid}"


def summary_id(story: str, inputs: str, model: str, prompt_version: str) -> str:
    """Deterministic summary identity: same story, inputs, model and prompt
    version always produce the same id, so replays cannot duplicate summaries."""
    return sha256_hex("|".join((story, inputs, model, prompt_version)))


def game_uid(competition: str, official_game_id: str | int) -> str:
    """'<competition>:<official id>' in that competition's own id space,
    e.g. 'bbl:2006923'."""
    return f"{competition}:{official_game_id}"
