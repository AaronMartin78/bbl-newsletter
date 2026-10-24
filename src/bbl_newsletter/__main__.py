"""Command-line runner for the pipeline.

    python -m bbl_newsletter --config config.yaml init
    python -m bbl_newsletter --config config.yaml collect
    python -m bbl_newsletter --config config.yaml schedule
    python -m bbl_newsletter --config config.yaml cluster   [--date YYYY-MM-DD]
    python -m bbl_newsletter --config config.yaml summarize [--date YYYY-MM-DD]
    python -m bbl_newsletter --config config.yaml render    [--date YYYY-MM-DD]
    python -m bbl_newsletter --config config.yaml run       [--date YYYY-MM-DD]

`run` executes collect -> schedule -> cluster -> summarize -> render for the
given date (default: today in Europe/Berlin). Every stage is idempotent, so
a failed run is repeated safely with the same command.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from . import cluster as cluster_mod
from . import db
from .collect import collect_all
from .fetch import Fetcher
from .registry import Registry
from .render import render_edition
from .schedule import collect_schedule, collect_schedule_api
from .summarize import RESPONSE_SCHEMA, summarize_pending

BERLIN = ZoneInfo("Europe/Berlin")


def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    for key in ("registry", "schema", "db", "data_dir", "site_dir"):
        if key not in cfg:
            raise SystemExit(f"config missing required key: {key}")
    return cfg


def make_fetcher(registry: Registry) -> Fetcher:
    d = registry.defaults
    return Fetcher(
        user_agent=d.user_agent,
        timeout_s=d.request_timeout_s,
        obey_robots=d.obey_robots,
    )


def make_generator(cfg: dict, model_override: str | None = None):
    """Ollama text generation via its local HTTP API. Imported lazily so the
    offline stages never need a model server."""
    import httpx

    ollama = cfg.get("ollama", {})
    host = ollama.get("host", "http://127.0.0.1:11434")
    # no default: the model choice is an explicit decision
    model = model_override or ollama["model"]
    options = ollama.get("options", {"temperature": 0.2})

    payload_extra = {}
    if "think" in ollama:  # reasoning models: false skips the think monologue
        payload_extra["think"] = ollama["think"]

    def generate(prompt: str) -> str:
        resp = httpx.post(
            f"{host}/api/generate",
            # format constrains decoding to RESPONSE_SCHEMA, so the model
            # cannot emit malformed JSON (the 2026-10-17 failure class)
            json={"model": model, "prompt": prompt, "stream": False,
                  "format": RESPONSE_SCHEMA,
                  "options": options, **payload_extra},
            timeout=ollama.get("timeout_s", 300),
        )
        resp.raise_for_status()
        return resp.json()["response"]

    return generate, model


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bbl_newsletter")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--date", type=date.fromisoformat, default=None)
    parser.add_argument("--limit", type=int, default=None,
                        help="max model calls for summarize")
    parser.add_argument("--model", default=None,
                        help="override ollama.model from config for this run")
    parser.add_argument("--url", action="append", default=None,
                        help="article URL for exclude (repeatable)")
    parser.add_argument("--reason", default=None,
                        help="reason recorded with exclude")
    parser.add_argument(
        "command",
        choices=["init", "collect", "schedule", "cluster", "summarize",
                 "render", "publish", "cleanup", "exclude", "run"],
    )
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    base = args.config.parent
    registry = Registry.load(base / cfg["registry"])
    edition_date = args.date or datetime.now(BERLIN).date()

    db_path = base / cfg["db"]
    db_path.parent.mkdir(parents=True, exist_ok=True)
    (base / cfg["data_dir"]).mkdir(parents=True, exist_ok=True)
    con = db.connect(db_path)
    db.init_schema(con, base / cfg["schema"])
    db.sync_sources(con, registry)
    out: dict = {"command": args.command, "date": edition_date.isoformat()}

    if args.command == "init":
        out["sources"] = len(registry.sources)
        out["usable"] = sorted(s.id for s in registry.usable())

    if args.command in ("collect", "run"):
        fetcher = make_fetcher(registry)
        print(f"collecting {len(registry.usable())} sources...", file=sys.stderr, flush=True)
        run_id, reports = collect_all(
            con, registry, fetcher, base / cfg["data_dir"],
            progress=lambda r: print(
                f"  {r.source_id}: {r.status}, {r.items_new} new"
                + (f", {len(r.errors)} errors" if r.errors else ""),
                file=sys.stderr, flush=True,
            ),
        )
        out["collect"] = {
            "run_id": run_id,
            "sources": {r.source_id: {"status": r.status, "new": r.items_new,
                                      "errors": r.errors}
                        for r in reports},
        }
        fetcher.close()

    if args.command in ("schedule", "run"):
        fetcher = make_fetcher(registry)
        listing = cfg.get(
            "schedule_listing", "https://www.easycredit-bbl.de/spielplan"
        )
        # API first: the only source that reliably carries cup games. The
        # page walk stays for score enrichment and as fallback when the API
        # errors (its scores can be null even on final games).
        out["schedule_api"] = collect_schedule_api(con, fetcher, listing)
        out["schedule"] = collect_schedule(con, fetcher, listing)
        fetcher.close()

    if args.command in ("cluster", "run"):
        out["cluster"] = {
            k: (v if not isinstance(v, list) else len(v))
            for k, v in cluster_mod.cluster(con, edition_date).items()
        }

    if args.command in ("summarize", "run"):
        generate, model = make_generator(cfg, model_override=args.model)
        print(f"summarizing with {model} (limit {args.limit})...",
              file=sys.stderr, flush=True)
        results = summarize_pending(con, generate, model, limit=args.limit,
                                    progress=lambda r: print(
                                        f"  {r.summary_id[:12] or r.status}: {r.status}",
                                        file=sys.stderr, flush=True))
        out["summarize"] = {
            "model": model,
            "candidate": sum(r.status == "candidate" for r in results),
            "rejected": sum(r.status == "rejected" for r in results),
            "exists": sum(r.status == "exists" for r in results),
        }

    if args.command in ("render", "run"):
        path = render_edition(con, edition_date, base / cfg["site_dir"])
        out["render"] = str(path) if path else None

    if args.command in ("publish", "run"):
        from .publish import publish_edition
        qmd = base / cfg["site_dir"] / f"{edition_date.isoformat()}.qmd"
        if "website_repo" not in cfg:
            out["publish"] = "skipped: website_repo not configured"
        elif not Path(cfg["website_repo"]).exists():
            out["publish"] = "skipped: website clone not set up yet (see website/README.md)"
        elif not qmd.exists():
            out["publish"] = "skipped: no edition file for this date"
        else:
            from .publish import PublishError
            try:
                out["publish"] = publish_edition(
                    con, edition_date, qmd, Path(cfg["website_repo"]),
                    branch=cfg.get("website_branch", "editions"),
                )
            except PublishError as exc:
                out["publish"] = f"failed: {exc}"

    if args.command == "exclude":
        from .maintenance import exclude_articles
        if not args.url:
            raise SystemExit("exclude needs at least one --url")
        out["exclude"] = exclude_articles(con, args.url, reason=args.reason)

    if args.command == "cleanup":
        from .maintenance import cleanup_listing_articles, retag_articles
        out["cleanup"] = cleanup_listing_articles(con, registry)
        out["retag"] = retag_articles(con)

    con.close()
    print(json.dumps(out, indent=2, default=str))
    # A failed publish must fail the command (and so the daily run and the
    # Kuma heartbeat); "skipped: ..." states stay successful by design.
    if str(out.get("publish", "")).startswith("failed:"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
