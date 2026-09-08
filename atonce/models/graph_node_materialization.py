"""Persisted evidence for one explicit graph-derived materialization."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .export_revision import BaselineRow


@dataclass
class GraphNodeMaterializationState:
    """Current QGIS binding plus semantic evidence for one logical graph node."""

    workflow_id: str
    node_id: str
    layer_id: Optional[str]
    created_at: str
    origin: str
    rows: List[BaselineRow] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "node_id": self.node_id,
            "layer_id": self.layer_id,
            "created_at": self.created_at,
            "origin": self.origin,
            "rows": [row.to_dict() for row in self.rows],
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "GraphNodeMaterializationState":
        return cls(
            workflow_id=str(payload.get("workflow_id", "")),
            node_id=str(payload.get("node_id", "")),
            layer_id=payload.get("layer_id"),
            created_at=str(payload.get("created_at", "")),
            origin=str(payload.get("origin", "")),
            rows=[
                BaselineRow.from_dict(item)
                for item in payload.get("rows", [])
                if isinstance(item, dict)
            ],
        )
