#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_BIN="${HERMES_BIN:-hermes}"

if ! command -v "$HERMES_BIN" >/dev/null 2>&1; then
  echo "Hermes CLI not found. Install Hermes Desktop/CLI first, then rerun this script."
  exit 1
fi

ensure_profile() {
  local name="$1"
  local description="$2"
  local soul_template="$3"

  if "$HERMES_BIN" profile show "$name" >/dev/null 2>&1; then
    echo "Updating existing Hermes profile: $name"
  else
    echo "Creating Hermes profile: $name"
    "$HERMES_BIN" profile create "$name" --clone --description "$description"
  fi

  local profile_dir="$HOME/.hermes/profiles/$name"
  mkdir -p "$profile_dir"
  cp "$soul_template" "$profile_dir/SOUL.md"
  "$HERMES_BIN" profile describe "$name" --text "$description" >/dev/null
  "$HERMES_BIN" -p "$name" config set terminal.cwd "$REPO_ROOT" >/dev/null

  local codex_source="$HOME/.codex"
  local codex_target="$profile_dir/home/.codex"
  mkdir -p "$codex_target"
  if [ -f "$codex_source/auth.json" ]; then
    ln -sf "$codex_source/auth.json" "$codex_target/auth.json"
  else
    echo "Warning: $codex_source/auth.json not found. Run 'codex login' before using nested Codex from Hermes profile '$name'."
  fi
  if [ -f "$codex_source/config.toml" ]; then
    ln -sf "$codex_source/config.toml" "$codex_target/config.toml"
  fi
}

ensure_profile \
  "workpaper" \
  "Accountant-facing financial statement workpaper assistant. Takes a local client folder path, coordinates Codex CLI to prepare a TB Bridge workbook, and returns accountant-friendly Excel output plus short review summary." \
  "$REPO_ROOT/docs/hermes_profiles/workpaper/SOUL.md"

ensure_profile \
  "turing" \
  "Senior accountant supervisor for financial statement automation. Reviews Codex-generated TB Bridge workpapers, challenges accounting logic and evidence, and writes correction briefs for Codex." \
  "$REPO_ROOT/docs/hermes_profiles/turing/SOUL.md"

echo
echo "Installed Hermes accounting profiles:"
echo "  - workpaper: accountant-facing front door"
echo "  - turing: senior accountant supervisor"
echo
echo "Open Hermes Desktop and start a new session under the workpaper profile."
