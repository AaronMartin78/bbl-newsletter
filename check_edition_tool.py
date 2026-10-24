#!/usr/bin/env python3
"""CI validation for BBL edition files (bbl/posts/*.qmd). Stdlib only.

Checks per file:
  frontmatter   - title, date, lang: en present; date matches the filename
  structure     - at least one section (##) and one story (###)
  citations     - every story ends with a Sources list of absolute https links
  language      - body is English (German function-word heuristic)
  no-model-urls - story paragraphs contain no raw URLs (links live in Sources)
  links (--check-links) - every cited URL answers (HEAD, 10s timeout, one
                  retry). Only 404/410 fail the check: a 5xx, timeout or
                  refused connection is the source having a bad minute, not
                  a dead link (2026-10-22: a newscenter 503 plus seawolves.de
                  being down blocked a valid publish), so those print WARN
                  and do not block.

Exit code 0 = all files pass; 1 = failures (printed, one per line).
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

GERMAN = re.compile(
    r"\b(und|nicht|gegen|wurde|dass|beim|spiel|sieg|niederlage)\b", re.IGNORECASE
)
LINK = re.compile(r"\]\((https?://[^)\s]+)\)")
BARE_URL = re.compile(r"(?<!\()https?://\S+")


def link_status(url: str) -> tuple[str, str]:
    """('ok' | 'dead' | 'warn', detail). Dead means the resource is gone
    (404/410); every other failure is judged transient after one retry."""
    request = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "edition-check/1"})
    detail = ""
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                return "ok", f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            detail = f"HTTP {exc.code}"
            if exc.code in (404, 410):
                return "dead", detail
        except Exception as exc:
            detail = str(exc) or type(exc).__name__
        if attempt == 1:
            time.sleep(3)
    return "warn", detail


def check_file(path: Path, check_links: bool) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    text = path.read_text(encoding="utf-8")

    front = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    if not front:
        return [f"{path}: no frontmatter"], warnings
    fm = front.group(1)
    for key in ("title:", "date:", "lang: en"):
        if key not in fm:
            errors.append(f"{path}: frontmatter missing {key.rstrip(':')}")
    date_match = re.search(r"date:\s*(\S+)", fm)
    if date_match and date_match.group(1) != path.stem:
        errors.append(f"{path}: frontmatter date {date_match.group(1)} != filename")

    body = text[front.end():]
    if "## " not in body:
        errors.append(f"{path}: no sections")

    stories = re.split(r"\n### ", body)[1:]
    if not stories:
        errors.append(f"{path}: no stories")
    for story in stories:
        headline = story.splitlines()[0].strip()
        if "Sources:" not in story:
            errors.append(f"{path}: story '{headline[:40]}' has no Sources list")
            continue
        prose, _, sources = story.partition("Sources:")
        links = LINK.findall(sources)
        if not links:
            errors.append(f"{path}: story '{headline[:40]}' cites no links")
        if any(not u.startswith("https://") for u in links):
            errors.append(f"{path}: story '{headline[:40]}' has non-https citation")
        if BARE_URL.search(prose) or LINK.search(prose):
            errors.append(f"{path}: story '{headline[:40]}' has URLs in prose")
        if len(GERMAN.findall(prose)) > 2:
            errors.append(f"{path}: story '{headline[:40]}' reads as German")
        if check_links:
            for url in links:
                status, detail = link_status(url)
                if status == "dead":
                    errors.append(f"{path}: dead link {url} ({detail})")
                elif status == "warn":
                    warnings.append(
                        f"{path}: link unreachable right now {url} ({detail})")
    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--check-links", action="store_true")
    args = parser.parse_args()

    all_errors: list[str] = []
    all_warnings: list[str] = []
    for path in args.files:
        if path.suffix == ".qmd":
            errors, warnings = check_file(path, args.check_links)
            all_errors.extend(errors)
            all_warnings.extend(warnings)
    for warning in all_warnings:
        print(f"WARN {warning}")
    for error in all_errors:
        print(f"FAIL {error}")
    if not all_errors:
        print(f"OK {len(args.files)} file(s)")
    return 1 if all_errors else 0


if __name__ == "__main__":
    sys.exit(main())
