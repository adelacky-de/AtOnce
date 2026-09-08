"""Pure returned-XLSX comparison and classification helpers.

This module deliberately contains no QGIS imports. Spreadsheet row order is never
identity: AtOnce compares rows by immutable source lineage and validates workflow /
export / revision metadata before considering any cell writable.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .export_snapshot import normalize_snapshot_value
from .lineage import (
    LINEAGE_FIELD_EXPORT,
    LINEAGE_FIELD_REVISION,
    LINEAGE_FIELD_SOURCE_KEY,
    LINEAGE_FIELD_SOURCE_LAYER,
    LINEAGE_FIELD_WORKFLOW,
)
from ..models.export_revision import BaselineRow


ROW_FIELD = "__row__"
MISSING_FIELD = "__missing_field__"
PROTECTED_METADATA_FIELDS = {
    LINEAGE_FIELD_WORKFLOW,
    LINEAGE_FIELD_SOURCE_LAYER,
    LINEAGE_FIELD_SOURCE_KEY,
    LINEAGE_FIELD_EXPORT,
    LINEAGE_FIELD_REVISION,
}


@dataclass(frozen=True)
class RawWorkbookChange:
    source_layer_id: str
    source_key: str
    field_name: str
    baseline_value: Any
    workbook_value: Any
    baseline_typed: Optional[Mapping[str, Any]] = None
    reason: Optional[str] = None
    unresolved: bool = False

    @property
    def identity(self) -> str:
        return f"{self.source_layer_id}:{self.source_key}"


@dataclass
class WorkbookComparison:
    expected_workflow_id: str
    expected_export_id: str
    expected_revision: int
    scanned_rows: int = 0
    baseline_rows: int = 0
    changes: List[RawWorkbookChange] = field(default_factory=list)



def snapshot_display_value(snapshot_value: Any) -> Any:
    """Return the stored scalar value from a typed baseline payload."""

    if isinstance(snapshot_value, Mapping) and "type" in snapshot_value:
        return snapshot_value.get("value")
    return snapshot_value



def snapshot_value_equal(snapshot_value: Any, current_value: Any) -> bool:
    """Compare an exported typed value with a newly read spreadsheet/source value.

    QGIS/GDAL may round-trip an integer cell as a floating value. Integer/float
    numeric equality is therefore treated as equivalent, while null/empty-string and
    other type differences remain meaningful.
    """

    if not isinstance(snapshot_value, Mapping) or "type" not in snapshot_value:
        return snapshot_value == current_value

    baseline_type = snapshot_value.get("type")
    baseline_value = snapshot_value.get("value")
    normalized = normalize_snapshot_value(current_value)
    current_type = normalized.get("type")
    current_scalar = normalized.get("value")

    if baseline_type == current_type:
        return baseline_value == current_scalar

    numeric = {"int", "float"}
    if baseline_type in numeric and current_type in numeric:
        try:
            return float(baseline_value) == float(current_scalar)
        except (TypeError, ValueError):
            return False

    return False



def parse_revision(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 1 else None



def workbook_revision_candidates(rows: Iterable[Mapping[str, Any]]) -> List[int]:
    return sorted(
        {
            revision
            for revision in (parse_revision(row.get(LINEAGE_FIELD_REVISION)) for row in rows)
            if revision is not None
        }
    )



def compare_workbook_rows(
    baseline_rows: Sequence[BaselineRow],
    workbook_rows: Sequence[Mapping[str, Any]],
    workbook_fields: Iterable[str],
    expected_workflow_id: str,
    expected_export_id: str,
    expected_revision: int,
) -> WorkbookComparison:
    """Compare workbook values against one authoritative export baseline.

    Invalid/tampered row metadata, duplicate lineage, inserted rows and deleted rows
    are surfaced as unresolved row-level changes. Only rows whose metadata and
    immutable lineage match the selected baseline are compared cell-by-cell.
    """

    result = WorkbookComparison(
        expected_workflow_id=expected_workflow_id,
        expected_export_id=expected_export_id,
        expected_revision=expected_revision,
        scanned_rows=len(workbook_rows),
        baseline_rows=len(baseline_rows),
    )
    fields = set(workbook_fields)
    baseline_index = {row.identity: row for row in baseline_rows}

    lineage_counts: Dict[str, int] = {}
    for row in workbook_rows:
        layer_id = str(row.get(LINEAGE_FIELD_SOURCE_LAYER) or "").strip()
        source_key = str(row.get(LINEAGE_FIELD_SOURCE_KEY) or "").strip()
        if layer_id and source_key:
            identity = f"{layer_id}:{source_key}"
            lineage_counts[identity] = lineage_counts.get(identity, 0) + 1

    seen = set()
    for row in workbook_rows:
        layer_id = str(row.get(LINEAGE_FIELD_SOURCE_LAYER) or "").strip()
        source_key = str(row.get(LINEAGE_FIELD_SOURCE_KEY) or "").strip()
        identity = f"{layer_id}:{source_key}" if layer_id and source_key else ""
        if identity in baseline_index:
            seen.add(identity)

        metadata_error = _metadata_error(
            row,
            expected_workflow_id,
            expected_export_id,
            expected_revision,
        )
        if metadata_error:
            result.changes.append(
                RawWorkbookChange(
                    layer_id,
                    source_key,
                    ROW_FIELD,
                    None,
                    None,
                    reason=metadata_error,
                    unresolved=True,
                )
            )
            continue

        if lineage_counts.get(identity, 0) > 1:
            result.changes.append(
                RawWorkbookChange(
                    layer_id,
                    source_key,
                    ROW_FIELD,
                    None,
                    None,
                    reason="Duplicate source lineage appears more than once in the workbook.",
                    unresolved=True,
                )
            )
            continue

        baseline = baseline_index.get(identity)
        if baseline is None:
            result.changes.append(
                RawWorkbookChange(
                    layer_id,
                    source_key,
                    ROW_FIELD,
                    None,
                    dict(row),
                    reason="Inserted/new spreadsheet rows are unsupported in v0.1.",
                    unresolved=True,
                )
            )
            continue

        for field_name, baseline_typed in baseline.values.items():
            if field_name in PROTECTED_METADATA_FIELDS:
                continue
            if field_name not in fields:
                result.changes.append(
                    RawWorkbookChange(
                        layer_id,
                        source_key,
                        field_name,
                        snapshot_display_value(baseline_typed),
                        None,
                        baseline_typed=baseline_typed,
                        reason=f"Spreadsheet column {field_name!r} is missing.",
                        unresolved=True,
                    )
                )
                continue
            current = row.get(field_name)
            if not snapshot_value_equal(baseline_typed, current):
                result.changes.append(
                    RawWorkbookChange(
                        layer_id,
                        source_key,
                        field_name,
                        snapshot_display_value(baseline_typed),
                        current,
                        baseline_typed=baseline_typed,
                    )
                )

    for identity, baseline in baseline_index.items():
        if identity in seen:
            continue
        result.changes.append(
            RawWorkbookChange(
                baseline.source_layer_id,
                baseline.source_key,
                ROW_FIELD,
                {name: snapshot_display_value(value) for name, value in baseline.values.items()},
                None,
                reason="Deleted spreadsheet rows are unsupported in v0.1.",
                unresolved=True,
            )
        )

    return result



def source_field_for(workflow, source_layer_id: str, derived_field_name: str, source_fields: Iterable[str]) -> Optional[str]:
    """Map one Layer-C/XLSX field back to its originating source field when direct."""

    if derived_field_name.startswith("_atonce_"):
        return None

    mapping = workflow.primary_field_mapping
    if mapping is not None and derived_field_name == mapping.left_field:
        if source_layer_id == mapping.left_layer_id:
            return mapping.left_field
        if source_layer_id == mapping.right_layer_id:
            return mapping.right_field

    source_field_set = set(source_fields)
    return derived_field_name if derived_field_name in source_field_set else None



def _metadata_error(
    row: Mapping[str, Any],
    expected_workflow_id: str,
    expected_export_id: str,
    expected_revision: int,
) -> Optional[str]:
    workflow_id = str(row.get(LINEAGE_FIELD_WORKFLOW) or "").strip()
    export_id = str(row.get(LINEAGE_FIELD_EXPORT) or "").strip()
    source_layer = str(row.get(LINEAGE_FIELD_SOURCE_LAYER) or "").strip()
    source_key = str(row.get(LINEAGE_FIELD_SOURCE_KEY) or "").strip()
    revision = parse_revision(row.get(LINEAGE_FIELD_REVISION))

    if not source_layer or not source_key:
        return "Missing source lineage metadata; AtOnce will not guess a row identity."
    if workflow_id != expected_workflow_id:
        return "Workbook workflow metadata is missing or does not match the registered workflow."
    if export_id != expected_export_id:
        return "Workbook export metadata is missing or does not match the selected linked export."
    if revision != expected_revision:
        return "Workbook revision metadata is missing or does not match the selected baseline revision."
    return None
