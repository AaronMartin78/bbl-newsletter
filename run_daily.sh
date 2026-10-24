#!/usr/bin/env bash
# Daily pipeline run on HUB: collect -> schedule -> cluster -> summarize ->
# render -> publish, then NAS backup and Uptime Kuma heartbeat.
# Called by CM4 over SSH after wake (deploy/cm4/bbl-newsletter-wake.sh),
# runnable by hand any time. Idempotent: a re-run resumes.
set -o pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"   # uv; non-interactive ssh skips .bashrc

LOG="data/run-$(date +%F).log"
exec > >(tee -a "$LOG") 2>&1
echo "=== bbl-newsletter daily run $(date -Is) ==="

# Shutdown protection comes from "bbl_newsletter" in HUB's auto-shutdown
# PROTECTED_PROCESSES (added 2026-10-24). systemd-inhibit was dropped: it is
# refused in non-interactive ssh sessions ("Failed to inhibit: Access
# denied"), which killed the first CM4-triggered run before the pipeline ran.
uv run python -m bbl_newsletter --config config.yaml run
status=$?

# One-line secret file, NOT in the vault (vault documents it as <push-url>).
KUMA_URL_FILE="$HOME/.config/bbl-newsletter/kuma-push-url"

if [ $status -eq 0 ]; then
    BACKUP_DIR=/mnt/data_lake/newsletters/bbl   # NAS CIFS mount on HUB
    if mountpoint -q /mnt/data_lake; then
        mkdir -p "$BACKUP_DIR"
        if rsync -a data/ "$BACKUP_DIR/data/" && rsync -a site/ "$BACKUP_DIR/site/"; then
            echo "NAS backup done: $BACKUP_DIR"
        else
            echo "BACKUP FAILED: rsync to $BACKUP_DIR"
            status=1
        fi
    else
        echo "WARN: /mnt/data_lake not mounted, NAS backup skipped"
    fi
    if [ -f "$KUMA_URL_FILE" ]; then
        curl -fsS "$(cat "$KUMA_URL_FILE")?status=up&msg=edition-ok" >/dev/null \
            || echo "WARN: Kuma heartbeat failed"
    fi
else
    echo "RUN FAILED with status $status"
    if [ -f "$KUMA_URL_FILE" ]; then
        curl -fsS "$(cat "$KUMA_URL_FILE")?status=down&msg=run-failed-$status" >/dev/null \
            || true
    fi
fi
exit $status
