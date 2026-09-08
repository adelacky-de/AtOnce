"""Serializable audit records for approved upstream sync operations."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SyncAuditChange:
    source_layer_id: str
    source_layer_name: str
    source_feature_key: str
    source_field_name: str
    before_value: Any
    after_value: Any
    result: str = "pending"
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_layer_id": self.source_layer_id,
            "source_layer_name": self.source_layer_name,
            "source_feature_key": self.source_feature_key,
            "source_field_name": self.source_field_name,
            "before_value": self.before_value,
            "after_value": self.after_value,
            "result": self.result,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "SyncAuditChange":
        return cls(
            source_layer_id=payload.get("source_layer_id", ""),
            source_layer_name=payload.get("source_layer_name", ""),
            source_feature_key=payload.get("source_feature_key", ""),
            source_field_name=payload.get("source_field_name", ""),
            before_value=payload.get("before_value"),
            after_value=payload.get("after_value"),
            result=payload.get("result", "pending"),
            message=payload.get("message", ""),
        )


@dataclass
class SyncAuditRecord:
    sync_id: str
    workflow_id: str
    export_id: str
    source_revision_number: int
    started_at: str
    status: str = "pending"
    completed_at: Optional[str] = None
    returned_workbook_archive: str = ""
    refresh_revision_number: Optional[int] = None
    message: str = ""
    changes: List[SyncAuditChange] = field(default_factory=list)
    # G5 exact run-scoped dependency evidence. These defaults keep every pre-G5
    # audit readable without migration and do not modify workflow edge defaults.
    selected_edge_ids: List[str] = field(default_factory=list)
    skipped_edge_ids: List[str] = field(default_factory=list)
    refreshed_node_ids: List[str] = field(default_factory=list)
    stale_node_ids: List[str] = field(default_factory=list)

    @staticmethod
    def _ids(values) -> List[str]:
        return sorted({str(value) for value in (values or []) if str(value)})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sync_id": self.sync_id,
            "workflow_id": self.workflow_id,
            "export_id": self.export_id,
            "source_revision_number": self.source_revision_number,
            "started_at": self.started_at,
            "status": self.status,
            "completed_at": self.completed_at,
            "returned_workbook_archive": self.returned_workbook_archive,
            "refresh_revision_number": self.refresh_revision_number,
            "message": self.message,
            "changes": [item.to_dict() for item in self.changes],
            "selected_edge_ids": self._ids(self.selected_edge_ids),
            "skipped_edge_ids": self._ids(self.skipped_edge_ids),
            "refreshed_node_ids": self._ids(self.refreshed_node_ids),
            "stale_node_ids": self._ids(self.stale_node_ids),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "SyncAuditRecord":
        return cls(
            sync_id=payload.get("sync_id", ""),
            workflow_id=payload.get("workflow_id", ""),
            export_id=payload.get("export_id", ""),
            source_revision_number=int(payload.get("source_revision_number", 0)),
            started_at=payload.get("started_at", ""),
            status=payload.get("status", "pending"),
            completed_at=payload.get("completed_at"),
            returned_workbook_archive=payload.get("returned_workbook_archive", ""),
            refresh_revision_number=(
                int(payload["refresh_revision_number"])
                if payload.get("refresh_revision_number") is not None
                else None
            ),
            message=payload.get("message", ""),
            changes=[
                SyncAuditChange.from_dict(item)
                for item in payload.get("changes", [])
                if isinstance(item, dict)
            ],
            selected_edge_ids=cls._ids(payload.get("selected_edge_ids")),
            skipped_edge_ids=cls._ids(payload.get("skipped_edge_ids")),
            refreshed_node_ids=cls._ids(payload.get("refreshed_node_ids")),
            stale_node_ids=cls._ids(payload.get("stale_node_ids")),
        )
