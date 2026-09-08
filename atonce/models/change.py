"""Detected-change and sync-preview models."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ChangeDisposition(str, Enum):
    WRITABLE = "writable"
    DERIVED = "derived"
    CONFLICT = "conflict"
    UNRESOLVED = "unresolved"


@dataclass
class DetectedChange:
    source_layer_id: str
    source_feature_key: str
    field_name: str
    old_value: Any
    new_value: Any
    disposition: ChangeDisposition = ChangeDisposition.UNRESOLVED
    reason: Optional[str] = None
    source_field_name: Optional[str] = None
    current_source_value: Any = None
    source_layer_name: Optional[str] = None
    export_id: Optional[str] = None
    revision_number: Optional[int] = None


@dataclass
class ChangeScanResult:
    workflow_id: str
    export_id: str
    export_name: str
    path: str
    revision_number: int
    scanned_rows: int
    baseline_rows: int
    changes: List[DetectedChange] = field(default_factory=list)

    @property
    def counts(self) -> Dict[ChangeDisposition, int]:
        return {
            disposition: sum(1 for item in self.changes if item.disposition == disposition)
            for disposition in ChangeDisposition
        }

    @property
    def writable_count(self) -> int:
        return self.counts[ChangeDisposition.WRITABLE]

    @property
    def blocked_count(self) -> int:
        counts = self.counts
        return counts[ChangeDisposition.DERIVED] + counts[ChangeDisposition.UNRESOLVED]
