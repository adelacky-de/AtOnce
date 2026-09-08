"""Prepare stable explicit source feature identity before graph execution."""

from dataclasses import dataclass
from typing import Any, Dict, Tuple


class SourceIdentityError(RuntimeError):
    """Raised when stable source identity cannot be safely prepared or verified."""

    def __init__(self, message, committed_source_lineage_ids=()):
        self.committed_source_lineage_ids = tuple(committed_source_lineage_ids)
        super().__init__(message)


@dataclass(frozen=True)
class SourceIdentityEntry:
    source_lineage_id: str
    source_name: str
    layer: Any = None
    preflight: Any = None
    status: str = "BLOCKED"
    error: str = ""

    @property
    def assignment_count(self) -> int:
        if self.preflight is None:
            return 0
        return len(self.preflight.plan.assignments)

    @property
    def writes_required(self) -> bool:
        return self.status == "NEEDS INITIALIZATION"


@dataclass(frozen=True)
class SourceIdentityPreflight:
    workflow_id: str
    entries: Tuple[SourceIdentityEntry, ...]

    @property
    def blocked(self) -> Tuple[SourceIdentityEntry, ...]:
        return tuple(item for item in self.entries if item.status == "BLOCKED")

    @property
    def needs_initialization(self) -> Tuple[SourceIdentityEntry, ...]:
        return tuple(
            item for item in self.entries if item.status == "NEEDS INITIALIZATION"
        )

    @property
    def ready(self) -> bool:
        return not self.blocked and not self.needs_initialization

    @property
    def writes_required(self) -> bool:
        return bool(self.needs_initialization)

    @property
    def assignment_count(self) -> int:
        return sum(item.assignment_count for item in self.needs_initialization)


@dataclass(frozen=True)
class SourceIdentityInitializationResult:
    assigned_source_keys: Dict[str, int]
    verified_source_lineage_ids: Tuple[str, ...]


class SourceIdentityService:
    """Orchestrate existing QGIS source-key preflight/apply/verify primitives."""

    def __init__(self, qgis_gateway):
        self.qgis = qgis_gateway

    def preflight(self, workflow) -> SourceIdentityPreflight:
        entries = []
        for source in workflow.source_layers:
            layer = None
            try:
                layer = self.qgis.resolve_layer(source.current_layer_id)
                if layer is None:
                    raise ValueError(
                        f"Registered source binding for '{source.name}' is missing."
                    )
                # Raster SOURCEs have no feature UUIDs; skip vector identity prep.
                is_raster = getattr(self.qgis, "is_raster_layer", lambda _layer: False)
                if callable(is_raster) and is_raster(layer):
                    entries.append(
                        SourceIdentityEntry(
                            source.stable_id,
                            source.name,
                            layer,
                            None,
                            "READY",
                        )
                    )
                    continue
                if not self.qgis.is_vector_layer(layer):
                    raise ValueError(f"Source layer '{source.name}' is not a vector layer.")
                preflight = self.qgis.preflight_source_keys(layer)
                status = (
                    "NEEDS INITIALIZATION"
                    if preflight.writes_required
                    else "READY"
                )
                entries.append(
                    SourceIdentityEntry(
                        source.stable_id,
                        source.name,
                        layer,
                        preflight,
                        status,
                    )
                )
            except Exception as exc:
                entries.append(
                    SourceIdentityEntry(
                        source.stable_id,
                        source.name,
                        layer,
                        None,
                        "BLOCKED",
                        str(exc),
                    )
                )
        return SourceIdentityPreflight(workflow.workflow_id, tuple(entries))

    def initialize(
        self, preflight: SourceIdentityPreflight
    ) -> SourceIdentityInitializationResult:
        if preflight.blocked:
            details = "; ".join(
                f"{item.source_name}: {item.error}" for item in preflight.blocked
            )
            raise SourceIdentityError(
                "Stable source identity preflight is blocked: " + details
            )

        assigned = {}
        committed = []
        try:
            for entry in preflight.entries:
                if not entry.writes_required:
                    assigned[entry.source_lineage_id] = 0
                    continue
                assigned[entry.source_lineage_id] = self.qgis.apply_source_key_plan(
                    entry.layer, entry.preflight
                )
                committed.append(entry.source_lineage_id)
        except Exception as exc:
            partial = (
                " Partial identity initialization committed for: "
                + ", ".join(committed)
                + ". Retry preserves those UUIDs."
                if committed
                else ""
            )
            raise SourceIdentityError(
                "Stable source identity initialization failed: " + str(exc) + partial,
                committed,
            ) from exc

        try:
            verified = []
            for entry in preflight.entries:
                # Raster READY entries have no vector key plan; skip verification.
                if entry.preflight is None:
                    verified.append(entry.source_lineage_id)
                    continue
                current = self.qgis.preflight_source_keys(entry.layer)
                if current.plan.assignments:
                    raise ValueError(
                        f"Source '{entry.source_name}' still has missing stable UUIDs."
                    )
                if current.plan.all_keys != entry.preflight.plan.all_keys:
                    raise ValueError(
                        f"Persisted source UUIDs for '{entry.source_name}' differ from the preflight plan."
                    )
                verified.append(entry.source_lineage_id)
        except Exception as exc:
            raise SourceIdentityError(
                "Stable source identity verification failed: " + str(exc),
                committed,
            ) from exc

        return SourceIdentityInitializationResult(assigned, tuple(verified))
