"""Structured evidence references for source-traceable workpapers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ._json import JsonModelMixin


@dataclass
class EvidenceRef(JsonModelMixin):
    evidence_id: str
    source_type: str
    file_path: str
    page: str | None = None
    row: str | None = None
    quote: str | None = None
    amount: str | None = None
    date: str | None = None
    confidence: str | None = None
    document_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvidenceRef":
        return cls(
            evidence_id=data["evidence_id"],
            source_type=data["source_type"],
            file_path=data["file_path"],
            page=data.get("page"),
            row=data.get("row"),
            quote=data.get("quote"),
            amount=data.get("amount"),
            date=data.get("date"),
            confidence=data.get("confidence"),
            document_id=data.get("document_id"),
        )
