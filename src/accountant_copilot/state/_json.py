"""Small JSON model helpers.

The project intentionally keeps the first foundation dependency-light. These
helpers provide the tiny subset of Pydantic-like ergonomics the domain models
need now (`model_dump_json` and `model_validate_json`) while leaving room to
move to Pydantic later if schemas become more complex.
"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any


def to_plain(value: Any) -> Any:
    """Convert dataclasses/enums into JSON-serialisable Python values."""
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {k: to_plain(v) for k, v in asdict(value).items()}
    if isinstance(value, list):
        return [to_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: to_plain(v) for k, v in value.items()}
    return value


class JsonModelMixin:
    """Minimal JSON API shared by domain dataclasses."""

    def model_dump(self) -> dict[str, Any]:
        return to_plain(self)

    def model_dump_json(self) -> str:
        return json.dumps(self.model_dump(), sort_keys=True)

    @classmethod
    def model_validate_json(cls, raw: str):
        return cls.from_dict(json.loads(raw))
