"""Serializable semantic baseline for the current derived Layer C state."""

from dataclasses import dataclass, field
from typing import Any, Dict, List

from .export_revision import BaselineRow


@dataclass
class DerivedState:
    workflow_id: str
    created_at: str
    origin: str
    rows: List[BaselineRow] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "created_at": self.created_at,
            "origin": self.origin,
            "rows": [row.to_dict() for row in self.rows],
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "DerivedState":
        return cls(
            workflow_id=payload.get("workflow_id", ""),
            created_at=payload.get("created_at", ""),
            origin=payload.get("origin", ""),
            rows=[
                BaselineRow.from_dict(item)
                for item in payload.get("rows", [])
                if isinstance(item, dict)
            ],
        )
