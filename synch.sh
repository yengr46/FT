#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "== Pre-check =="
git status -sb

echo "== Pull latest =="
git pull --rebase origin main

echo "== Stage changes =="
git add -A

if git diff --cached --quiet; then
  echo "No changes to commit."
  exit 0
fi

msg="${1:-update}"
echo "== Commit =="
git commit -m "$msg"

echo "== Push =="
git push origin main

echo "Done."
git status -sb