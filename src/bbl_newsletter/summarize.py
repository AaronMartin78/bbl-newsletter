"""Summarization stage: story -> validated English summary.

The LLM is an injectable callable (prompt -> raw text), so the pipeline is
testable without a model and the model choice (Ollama on HUB, still to be
benchmarked) is a one-line swap. The model returns ONLY a headline and a
summary as JSON; it never produces URLs or citations - the renderer takes
those from the database, so a hallucinated link is structurally impossible.

A summary is stored with a deterministic id (story + input content hashes +
model + prompt version), making replays idempotent. The validator decides
status: 'candidate' when every check passes, 'rejected' otherwise; rejected
summaries keep their validation record for review and are never rendered.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import duckdb
from pydantic import BaseModel, ValidationError

from . import ids

PROMPT_VERSION = "v3"  # v3: person/team identity rules (owner review problems 1-2, 2026-10-14)
MIN_WORDS, MAX_WORDS = 30, 250
MAX_SOURCES_PER_STORY = 3   # CPU prefill dominates runtime
BODY_CHARS_PER_SOURCE = 2000

Generate = Callable[[str], str]


# ------------------------------------------------------------------ prompt
_PROMPT = """You are writing one item for a daily English-language newsletter
about German professional basketball (easyCredit BBL).

Below are {n} German-language source article(s) about the same {what}. Write:
1. "headline": a short English headline (at most 12 words).
2. "summary": one English paragraph of 60-180 words that combines the facts
   from ALL sources into a single composite account. Use only facts stated
   in the sources. Keep every name, number, score and date exactly as given.
   If sources disagree, say so and attribute each version to its outlet.
   Treat every named person as a separate individual: never combine facts
   about two different people into one person, and keep each player and
   coach with the team the sources give them. If you are not certain which
   person or team a fact belongs to, leave that fact out.
   Do not translate articles verbatim; write an original synthesis.
   No URLs, no markdown, no commentary about the sources themselves.

Respond with ONLY a JSON object: {{"headline": "...", "summary": "..."}}

