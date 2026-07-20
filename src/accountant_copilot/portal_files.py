"""Portal upload, archive extraction and source-file helpers."""
from __future__ import annotations

import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

def _safe_relative_path(name: str) -> Path:
    cleaned = unquote(name or "").replace("\\", "/").lstrip("/")
    parts = [part for part in cleaned.split("/") if part and part not in {".", ".."}]
    if not parts:
        parts = ["uploaded_file"]
    return Path(*parts)

def _safe_extract_zip(zip_path: Path, destination: Path) -> int:
    count = 0
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            relative = _safe_relative_path(member.filename)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            count += 1
    return count

def _scan_files(folder: Path) -> list[dict[str, Any]]:
    if not folder.exists() or not folder.is_dir():
        return []
    files: list[dict[str, Any]] = []
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        if any(part.startswith(".") for part in path.relative_to(folder).parts):
            continue
        stat = path.stat()
        files.append(
            {
                "name": path.name,
                "relative_path": str(path.relative_to(folder)),
                "path": str(path),
                "size": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            }
        )
    return files

def _prior_fs_candidates(folder: Path) -> list[dict[str, Any]]:
    scored: list[tuple[int, Path]] = []
    for path in (folder.rglob("*") if folder.exists() else []):
        if not path.is_file():
            continue
        name = path.name.lower()
        score = 0
        if "financial statement" in name or "financial statements" in name:
            score += 50
        if "fy24" in name or "2024" in name or "prior" in name:
            score += 12
        if name.endswith(".pdf"):
            score += 6
        if "tax statement" in name or "bank statement" in name or "distribution" in name:
            score -= 20
        if score > 0:
            scored.append((score, path))
    scored.sort(key=lambda item: (-item[0], item[1].name.lower()))
    return [
        {
            "name": path.name,
            "path": str(path),
            "relative_path": str(path.relative_to(folder)),
            "score": score,
        }
        for score, path in scored[:10]
    ]
