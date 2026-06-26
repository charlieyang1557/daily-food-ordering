#!/usr/bin/env bash
# Install daily-food-ordering as an OpenClaw skill on this machine.
#   1. copies the skill into ~/.openclaw/workspace/skills/daily-food-ordering
#   2. registers the daily cron trigger (DISABLED) in ~/.openclaw/cron/jobs.json
# Idempotent: re-running re-syncs files and never double-registers the cron job.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DST="$HOME/.openclaw/workspace/skills/daily-food-ordering"
JOBS="$HOME/.openclaw/cron/jobs.json"

echo "==> Installing skill to $SKILL_DST"
mkdir -p "$SKILL_DST"
rsync -a --delete \
  --exclude '.git' --exclude '__pycache__' --exclude '.pytest_cache' \
  --exclude '.daily-food-ordering' --exclude '*.pyc' \
  "$REPO_DIR"/ "$SKILL_DST"/

echo "==> Registering daily cron trigger (disabled) in $JOBS"
python3 - "$REPO_DIR/references/cron-job.json" "$JOBS" <<'PY'
import json, os, shutil, sys
job_path, jobs_path = sys.argv[1], sys.argv[2]
with open(job_path) as f:
    job = json.load(f)
job.pop("_comment", None)
with open(jobs_path) as f:
    data = json.load(f)
if any(j.get("name") == "daily-food-ordering" for j in data.get("jobs", [])):
    print("    already registered — no change")
else:
    shutil.copy2(jobs_path, jobs_path + ".bak.pre-daily-food")
    data.setdefault("jobs", []).append(job)
    tmp = jobs_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, jobs_path)
    print("    registered (enabled:false)")
PY

echo "==> Done. Skill installed and cron registered (disabled)."
echo "    Dry-run:        cd \"$SKILL_DST\" && python3 run.py"
echo "    DoorDash login: cd \"$SKILL_DST\" && python3 run.py --provider doordash --login"
echo "    Enable cron:    set enabled:true on the daily-food-ordering job in $JOBS"
