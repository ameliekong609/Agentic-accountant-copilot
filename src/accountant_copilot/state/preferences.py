"""Preference memory models.

Preference rules capture approved accounting/client/firm conventions. Agents can
suggest rules, but only approved rules should be applied automatically.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4

from ._json import JsonModelMixin


class PreferenceScope(str, Enum):
    FIRM = "firm"
    ACCOUNTANT = "accountant"
    CLIENT = "client"
    ENTITY_TYPE = "entity_type"
    ENGAGEMENT = "engagement"


class PreferenceStatus(str, Enum):
    SUGGESTED = "suggested"
    APPROVED = "approved"
    RETIRED = "retired"


@dataclass
class PreferenceRule(JsonModelMixin):
    scope: PreferenceScope
    subject: str
    rule: str
    status: PreferenceStatus = PreferenceStatus.SUGGESTED
    preference_id: str = field(default_factory=lambda: f"pref_{uuid4().hex[:12]}")
    evidence_refs: list[str] = field(default_factory=list)
    approved_by: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PreferenceRule":
        return cls(
            preference_id=data.get("preference_id") or f"pref_{uuid4().hex[:12]}",
            scope=PreferenceScope(data["scope"]),
            subject=data["subject"],
            rule=data["rule"],
            status=PreferenceStatus(data.get("status", PreferenceStatus.SUGGESTED.value)),
            evidence_refs=list(data.get("evidence_refs", [])),
            approved_by=data.get("approved_by"),
        )

    @property
    def is_approved(self) -> bool:
        return self.status == PreferenceStatus.APPROVED
