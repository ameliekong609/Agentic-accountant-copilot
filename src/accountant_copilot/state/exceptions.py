"""Exception queue models.

Exceptions are unresolved accounting/control issues that must be resolved,
approved, or accepted as risk before final release.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4

from ._json import JsonModelMixin


class ExceptionSeverity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ExceptionStatus(str, Enum):
    OPEN = "open"
    PROPOSED = "proposed"
    RESOLVED = "resolved"
    ACCEPTED_RISK = "accepted_risk"
    REJECTED = "rejected"


@dataclass
class ExceptionItem(JsonModelMixin):
    source: str
    severity: ExceptionSeverity
    category: str
    description: str
    recommended_action: str
    exception_id: str = field(default_factory=lambda: f"exc_{uuid4().hex[:12]}")
    evidence_refs: list[str] = field(default_factory=list)
    status: ExceptionStatus = ExceptionStatus.OPEN
    requires_human_approval: bool = False
    decision_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExceptionItem":
        return cls(
            exception_id=data.get("exception_id") or f"exc_{uuid4().hex[:12]}",
            source=data["source"],
            severity=ExceptionSeverity(data["severity"]),
            category=data["category"],
            description=data["description"],
            evidence_refs=list(data.get("evidence_refs", [])),
            recommended_action=data["recommended_action"],
            status=ExceptionStatus(data.get("status", ExceptionStatus.OPEN.value)),
            requires_human_approval=bool(data.get("requires_human_approval", False)),
            decision_id=data.get("decision_id"),
        )

    @property
    def is_open(self) -> bool:
        return self.status in {ExceptionStatus.OPEN, ExceptionStatus.PROPOSED}

    @property
    def is_blocking_by_default(self) -> bool:
        return (self.is_open or self.status == ExceptionStatus.REJECTED) and self.severity in {
            ExceptionSeverity.CRITICAL,
            ExceptionSeverity.HIGH,
        }
