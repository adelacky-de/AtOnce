"""Pure validation for trace-to-source navigation targets."""

from dataclasses import dataclass

from ..models.change import ChangeDisposition, DetectedChange


class TracePlanningError(ValueError):
    """Raised when a detected change cannot identify one authoritative source target."""


@dataclass(frozen=True)
class TraceTarget:
    source_layer_id: str
    source_feature_key: str
    source_layer_name: str
    field_name: str
    source_field_name: str


def plan_trace_target(change: DetectedChange) -> TraceTarget:
    """Validate one detected change for read-only source navigation.

    Trace identity is immutable source layer + source UUID. Visible table position,
    provider FID and human business IDs are never accepted as substitutes.
    """

    if not isinstance(change, DetectedChange):
        raise TracePlanningError("Trace requires an authoritative detected change.")
    if change.disposition == ChangeDisposition.UNRESOLVED:
        raise TracePlanningError("Unresolved changes do not have authoritative source identity.")
    layer_id = str(change.source_layer_id or "").strip()
    source_key = str(change.source_feature_key or "").strip()
    if not layer_id:
        raise TracePlanningError("Detected change is missing its originating source layer id.")
    if not source_key:
        raise TracePlanningError("Detected change is missing its immutable source UUID.")

    return TraceTarget(
        source_layer_id=layer_id,
        source_feature_key=source_key,
        source_layer_name=str(change.source_layer_name or layer_id),
        field_name=str(change.field_name or ""),
        source_field_name=str(change.source_field_name or change.field_name or ""),
    )
