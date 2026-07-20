#!/usr/bin/env python3
"""Send an optional Telegram status message for background workpaper jobs.

The script is intentionally no-op when Telegram env vars are absent so the
same job runner can be used from desktop, terminal, or CI.
"""
from __future__ import annotations

import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path


def _load_local_env() -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip("\"'")


def _env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def main() -> int:
    _load_local_env()
    text = " ".join(sys.argv[1:]).strip()
    if not text:
        return 0
    token = _env("ACCOUNTANT_COPILOT_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN")
    chat_id = _env("ACCOUNTANT_COPILOT_TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID", "TELEGRAM_HOME_CHANNEL")
    thread_id = _env("ACCOUNTANT_COPILOT_TELEGRAM_THREAD_ID", "TELEGRAM_THREAD_ID")
    if not token or not chat_id:
        return 0

    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    if thread_id:
        payload["message_thread_id"] = thread_id
    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
    except Exception as exc:  # noqa: BLE001 - status notification must never break the job.
        print(f"Telegram status send failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
