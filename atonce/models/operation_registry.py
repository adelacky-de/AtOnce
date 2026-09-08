"""QGIS-free attribute operation registry for the free-form AtOnce canvas.

This module defines the QGIS-free product capability catalogue introduced by
Issue #39. Runtime execution remains owned by the core registry and gateway;
these entries mirror the operations that now have validated adapters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


MIN_SOURCE_ACCEPTANCE = 5
"""Real-QGIS acceptance must prove at least five simultaneous source nodes."""

MAX_SOURCE_COUNT: Optional[int] = None
"""Source count is architecturally unbounded; never replace this with a UI cap."""


@dataclass(frozen=True)
class OperationDefinition:
    key: str
    title: str
    category: str
    min_inputs: int
    max_inputs: Optional[int]
    input_roles: Tuple[str, ...] = ()
    executable_now: bool = False
    description: str = ""

    def accepts_input_count(self, count: int) -> bool:
        if count < self.min_inputs:
            return False
        if self.max_inputs is not None and count > self.max_inputs:
            return False
        return True


_OPERATION_LIST = (
    OperationDefinition(
        "filter",
        "FILTER",
        "records",
        1,
        1,
        ("input",),
        True,
        "Keep records matching an attribute condition.",
    ),
    OperationDefinition(
        "join",
        "JOIN",
        "records",
        2,
        2,
        ("left", "right"),
        True,
        "Equality attribute join. LEFT/INNER first; not a spatial join.",
    ),
    OperationDefinition(
        "merge",
        "MERGE",
        "records",
        2,
        None,
        (),
        True,
        "Append records from two or more compatible datasets.",
    ),
    OperationDefinition(
        "compare_changes",
        "COMPARE CHANGES",
        "records",
        2,
        2,
        ("previous", "current"),
        True,
        "Compare datasets by an explicit key and classify record/field changes.",
    ),
    OperationDefinition(
        "remove_duplicates",
        "REMOVE DUPLICATES",
        "records",
        1,
        1,
        ("input",),
        True,
        "Deduplicate by selected key fields.",
    ),
    OperationDefinition(
        "sort",
        "SORT",
        "records",
        1,
        1,
        ("input",),
        True,
        "Sort records by one or more attribute fields.",
    ),
    OperationDefinition(
        "aggregate",
        "AGGREGATE",
        "records",
        1,
        1,
        ("input",),
        True,
        "Group records and calculate count/sum/min/max-style summaries.",
    ),
    OperationDefinition(
        "calculate_field",
        "CALCULATE FIELD",
        "fields",
        1,
        1,
        ("input",),
        True,
        "Create or update a field from an expression.",
    ),
    OperationDefinition(
        "field_mapping",
        "FIELD MAPPING",
        "fields",
        1,
        1,
        ("input",),
        True,
        "Explicit schema/field correspondence and standardisation.",
    ),
    OperationDefinition(
        "keep_fields",
        "KEEP FIELDS",
        "fields",
        1,
        1,
        ("input",),
        True,
        "Keep only selected attribute fields.",
    ),
    OperationDefinition(
        "rename_field",
        "RENAME FIELD",
        "fields",
        1,
        1,
        ("input",),
        True,
        "Rename one or more attribute fields.",
    ),
    OperationDefinition(
        "change_field_type",
        "CHANGE FIELD TYPE",
        "fields",
        1,
        1,
        ("input",),
        True,
        "Convert attribute field types explicitly.",
    ),
    OperationDefinition(
        "passthrough",
        "PASSTHROUGH",
        "utility",
        1,
        1,
        ("input",),
        True,
        "No data transform; provides the required Source -> Operation -> Output path.",
    ),
)


OPERATION_REGISTRY: Dict[str, OperationDefinition] = {
    definition.key: definition for definition in _OPERATION_LIST
}


SPATIAL_OPERATIONS_EXCLUDED = frozenset(
    {
        "clip",
        "dissolve",
        "buffer",
        "intersection",
        "union",
        "spatial_join",
        "select_by_location",
        "reproject",
    }
)


def operation_definition(key: str) -> OperationDefinition:
    """Return a registered attribute operation or raise a clear KeyError."""

    normalized = str(key or "").strip().lower()
    return OPERATION_REGISTRY[normalized]


def palette_categories() -> Tuple[Tuple[str, Tuple[OperationDefinition, ...]], ...]:
    """Return stable palette groups without embedding UI toolkit dependencies."""

    categories = []
    for category in ("records", "fields", "utility"):
        categories.append(
            (
                category,
                tuple(
                    item
                    for item in _OPERATION_LIST
                    if item.category == category
                ),
            )
        )
    return tuple(categories)
