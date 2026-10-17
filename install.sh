#!/usr/bin/env bash
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="${VIX_DEST:-$HOME/Sites/vix}"
REPO="${VIX_REPO:-merolaagi/vix}"
VERSION="$(cat "$SRC/VERSION")"
mkdir -p "$DEST"
if [ -d "$DEST/.git" ]; then
  (cd "$SRC" && find . -type f ! -path "./.git/*" | sed 's|^\./||' | sort) > /tmp/vix-release-files.$$
  (cd "$DEST" && git ls-files) | while read -r f; do
    if ! grep -qxF "$f" /tmp/vix-release-files.$$; then
      (cd "$DEST" && git rm -q --cached -- "$f" && rm -f -- "$f") && echo "Removed (no longer in release): $f"
    fi
  done
  rm -f /tmp/vix-release-files.$$
fi
(cd "$SRC" && tar --exclude ./.venv --exclude ./data --exclude ./logs --exclude ./.env --exclude ./.git -cf - .) | (cd "$DEST" && tar -xf -)
cd "$DEST"
chmod +x serve.sh install.sh
if [ ! -d .venv ]; then
  PY=$(command -v python3.12 || command -v python3.13 || command -v python3.11 || command -v python3)
  "$PY" -m venv .venv
fi
echo "Installing Python packages ..."
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
.venv/bin/pip install -q tableauhyperapi || echo "Note: Tableau Hyper API not installed; BI exports will be CSV only."
.venv/bin/pip install -q napalm || echo "Note: NAPALM not installed; the network runs fully simulated until it is (pip install napalm)."
command -v npx > /dev/null || echo "Note: REA tools need Node.js 22+ (brew install node). Everything else works without it."
[ -f .env ] || cp .env.example .env
if [ ! -d .git ]; then git init -q -b main; fi
if ! git remote get-url origin > /dev/null 2>&1; then
  if command -v gh > /dev/null && gh auth status > /dev/null 2>&1; then
    gh repo view "$REPO" > /dev/null 2>&1 || gh repo create "$REPO" --public --description "vix: a headless, simulated digital-infrastructure company run by agents" > /dev/null
  fi
  git remote add origin "https://github.com/$REPO.git"
fi
git add -A
git commit -q -m "vix v$VERSION" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_01S3jC8duPDMyLPt6Kh4SEpv" || echo "No changes to commit."
echo "Installed vix v$VERSION into $DEST"
