"""Dependency graph primitives used by workflow registration and UI rendering."""

from dataclasses import dataclass
from enum import Enum


class NodeKind(str, Enum):
    SOURCE_LAYER = "source_layer"
    DERIVED_LAYER = "derived_layer"
    GEOPACKAGE = "geopackage"
    EXPORT = "export"


@dataclass(frozen=True)
class DependencyNode:
    node_id: str
    label: str
    kind: NodeKind


@dataclass(frozen=True)
class DependencyEdge:
    upstream_id: str
    downstream_id: str
    operation: str
