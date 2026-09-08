"""Pure semantic comparison for derived Layer C / GeoPackage state.

Identity is immutable AtOnce lineage (source layer + source UUID). Human business
keys, provider FIDs and row order are deliberately never used to pair features.
"""

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from ..models.export_revision import BaselineRow

GEOMETRY_FIELD = "__geometry_wkb"
LINEAGE_PREFIX = "_atonce_"


@dataclass(frozen=True)
class DerivedDifference:
    kind: str
    identity: str
    field_name: str = ""
    baseline_value: object = None
    current_value: object = None
    high_risk: bool = False


@dataclass
class DerivedDiffResult:
    differences: List[DerivedDifference] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.differences)

    @property
    def counts(self) -> Dict[str, int]:
        result = {
            "attribute": 0,
            "geometry": 0,
            "insert": 0,
            "delete": 0,
            "lineage": 0,
        }
        for item in self.differences:
            result[item.kind] = result.get(item.kind, 0) + 1
        return result

    @property
    def high_risk_count(self) -> int:
        return sum(1 for item in self.differences if item.high_risk)

    def summary(self) -> str:
        counts = self.counts
        return (
            f"attributes={counts['attribute']}, geometry={counts['geometry']}, "
            f"inserted={counts['insert']}, deleted={counts['delete']}, "
            f"lineage={counts['lineage']}"
        )


def _index(rows: Iterable[BaselineRow]) -> Dict[str, BaselineRow]:
    indexed: Dict[str, BaselineRow] = {}
    for row in rows:
        if not row.source_layer_id or not row.source_key:
            raise ValueError("Derived snapshots require source layer ID and source UUID.")
        if row.identity in indexed:
            raise ValueError(
                f"Duplicate derived lineage identity {row.identity!r}; refusing ambiguous diff."
            )
        indexed[row.identity] = row
    return indexed


def compare_derived_snapshots(
    baseline_rows: Iterable[BaselineRow],
    current_rows: Iterable[BaselineRow],
) -> DerivedDiffResult:
    """Compare two lineage-keyed snapshots without guessing identity.

    If `_atonce_source_layer` or `_atonce_source_key` itself is changed, the old
    identity disappears and a new identity appears. AtOnce intentionally reports
    that as delete + insert instead of trying to infer that the rows are the same
    feature. These events are high-risk because lineage may have been altered.
    """

    baseline = _index(baseline_rows)
    current = _index(current_rows)
    differences: List[DerivedDifference] = []

    for identity in sorted(set(baseline) - set(current)):
        differences.append(
            DerivedDifference(
                kind="delete",
                identity=identity,
                baseline_value=baseline[identity].values,
                high_risk=True,
            )
        )

    for identity in sorted(set(current) - set(baseline)):
        differences.append(
            DerivedDifference(
                kind="insert",
                identity=identity,
                current_value=current[identity].values,
                high_risk=True,
            )
        )

    for identity in sorted(set(baseline) & set(current)):
        old_values = baseline[identity].values
        new_values = current[identity].values
        for field_name in sorted(set(old_values) | set(new_values)):
            old_value = old_values.get(field_name)
            new_value = new_values.get(field_name)
            if old_value == new_value:
                continue
            if field_name == GEOMETRY_FIELD:
                kind = "geometry"
                high_risk = False
            elif field_name.startswith(LINEAGE_PREFIX):
                kind = "lineage"
                high_risk = True
            else:
                kind = "attribute"
                high_risk = False
            differences.append(
                DerivedDifference(
                    kind=kind,
                    identity=identity,
                    field_name=field_name,
                    baseline_value=old_value,
                    current_value=new_value,
                    high_risk=high_risk,
                )
            )

    return DerivedDiffResult(differences)


def format_derived_diff(diff: DerivedDiffResult, limit: Optional[int] = 40) -> str:
    """Return concise human-readable evidence for a QGIS warning dialog."""

    lines = ["Derived output differs from the recorded AtOnce state:", diff.summary()]
    items = diff.differences if limit is None else diff.differences[:limit]
    for item in items:
        marker = "HIGH-RISK" if item.high_risk else item.kind.upper()
        field = f" · {item.field_name}" if item.field_name else ""
        if item.kind in {"insert", "delete"}:
            lines.append(f"{marker}: {item.identity}{field}")
        else:
            lines.append(
                f"{marker}: {item.identity}{field} · "
                f"AtOnce={item.baseline_value!r} · current={item.current_value!r}"
            )
    if limit is not None and len(diff.differences) > limit:
        lines.append(f"… {len(diff.differences) - limit} more difference(s)")
    return "\n".join(lines)
