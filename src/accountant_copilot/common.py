"""Shared runtime helpers for the financial statement workflow."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path

_AMOUNT_RE = re.compile(r"(?:[$€£]\s?-?\d[\d,]*(?:\.\d{2})?|-?\d{1,3}(?:,\d{3})+(?:\.\d{2})?)")

_DATE_RE = re.compile(r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4})\b")

def _load_local_env(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip("\"'")

def _unique_matches(pattern: re.Pattern[str], text: str, limit: int = 8) -> list[str]:
    seen: list[str] = []
    for match in pattern.findall(text):
        value = match.strip()
        if value and value not in seen:
            seen.append(value)
        if len(seen) >= limit:
            break
    return seen

def _clean_money_amount(amount: str | None) -> str | None:
    if amount is None:
        return None
    return amount.replace("$ ", "$").replace("€ ", "€").replace("£ ", "£").replace("+ ", "").replace("+", "").strip()

def _money_value(value: str | None) -> float | None:
    if value is None:
        return None
    cleaned = re.sub(r"[^0-9.-]", "", str(value))
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None

def _parse_bank_statement_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None

def _date_value(value: str | None) -> str | None:
    parsed = _parse_bank_statement_date(value)
    return parsed.strftime("%Y-%m-%d") if parsed else None

def _bank_transaction_amount(transaction: dict) -> float | None:
    return _money_value(transaction.get("debit") or transaction.get("credit"))

def _list_value(value: object) -> list:
    return value if isinstance(value, list) else []

def _load_optional_json(path: str | None) -> dict | None:
    return json.loads(Path(path).read_text()) if path else None

def _extract_json_object(text: str) -> dict | None:
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

def _read_json_object_file(path: Path | None) -> tuple[dict | None, str | None]:
    if path is None or not path.exists():
        return None, None
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return None, f"Codex wrote invalid JSON sidecar {path}: {exc}"
    except OSError as exc:
        return None, f"Could not read Codex JSON sidecar {path}: {exc}"
    if not isinstance(payload, dict):
        return None, f"Codex JSON sidecar was not an object: {path}"
    return payload, None

def _normalise_codex_cli_command(command: str) -> str:
    command = str(command or "").strip()
    return "codex exec" if command == "codex" else command or "codex exec"

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _normalise_amount(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    cleaned = str(value).replace("$", "").replace(",", "").strip()
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    try:
        amount = float(cleaned)
    except ValueError:
        return str(value)
    if negative:
        amount = -amount
    return f"{amount:.2f}"

def _xml_text(element: ET.Element) -> str:
    values: list[str] = []
    for child in element.iter():
        if child.text:
            values.append(child.text)
    return " ".join(" ".join(values).split())

def _write_step_progress(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    path.write_text(json.dumps(checkpoint, indent=2, sort_keys=True))

def _write_codex_attempt_history(
    path: Path,
    *,
    stage: str,
    attempts: list[dict],
    status: str,
    message: str,
    extra: dict | None = None,
) -> None:
    payload = {
        "stage": stage,
        "status": status,
        "message": message,
        "attempt_count": len(attempts),
        "last_attempt": attempts[-1] if attempts else {},
        "attempts": attempts,
    }
    if extra:
        payload.update(extra)
    _write_step_progress(path, payload)
