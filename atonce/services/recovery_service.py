"""Explicit source/workbook recovery without identity guessing or silent mutation."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Set
from uuid import uuid4

from ..core.change_detection import compare_workbook_rows, parse_revision
from ..core.lineage import (
    LINEAGE_FIELD_EXPORT,
    LINEAGE_FIELD_REVISION,
    LINEAGE_FIELD_SOURCE_KEY,
    LINEAGE_FIELD_SOURCE_LAYER,
    LINEAGE_FIELD_WORKFLOW,
)
from ..infrastructure.recovery_runtime import file_sha256, require_source_write_capability


class RecoveryError(RuntimeError):
    """Raised when an explicit recovery request cannot be proven safe."""


@dataclass(frozen=True)
class SourceRelinkResult:
    workflow_id: str
    source_lineage_id: str
    old_binding_id: str
    new_binding_id: str
    historical_uuid_count: int
    candidate_uuid_count: int


@dataclass(frozen=True)
class SourceReplaceResult:
    workflow_id: str
    old_source_lineage_id: str
    new_source_lineage_id: str
    new_binding_id: str
    lineage_generation: int
    detached_old_derived: bool = True


@dataclass(frozen=True)
class WorkbookRelinkResult:
    workflow_id: str
    export_id: str
    path: str
    revision_number: int
    contains_changes: bool


class RecoveryService:
    """Recover bindings while keeping source lineage identity explicit and durable."""

    def __init__(
        self,
        store,
        qgis_gateway,
        reader,
        propagation_service=None,
    ):
        self.store = store
        self.qgis = qgis_gateway
        self.reader = reader
        self.propagation_service = propagation_service

    def relink_source(
        self,
        source_lineage_id: str,
        candidate_layer_id: str,
        workflow_id: Optional[str] = None,
    ) -> SourceRelinkResult:
        workflow = self._workflow(workflow_id)
        source = workflow.source_by_lineage_id(source_lineage_id)
        if source is None:
            raise RecoveryError("Unknown registered source lineage identity.")

        candidate = self.qgis.resolve_layer(candidate_layer_id)
        if candidate is None or not self.qgis.is_vector_layer(candidate):
            raise RecoveryError("Relink Source requires one loaded vector layer candidate.")
        self._require_candidate_not_bound_elsewhere(workflow, source.stable_id, candidate.id())
        self._require_candidate_not_derived(workflow, candidate.id())
        self._require_mapped_field(workflow, source.stable_id, candidate)
        self._require_write_capability(candidate)

        try:
            preflight = self.qgis.preflight_source_keys(candidate)
        except Exception as exc:
            raise RecoveryError(f"Candidate source UUID validation failed: {exc}") from exc

        if not preflight.field_exists:
            raise RecoveryError(
                "Relink requires the existing _atonce_source_key field. AtOnce will not create lineage during relink; use Replace Source for a new authority."
            )
        if preflight.plan.assignments:
            raise RecoveryError(
                "Relink candidate contains blank AtOnce UUID values. Relink never regenerates historical source identity."
            )

        expected = self._current_expected_source_keys(workflow, source.stable_id)
        if not expected:
            raise RecoveryError(
                "No current AtOnce UUID baseline exists for this source, so continuity cannot be proven safely. Use Replace Source instead."
            )
        candidate_keys = set(preflight.plan.existing.values())
        missing = sorted(expected.difference(candidate_keys))
        if missing:
            preview = ", ".join(missing[:3])
            suffix = "…" if len(missing) > 3 else ""
            raise RecoveryError(
                f"Relink candidate is missing {len(missing)} UUID(s) from the latest authoritative state, including {preview}{suffix}. "
                "Use Replace Source if this is a new authoritative dataset."
            )

        old_binding = source.current_layer_id
        source.binding_id = candidate.id()
        source.name = candidate.name()
        source.source_uri = candidate.source()
        source.provider = candidate.providerType()
        self.store.save_workflow(workflow)
        return SourceRelinkResult(
            workflow_id=workflow.workflow_id,
            source_lineage_id=source.stable_id,
            old_binding_id=old_binding,
            new_binding_id=candidate.id(),
            historical_uuid_count=len(expected),
            candidate_uuid_count=len(candidate_keys),
        )

    def replace_source(
        self,
        source_lineage_id: str,
        candidate_layer_id: str,
        workflow_id: Optional[str] = None,
    ) -> SourceReplaceResult:
        workflow = self._workflow(workflow_id)
        source = workflow.source_by_lineage_id(source_lineage_id)
        if source is None:
            raise RecoveryError("Unknown registered source lineage identity.")

        candidate = self.qgis.resolve_layer(candidate_layer_id)
        if candidate is None or not self.qgis.is_vector_layer(candidate):
            raise RecoveryError("Replace Source requires one loaded vector layer candidate.")
        self._require_candidate_not_bound_elsewhere(workflow, source.stable_id, candidate.id())
        self._require_candidate_not_derived(workflow, candidate.id())
        self._require_mapped_field(workflow, source.stable_id, candidate)
        self._require_write_capability(candidate)

        try:
            self.qgis.preflight_source_keys(candidate)
        except Exception as exc:
            raise RecoveryError(f"Replacement source preflight failed: {exc}") from exc

        derived = workflow.derived_layer
        old_derived_id = derived.layer_id if derived else None
        if old_derived_id and self.qgis.resolve_layer(old_derived_id) is not None:
            load_state = getattr(self.store, "load_derived_state", None)
            state = load_state(workflow.workflow_id) if callable(load_state) else None
            if state is None:
                raise RecoveryError(
                    "The current Layer C has no recorded semantic baseline, so AtOnce cannot prove it is safe to detach during source replacement. Rebuild/export Layer C first or remove it explicitly."
                )

        if self.propagation_service is not None:
            try:
                divergence = self.propagation_service.check_derived_divergence(workflow.workflow_id)
            except Exception as exc:
                raise RecoveryError(f"Could not verify current Layer C before replacement: {exc}") from exc
            if divergence is not None:
                raise RecoveryError(
                    "Layer C has manual changes. Review/rebuild those derived changes before replacing an authoritative source."
                )

        old_stable = source.stable_id
        new_stable = str(uuid4())
        mapping = workflow.primary_field_mapping
        if mapping is not None:
            if mapping.left_layer_id == old_stable:
                mapping.left_layer_id = new_stable
            elif mapping.right_layer_id == old_stable:
                mapping.right_layer_id = new_stable
            else:
                raise RecoveryError("The source being replaced is not represented in the confirmed mapping.")

        source.layer_id = new_stable
        source.binding_id = candidate.id()
        source.name = candidate.name()
        source.source_uri = candidate.source()
        source.provider = candidate.providerType()
        workflow.lineage_generation += 1

        if workflow.derived_layer is not None:
            workflow.derived_layer.layer_id = None
            workflow.derived_layer.source_uri = None
            workflow.derived_layer.provider = None

        # Persist the new authority before any project-layer cleanup. A persistence
        # failure therefore leaves the visible QGIS project untouched rather than in
        # a half-replaced state.
        self.store.save_workflow(workflow)

        detached = True
        if old_derived_id:
            project = getattr(self.qgis, "project", None)
            try:
                if project is not None and project.mapLayer(old_derived_id) is not None:
                    project.removeMapLayer(old_derived_id)
                    set_dirty = getattr(project, "setDirty", None)
                    if callable(set_dirty):
                        set_dirty(True)
            except Exception:
                # Replacement is already durably persisted. Do not report the whole
                # operation as failed (which could tempt a retry and increment the
                # generation again); return a cleanup warning signal instead.
                detached = False

        return SourceReplaceResult(
            workflow_id=workflow.workflow_id,
            old_source_lineage_id=old_stable,
            new_source_lineage_id=new_stable,
            new_binding_id=candidate.id(),
            lineage_generation=workflow.lineage_generation,
            detached_old_derived=detached,
        )

    def relink_workbook(
        self,
        export_id: str,
        path: str,
        workflow_id: Optional[str] = None,
    ) -> WorkbookRelinkResult:
        workflow = self._workflow(workflow_id)
        export = next((item for item in workflow.exports if item.export_id == export_id), None)
        if export is None:
            raise RecoveryError("Unknown linked XLSX export.")

        workbook = Path(path).expanduser()
        if not workbook.is_file():
            raise RecoveryError(f"Selected workbook does not exist: {workbook}")
        candidate_path = str(workbook.resolve())
        gpkg_path = str(Path(workflow.gpkg_path).expanduser().resolve()) if workflow.gpkg_path else ""
        if candidate_path == gpkg_path:
            raise RecoveryError("A linked XLSX cannot use the GeoPackage output path.")
        for other in workflow.exports:
            if other.export_id == export.export_id or not other.path:
                continue
            if candidate_path == str(Path(other.path).expanduser().resolve()):
                raise RecoveryError("Two linked exports cannot relink to the same workbook path.")

        try:
            read = self.reader.read_xlsx_rows(str(workbook))
        except Exception as exc:
            raise RecoveryError(f"Could not read selected workbook: {exc}") from exc
        if not read.rows:
            raise RecoveryError("Selected workbook has no AtOnce data rows to prove identity.")

        revisions = set()
        identities = []
        for row in read.rows:
            workflow_value = str(row.get(LINEAGE_FIELD_WORKFLOW) or "").strip()
            export_value = str(row.get(LINEAGE_FIELD_EXPORT) or "").strip()
            source_layer = str(row.get(LINEAGE_FIELD_SOURCE_LAYER) or "").strip()
            source_key = str(row.get(LINEAGE_FIELD_SOURCE_KEY) or "").strip()
            revision = parse_revision(row.get(LINEAGE_FIELD_REVISION))
            if workflow_value != workflow.workflow_id:
                raise RecoveryError("Workbook workflow metadata does not match this AtOnce workflow.")
            if export_value != export.export_id:
                raise RecoveryError("Workbook export metadata does not match the selected linked export.")
            if not source_layer or not source_key or revision is None:
                raise RecoveryError("Workbook has missing/tampered immutable AtOnce row metadata.")
            revisions.add(revision)
            identities.append(f"{source_layer}:{source_key}")

        if len(revisions) != 1:
            raise RecoveryError("Workbook contains mixed or ambiguous AtOnce revision metadata.")
        if len(identities) != len(set(identities)):
            raise RecoveryError("Workbook contains duplicate immutable source lineage rows.")
        revision_number = next(iter(revisions))

        revision = next(
            (
                item
                for item in self.store.load_export_revisions(workflow.workflow_id)
                if item.revision_number == revision_number
                and item.lineage_generation == workflow.lineage_generation
            ),
            None,
        )
        if revision is None:
            raise RecoveryError(
                "Workbook belongs to an unavailable or superseded source-lineage generation."
            )
        baseline = next((item for item in revision.outputs if item.export_id == export.export_id), None)
        if baseline is None:
            raise RecoveryError("Recorded revision does not contain this linked export.")

        expected_identities = {row.identity for row in baseline.baseline_rows}
        if set(identities) != expected_identities:
            raise RecoveryError(
                "Workbook row lineage does not exactly match the recorded export baseline; inserted/deleted/tampered rows cannot be relinked."
            )

        comparison = compare_workbook_rows(
            baseline.baseline_rows,
            read.rows,
            read.fields,
            workflow.workflow_id,
            export.export_id,
            revision_number,
        )
        digest = file_sha256(str(workbook))
        export.path = candidate_path
        export.relink_revision_number = revision_number
        export.relink_sha256 = digest
        export.relink_modified = bool(comparison.changes)
        self.store.save_workflow(workflow)
        return WorkbookRelinkResult(
            workflow_id=workflow.workflow_id,
            export_id=export.export_id,
            path=candidate_path,
            revision_number=revision_number,
            contains_changes=bool(comparison.changes),
        )

    def _workflow(self, workflow_id):
        workflow = self.store.load_workflow(workflow_id)
        if workflow is None:
            raise RecoveryError("No registered AtOnce workflow is available for recovery.")
        return workflow

    def _current_expected_source_keys(self, workflow, source_lineage_id: str) -> Set[str]:
        """Return UUIDs from the newest authoritative state, not all historical rows."""

        load_state = getattr(self.store, "load_derived_state", None)
        state = load_state(workflow.workflow_id) if callable(load_state) else None
        if state is not None:
            keys = {
                row.source_key
                for row in state.rows
                if row.source_layer_id == source_lineage_id and row.source_key
            }
            if keys:
                return keys

        revisions = [
            item
            for item in self.store.load_export_revisions(workflow.workflow_id)
            if item.lineage_generation == workflow.lineage_generation
        ]
        for revision in reversed(revisions):
            keys = {
                row.source_key
                for row in revision.gpkg_baseline_rows
                if row.source_layer_id == source_lineage_id and row.source_key
            }
            if keys:
                return keys
            for output in revision.outputs:
                keys.update(
                    row.source_key
                    for row in output.baseline_rows
                    if row.source_layer_id == source_lineage_id and row.source_key
                )
            if keys:
                return keys
        return set()

    def _require_candidate_not_bound_elsewhere(self, workflow, source_lineage_id, candidate_id):
        conflict = next(
            (
                item
                for item in workflow.source_layers
                if item.stable_id != source_lineage_id and item.current_layer_id == candidate_id
            ),
            None,
        )
        if conflict is not None:
            raise RecoveryError(
                f"Candidate layer is already bound to another source role: {conflict.name}."
            )

    @staticmethod
    def _require_candidate_not_derived(workflow, candidate_id):
        derived = workflow.derived_layer
        if derived is not None and derived.layer_id == candidate_id:
            raise RecoveryError("Layer C cannot be relinked/replaced as an authoritative source.")

    def _require_mapped_field(self, workflow, source_lineage_id, candidate):
        mapping = workflow.primary_field_mapping
        if mapping is None:
            raise RecoveryError("Source recovery requires the confirmed business-key mapping.")
        if mapping.left_layer_id == source_lineage_id:
            field_name = mapping.left_field
        elif mapping.right_layer_id == source_lineage_id:
            field_name = mapping.right_field
        else:
            raise RecoveryError("Source recovery target is not represented in the confirmed mapping.")
        if not self.qgis.has_field(candidate, field_name):
            raise RecoveryError(
                f"Candidate source is missing the required mapped field {field_name!r}."
            )

    def _require_write_capability(self, candidate):
        method = getattr(self.qgis, "require_source_write_capability", None)
        try:
            if callable(method):
                method(candidate)
            else:
                require_source_write_capability(candidate)
        except Exception as exc:
            raise RecoveryError(str(exc)) from exc
