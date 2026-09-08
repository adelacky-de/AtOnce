"""Pure planning rules for A/B -> C propagation.

This module contains no QGIS imports. It defines how two source schemas become a
single derived schema and how source attributes project into that schema.
"""

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from .lineage import RESERVED_LINEAGE_FIELDS


class PropagationPlanError(ValueError):
    """Raised when AtOnce cannot derive a safe, unambiguous Layer C schema."""


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type_name: str
    length: int = 0
    precision: int = 0


@dataclass(frozen=True)
class DerivedField:
    """One output field and the source field feeding it from each source side."""

    name: str
    type_name: str
    length: int = 0
    precision: int = 0
    left_name: Optional[str] = None
    right_name: Optional[str] = None


@dataclass(frozen=True)
class DerivedSchemaPlan:
    fields: Sequence[DerivedField]
    canonical_business_key: str

    @property
    def field_names(self) -> List[str]:
        return [field.name for field in self.fields]


def _index_fields(fields: Iterable[FieldSpec], side: str) -> Dict[str, FieldSpec]:
    indexed: Dict[str, FieldSpec] = {}
    for field in fields:
        if field.name in indexed:
            raise PropagationPlanError(f"Duplicate field name {field.name!r} on {side} source.")
        indexed[field.name] = field
    return indexed


def _merged_shape(left: FieldSpec, right: Optional[FieldSpec] = None):
    if right is None:
        return left.length, left.precision
    return max(left.length, right.length), max(left.precision, right.precision)


def build_derived_schema_plan(
    left_fields: Iterable[FieldSpec],
    right_fields: Iterable[FieldSpec],
    left_business_key: str,
    right_business_key: str,
) -> DerivedSchemaPlan:
    """Build a safe union schema with one canonical business-key column.

    The left mapped field name is canonical for Layer C. The right mapped field
    feeds that canonical output field and is not emitted as a second equivalent
    column. Unmapped same-name fields are merged only when their type names match;
    type conflicts block propagation instead of coercing silently. Compatible
    fields keep the maximum declared length/precision so right-side values are not
    truncated merely because the left provider declared a narrower field.
    """

    left = _index_fields(left_fields, "left")
    right = _index_fields(right_fields, "right")

    if left_business_key not in left:
        raise PropagationPlanError(
            f"Mapped left business-key field {left_business_key!r} does not exist."
        )
    if right_business_key not in right:
        raise PropagationPlanError(
            f"Mapped right business-key field {right_business_key!r} does not exist."
        )

    for reserved in RESERVED_LINEAGE_FIELDS:
        if reserved in left and reserved != "_atonce_source_key":
            raise PropagationPlanError(
                f"Left source contains reserved AtOnce field {reserved!r}."
            )
        if reserved in right and reserved != "_atonce_source_key":
            raise PropagationPlanError(
                f"Right source contains reserved AtOnce field {reserved!r}."
            )

    if left_business_key != right_business_key and left_business_key in right:
        raise PropagationPlanError(
            f"Right source contains field {left_business_key!r} in addition to mapped field "
            f"{right_business_key!r}; the canonical Layer C business-key column would be ambiguous."
        )

    left_key = left[left_business_key]
    right_key = right[right_business_key]
    if left_key.type_name != right_key.type_name:
        raise PropagationPlanError(
            "Mapped business-key field types are incompatible: "
            f"{left_key.type_name!r} vs {right_key.type_name!r}."
        )

    output: List[DerivedField] = []
    emitted = set()

    for field in left.values():
        if field.name == "_atonce_source_key":
            continue
        if field.name == left_business_key:
            length, precision = _merged_shape(field, right_key)
            output.append(
                DerivedField(
                    name=left_business_key,
                    type_name=field.type_name,
                    length=length,
                    precision=precision,
                    left_name=left_business_key,
                    right_name=right_business_key,
                )
            )
        else:
            right_match = right.get(field.name)
            if right_match and field.name != right_business_key:
                if field.type_name != right_match.type_name:
                    raise PropagationPlanError(
                        f"Field {field.name!r} exists on both sources with incompatible "
                        f"types {field.type_name!r} and {right_match.type_name!r}."
                    )
                right_name = field.name
                length, precision = _merged_shape(field, right_match)
            else:
                right_name = None
                length, precision = _merged_shape(field)
            output.append(
                DerivedField(
                    name=field.name,
                    type_name=field.type_name,
                    length=length,
                    precision=precision,
                    left_name=field.name,
                    right_name=right_name,
                )
            )
        emitted.add(field.name)

    for field in right.values():
        if field.name in ("_atonce_source_key", right_business_key):
            continue
        if field.name in emitted:
            continue
        output.append(
            DerivedField(
                name=field.name,
                type_name=field.type_name,
                length=field.length,
                precision=field.precision,
                left_name=None,
                right_name=field.name,
            )
        )
        emitted.add(field.name)

    return DerivedSchemaPlan(fields=tuple(output), canonical_business_key=left_business_key)


def project_attributes(
    side: str,
    attributes: Mapping[str, object],
    plan: DerivedSchemaPlan,
) -> Dict[str, object]:
    """Project one source feature into the derived attribute schema."""

    if side not in {"left", "right"}:
        raise ValueError("side must be 'left' or 'right'")

    projected: Dict[str, object] = {}
    for field in plan.fields:
        source_name = field.left_name if side == "left" else field.right_name
        projected[field.name] = attributes.get(source_name) if source_name else None
    return projected
