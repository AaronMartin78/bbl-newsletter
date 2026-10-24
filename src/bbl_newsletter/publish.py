"""Publisher stage: push a rendered edition into the website repository.

The pipeline owns a local clone of the website repo on HUB. Publishing
copies the day's .qmd into the clone's bbl/posts/ directory, commits on the
`editions` branch and pushes. GitHub Actions on the repo then validates and
(after approval) merges to main, which deploys. The editions table records
status 'pushed' and the commit hash as the receipt; re-publishing identical
content is a no-op, so retries never create duplicate commits.
"""
from __future__ import annotations

import shutil
import subprocess
from datetime import date
from pathlib import Path
from typing import Optional

import duckdb


class PublishError(RuntimeError):
    pass


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise PublishError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def publish_edition(
    con: duckdb.DuckDBPyConnection,
    edition_date: date,
    qmd_path: Path,
    repo_dir: Path,
    branch: str = "editions",
    dest_dir: str = "bbl/posts",
    remote: str = "origin",
    push: bool = True,
) -> dict:
    """Copy the edition into the website clone, commit on `branch`, push.

    Returns {status: pushed|unchanged, commit: sha|None}.
    """
    if not qmd_path.exists():
        raise PublishError(f"edition file missing: {qmd_path}")
    if not (repo_dir / ".git").exists():
        raise PublishError(f"not a git repository: {repo_dir}")

    # Build the branch on top of the remote state so retries and multiple
    # days stack cleanly. If the branch does not exist yet, start it from
    # the default branch's current state.
    _git(repo_dir, "fetch", remote)
    try:
        _git(repo_dir, "checkout", "-B", branch, f"{remote}/{branch}")
    except PublishError:
        _git(repo_dir, "checkout", "-B", branch)

    target = repo_dir / dest_dir / f"{edition_date.isoformat()}.qmd"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(qmd_path, target)

    _git(repo_dir, "add", str(target.relative_to(repo_dir)))
    status = _git(repo_dir, "status", "--porcelain")
    if not status:
        commit = _git(repo_dir, "rev-parse", "HEAD")
        return {"status": "unchanged", "commit": commit}

    _git(repo_dir, "commit", "-m", f"BBL Daily {edition_date.isoformat()} (automated)")
    commit = _git(repo_dir, "rev-parse", "HEAD")
    if push:
        _git(repo_dir, "push", remote, f"{branch}:{branch}")

    con.execute(
        "UPDATE editions SET status = 'pushed', git_commit = ? WHERE edition_date = ?",
        [commit, edition_date],
    )
    return {"status": "pushed", "commit": commit}
