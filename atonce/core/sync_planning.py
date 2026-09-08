"""Pure safety rules for approved source write-back planning."""

from dataclasses import dataclass
from typing import Any, Iterable, List, Tuple

from .export_snapshot import normalize_snapshot_value
from ..models.change import ChangeDisposition, DetectedChange


class SyncPlanningError(ValueError):
    """Raised before any source mutation when an approval set is unsafe."""


@dataclass(frozen=True)
class SourceWriteProposal:
    source_layer_id: str
    source_layer_name: str
    source_feature_key: str
    source_field_name: str
    expected_current_value: Any
    new_value: Any
    export_id: str
    revision_number: int

    @property
    def identity(self) -> Tuple[str, str, str]:
        return (
            self.source_layer_id,
            self.source_feature_key,
            self.source_field_name,
        )


def values_equal(left: Any, right: Any) -> bool:
    """Compare source values using the same typed normalization as snapshots."""

    return normalize_snapshot_value(left) == normalize_snapshot_value(right)


def change_signature(change: DetectedChange):
    """Deterministic semantic identity for a classified scan change."""

    return (
        change.source_layer_id,
        change.source_feature_key,
        change.field_name,
        change.source_field_name,
        change.export_id,
        change.revision_number,
        repr(normalize_snapshot_value(change.old_value)),
        repr(normalize_snapshot_value(change.current_source_value)),
        repr(normalize_snapshot_value(change.new_value)),
        change.disposition.value,
    )


def scan_changes_signature(changes: Iterable[DetectedChange]):
    """Order-independent fingerprint of the complete reviewed change set."""

    return tuple(sorted(change_signature(item) for item in changes))


def plan_approved_changes(
    scan_changes: Iterable[DetectedChange],
    approved_changes: Iterable[DetectedChange],
) -> List[SourceWriteProposal]:
    """Validate explicit approval and produce one proposal per source field.

    The caller must pass changes from the authoritative scan result. UI state is not
    trusted: non-Writable changes, fabricated changes, missing source fields and
    contradictory duplicate proposals all block before any provider mutation.
    """

    authoritative = {
        change_signature(item): item
        for item in scan_changes
    }
    proposals = {}

    for change in approved_changes:
        signature = change_signature(change)
        source = authoritative.get(signature)
        if source is None:
            raise SyncPlanningError(
                "Approved change is not present in the authoritative scan result."
            )
        if source.disposition != ChangeDisposition.WRITABLE:
            raise SyncPlanningError(
                f"Only Writable changes may be approved; {source.disposition.value} is blocked."
            )
        if not source.source_layer_id or not source.source_feature_key:
            raise SyncPlanningError("Writable change is missing immutable source lineage.")
        if not source.source_field_name:
            raise SyncPlanningError("Writable change has no resolved source field.")
        if source.export_id is None or source.revision_number is None:
            raise SyncPlanningError("Writable change is missing export revision identity.")

        proposal = SourceWriteProposal(
            source_layer_id=source.source_layer_id,
            source_layer_name=source.source_layer_name or source.source_layer_id,
            source_feature_key=source.source_feature_key,
            source_field_name=source.source_field_name,
            expected_current_value=source.current_source_value,
            new_value=source.new_value,
            export_id=source.export_id,
            revision_number=int(source.revision_number),
        )
        existing = proposals.get(proposal.identity)
        if existing is not None:
            if not values_equal(existing.new_value, proposal.new_value):
                raise SyncPlanningError(
                    "Approved changes contain contradictory values for the same source field."
                )
            continue
        proposals[proposal.identity] = proposal

    result = list(proposals.values())
    if not result:
        raise SyncPlanningError("Select at least one Writable change to apply.")

    export_ids = {item.export_id for item in result}
    revisions = {item.revision_number for item in result}
    if len(export_ids) != 1 or len(revisions) != 1:
        raise SyncPlanningError(
            "One sync operation must come from one linked export revision."
        )

    result.sort(key=lambda item: item.identity)
    return result