{game_context}SOURCES:
{sources}"""


def build_prompt(
    members: list[tuple[str, Optional[str], str]],  # (publisher, title, body)
    game_line: Optional[str] = None,
) -> str:
    # keep the fullest reports; deterministic (length, then publisher)
    members = sorted(members, key=lambda m: (-len(m[2]), m[0]))[:MAX_SOURCES_PER_STORY]
    sources = "\n\n".join(
        f"[{i + 1}] {publisher} - {title or 'ohne Titel'}\n{body[:BODY_CHARS_PER_SOURCE]}"
        for i, (publisher, title, body) in enumerate(members)
    )
    return _PROMPT.format(
        n=len(members),
        what="game" if game_line else "topic",
        game_context=f"GAME: {game_line}\n\n" if game_line else "",
        sources=sources,
    )


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)

class Response(BaseModel):
    """The model's output contract; also the source of RESPONSE_SCHEMA, so
    the decoding constraint and the validation can never drift apart."""
    headline: str
    summary: str


# Passed as Ollama's `format` parameter: decoding is constrained to this
# shape, so unquoted values and missing braces (99/120 rejects in the
# 2026-10-17 run) cannot be emitted at all. parse_response stays as backstop.
RESPONSE_SCHEMA = Response.model_json_schema()


def _salvage(blob: str) -> dict:
    """Local models often emit almost-JSON: unquoted or unescaped summary
    value (observed live 2026-10-14), since v3 also without the closing
    brace (observed live 2026-10-17). Salvage the two fields directly."""
    head = re.search(r'"headline"\s*:\s*"([^"\n]+)"', blob)
    summ = re.search(r'"summary"\s*:\s*"?\s*(.+?)"?\s*\}?\s*$', blob, re.DOTALL)
    if not (head and summ):
        raise ValueError("JSON object malformed")
    return {"headline": head.group(1), "summary": summ.group(1)}


def parse_response(raw: str) -> tuple[str, str]:
    """Extract the JSON object from a model response. Tolerates markdown
    fencing, reasoning-model <think> blocks, and an unclosed object."""
    if "<think>" in raw and "</think>" not in raw:
        raise ValueError("unterminated <think> block (output likely truncated)")
    cleaned = _THINK.sub("", raw)
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            data = _salvage(match.group(0))
    elif "{" in cleaned:  # opened but never closed
        data = _salvage(cleaned[cleaned.index("{"):])
    else:
        raise ValueError("no JSON object in model response")
    try:
        parsed = Response.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"headline/summary missing or not strings: {exc}") from exc
    return parsed.headline.strip(), parsed.summary.strip()


# --------------------------------------------------------------- validation
_GERMAN_MARKERS = re.compile(
    r"\b(und|nicht|gegen|wurde|wurden|dass|beim|einen|eine[rm]?|spiel|sieg|niederlage)\b",
    re.IGNORECASE,
)

# A competition round the summary names must appear in the sources, in
# German or English: the model rendered Achtelfinale as "quarterfinals"
# and no numeric check could see it (owner's read, 2026-10-20). Bare
# "final" is skipped: too ambiguous ("final score", "finals").
_ROUND_TERMS = {
    "round of 16": ("achtelfinale", "round of 16"),
    "quarterfinal": ("viertelfinale", "quarterfinal"),
    "semifinal": ("halbfinale", "semifinal"),
}


def _rounds_ok(text: str, sources: str) -> bool:
    text = text.lower().replace("-", "")
    sources = sources.lower().replace("-", "")
    return all(
        any(marker in sources for marker in markers)
        for term, markers in _ROUND_TERMS.items()
        if term in text
    )


def validate(headline: str, summary: str, source_bodies: list[str]) -> dict:
    """Checks are conservative: fail on anything the renderer should not
    publish unreviewed. Returns {check: ok} plus a 'failed' list."""
    text = f"{headline} {summary}"
    joined_sources = " ".join(source_bodies)

    # digits in the sources, with German thousands dots collapsed
    source_numbers = set(re.findall(r"\d{2,}", joined_sources.replace(".", "")))
    source_numbers |= set(re.findall(r"\d{2,}", joined_sources))

    def traceable(n: str) -> bool:
        if n in source_numbers:
            return True
        # season shorthand: summary "2026/2027" vs source "2026/27"
        if len(n) == 4 and n[:2] in ("19", "20") and n[2:] in source_numbers:
            return True
        return False

    numbers_ok = all(traceable(n) for n in re.findall(r"\d{2,}", text))
    words = len(summary.split())
    length_ok = MIN_WORDS <= words <= MAX_WORDS
    english_ok = len(_GERMAN_MARKERS.findall(text)) <= 1
    no_urls_ok = "http" not in text.lower()
    # truncated model output can be valid JSON and pass every other check
    # (observed 2026-10-20: a summary ending "...described it as their")
    complete_ok = bool(re.search(r'[.!?]["\')\]]?\s*$', summary))

    checks = {
        "numbers_traceable": numbers_ok,
        "length": length_ok,
        "english": english_ok,
        "no_urls": no_urls_ok,
        "complete_sentence": complete_ok,
        "round_names": _rounds_ok(text, joined_sources),
    }
    return {**checks, "failed": sorted(k for k, ok in checks.items() if not ok)}


# ------------------------------------------------------------------ storage
@dataclass
class SummaryResult:
    summary_id: str
    status: str  # candidate | rejected | exists
    failed_checks: list[str]


def _story_members(con: duckdb.DuckDBPyConnection, story_id: str):
    """Latest revision per member article: (article_id, content_hash,
    publisher, title, body_path)."""
    return con.execute(
        """
        SELECT a.article_id, a.content_hash, s.publisher, a.title, a.body_path
        FROM story_articles sa
        JOIN articles a USING (article_id)
        JOIN sources s ON s.source_id = a.source_id
        WHERE sa.story_id = ?
        QUALIFY row_number() OVER (
            PARTITION BY a.article_id ORDER BY a.observed_at DESC, a.content_hash
        ) = 1
        ORDER BY a.article_id
        """,
        [story_id],
    ).fetchall()


def _game_line(con: duckdb.DuckDBPyConnection, story_id: str) -> Optional[str]:
    row = con.execute(
        """
        SELECT g.game_uid, th.name, tg.name, g.home_score, g.away_score,
               g.tipoff, g.status
        FROM stories st JOIN games g ON g.game_uid = st.game_uid
        JOIN teams th ON th.team_id = g.home_team_id
        JOIN teams tg ON tg.team_id = g.away_team_id
        WHERE st.story_id = ?
        """,
        [story_id],
    ).fetchone()
    if not row:
        return None
    _, home, guest, hs, gs, tipoff, status = row
    score = f" {hs}:{gs}" if hs is not None else ""
    return f"{home} vs {guest}{score} ({status}, tipoff {tipoff:%Y-%m-%d %H:%M} UTC)"


def summarize_story(
    con: duckdb.DuckDBPyConnection,
    story_id: str,
    generate: Generate,
    model: str,
    now: Optional[datetime] = None,
) -> SummaryResult:
    members = _story_members(con, story_id)
    if not members:
        raise ValueError(f"story has no members: {story_id}")
    input_h = ids.input_hash([m[1] for m in members])
    sid = ids.summary_id(story_id, input_h, model, PROMPT_VERSION)
    if con.execute("SELECT 1 FROM summaries WHERE summary_id = ?", [sid]).fetchone():
        return SummaryResult(sid, "exists", [])

    bodies = [
        Path(bp).read_text(encoding="utf-8") if bp and Path(bp).exists() else ""
        for *_fields, bp in members
    ]
    prompt = build_prompt(
        [(pub, title, body) for (_, _, pub, title, _), body in zip(members, bodies)],
        game_line=_game_line(con, story_id),
    )
    started = time.monotonic()
    raw = generate(prompt)
    elapsed = time.monotonic() - started

    try:
        headline, summary = parse_response(raw)
        checks = validate(headline, summary, bodies)
        status = "candidate" if not checks["failed"] else "rejected"
        text_en = f"{headline}\n\n{summary}"
    except (ValueError, json.JSONDecodeError) as exc:
        checks = {"failed": ["parse"], "parse_error": str(exc)}
        status = "rejected"
        text_en = raw[:2000]

    con.execute(
        "INSERT INTO summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            sid, story_id, input_h, model, PROMPT_VERSION,
            now or datetime.now(timezone.utc).replace(tzinfo=None),
            elapsed, text_en, json.dumps(checks), status,
        ],
    )
    return SummaryResult(sid, status, checks["failed"])


def summarize_pending(
    con: duckdb.DuckDBPyConnection,
    generate: Generate,
    model: str,
    now: Optional[datetime] = None,
    limit: Optional[int] = None,
    progress=None,
) -> list[SummaryResult]:
    """Summarize stories that have no summary for the current inputs yet.
    Game stories first (they are the newsletter's core), then multi-source
    stories, then the rest; `limit` caps the number of model calls."""
    results = []
    rows = con.execute(
        """
        SELECT s.story_id FROM stories s
        JOIN story_articles sa USING (story_id)
        GROUP BY s.story_id, s.kind, s.created_at
        ORDER BY (s.kind = 'game') DESC, s.created_at DESC,
                 count(*) DESC, s.story_id
        """
    ).fetchall()
    calls = 0
    for (story_id,) in rows:
        if limit is not None and calls >= limit:
            break
        try:
            result = summarize_story(con, story_id, generate, model, now=now)
        except Exception as exc:  # model/transport failure: skip, keep going
            result = SummaryResult(story_id, f"error:{type(exc).__name__}", [str(exc)[:200]])
            calls += 1
        else:
            if result.status != "exists":
                calls += 1
        results.append(result)
        if progress is not None:
            progress(result)
    return results
