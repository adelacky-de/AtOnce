"""Persisted evidence for one completed selective dependency propagation run."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class GraphRunState:
    """Durable selected/skipped/stale evidence for one completed graph run.

    This is execution history, not workflow configuration.  In particular,
    ``selected_edge_ids`` must never rewrite an edge's saved
    ``enabled_by_default`` setting.
    """

    workflow_id: str
    run_id: str
    created_at: str
    reason: str
    changed_node_ids: List[str] = field(default_factory=list)
    selected_edge_ids: List[str] = field(default_factory=list)
    skipped_edge_ids: List[str] = field(default_factory=list)
    refreshed_node_ids: List[str] = field(default_factory=list)
    stale_node_ids: List[str] = field(default_factory=list)
    export_revision_number: Optional[int] = None
    source_sync_id: str = ""
    workflow_config_fingerprint: str = ""

    @staticmethod
    def _normalized(values) -> List[str]:
        return sorted({str(value) for value in (values or []) if str(value)})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "reason": self.reason,
            "changed_node_ids": self._normalized(self.changed_node_ids),
            "selected_edge_ids": self._normalized(self.selected_edge_ids),
            "skipped_edge_ids": self._normalized(self.skipped_edge_ids),
            "refreshed_node_ids": self._normalized(self.refreshed_node_ids),
            "stale_node_ids": self._normalized(self.stale_node_ids),
            "export_revision_number": self.export_revision_number,
            "source_sync_id": self.source_sync_id,
            "workflow_config_fingerprint": self.workflow_config_fingerprint,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "GraphRunState":
        revision = payload.get("export_revision_number")
        return cls(
            workflow_id=str(payload.get("workflow_id", "")),
            run_id=str(payload.get("run_id", "")),
            created_at=str(payload.get("created_at", "")),
            reason=str(payload.get("reason", "")),
            changed_node_ids=cls._normalized(payload.get("changed_node_ids")),
            selected_edge_ids=cls._normalized(payload.get("selected_edge_ids")),
            skipped_edge_ids=cls._normalized(payload.get("skipped_edge_ids")),
            refreshed_node_ids=cls._normalized(payload.get("refreshed_node_ids")),
            stale_node_ids=cls._normalized(payload.get("stale_node_ids")),
            export_revision_number=(
                int(revision) if revision not in (None, "") else None
            ),
            source_sync_id=str(payload.get("source_sync_id", "")),
            workflow_config_fingerprint=str(
                payload.get("workflow_config_fingerprint", "")
            ),
        )
