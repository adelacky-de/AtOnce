"""Authoritative forward propagation from registered sources into Layer C."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

from ..core.derived_diff import compare_derived_snapshots, format_derived_diff
from ..infrastructure.derived_runtime import layer_baseline_rows
from ..infrastructure.source_binding import StableSourceLayerView
from ..models.derived_state import DerivedState
from ..models.workflow import DERIVED_ROLE, WorkflowDefinition


class PropagationError(RuntimeError):
    """Raised when safe forward propagation cannot complete."""


class PropagationDivergenceError(PropagationError):
    """Raised when a rebuild would discard unreviewed manual Layer C changes."""

    def __init__(self, diff):
        self.diff = diff
        super().__init__(
            "Layer C differs from the recorded AtOnce state. "
            f"Review before rebuilding: {diff.summary()}"
        )

    @property
    def details(self):
        return format_derived_diff(self.diff)


@dataclass(frozen=True)
class PropagationResult:
    workflow_id: str
    derived_layer_id: str
    derived_feature_count: int
    assigned_source_keys: Dict[str, int]


class PropagationService:
    """Build Layer C only after workflow, schema and source identity preflight."""

    def __init__(self, store, qgis_gateway, workflow_service):
        self.store = store
        self.qgis = qgis_gateway
        self.workflow_service = workflow_service

    def check_derived_divergence(self, workflow_id: Optional[str] = None):
        workflow = self.store.load_workflow(workflow_id)
        if workflow is None:
            return None
        derived_ref = workflow.derived_layer
        if derived_ref is None or not derived_ref.layer_id:
            return None
        layer = self.qgis.resolve_layer(derived_ref.layer_id)
        if layer is None:
            return None
        load_state = getattr(self.store, "load_derived_state", None)
        state = load_state(workflow.workflow_id) if callable(load_state) else None
        if state is None:
            return None
        try:
            current = self._layer_baseline_rows(layer)
            diff = compare_derived_snapshots(state.rows, current)
        except Exception as exc:
            raise PropagationError(f"Could not inspect Layer C divergence safely: {exc}") from exc
        return diff if diff.has_changes else None

    def rebuild_layer_c(
        self,
        workflow_id: Optional[str] = None,
        *,
        approved_divergence=None,
    ) -> PropagationResult:
        workflow = self.store.load_workflow(workflow_id)
        if workflow is None:
            raise PropagationError("No registered AtOnce workflow is available for propagation.")

        validation = self.workflow_service.validate(workflow)
        if not validation.is_valid:
            errors = [issue.message for issue in validation.issues if issue.severity == "error"]
            raise PropagationError("Workflow validation failed: " + " | ".join(errors))

        current_diff = self.check_derived_divergence(workflow.workflow_id)
        if current_diff is not None:
            if approved_divergence is None or current_diff != approved_divergence:
                raise PropagationDivergenceError(current_diff)

        mapping = workflow.primary_field_mapping
        if mapping is None or not mapping.confirmed:
            raise PropagationError("A confirmed source business-key mapping is required.")

        sources = {layer.stable_id: layer for layer in workflow.source_layers if layer.stable_id}
        left_ref = sources.get(mapping.left_layer_id)
        right_ref = sources.get(mapping.right_layer_id)
        if left_ref is None or right_ref is None:
            raise PropagationError("The confirmed field mapping no longer matches both source identities.")

        left_layer = self.qgis.resolve_layer(left_ref.current_layer_id)
        right_layer = self.qgis.resolve_layer(right_ref.current_layer_id)
        if left_layer is None or right_layer is None:
            raise PropagationError("Both registered source layers must be loaded before propagation.")

        try:
            schema_plan = self.qgis.preflight_derived_schema(left_layer, right_layer, mapping)
            left_preflight = self.qgis.preflight_source_keys(left_layer)
            right_preflight = self.qgis.preflight_source_keys(right_layer)
        except Exception as exc:
            raise PropagationError(f"Propagation preflight failed: {exc}") from exc

        left_view = StableSourceLayerView(left_layer, left_ref.stable_id)
        right_view = StableSourceLayerView(right_layer, right_ref.stable_id)

        derived_ref = workflow.derived_layer
        derived_name = derived_ref.name if derived_ref else "Layer C"
        try:
            new_layer, feature_count = self.qgis.build_derived_memory_layer(
                workflow.workflow_id,
                derived_name,
                left_view,
                right_view,
                schema_plan,
                {
                    left_ref.stable_id: left_preflight.plan,
                    right_ref.stable_id: right_preflight.plan,
                },
            )
            derived_rows = self._layer_baseline_rows(new_layer)
        except Exception as exc:
            raise PropagationError(f"Layer C candidate build failed: {exc}") from exc

        assigned = {}
        try:
            assigned[left_ref.stable_id] = self.qgis.apply_source_key_plan(left_layer, left_preflight)
            assigned[right_ref.stable_id] = self.qgis.apply_source_key_plan(right_layer, right_preflight)

            left_verified = self.qgis.preflight_source_keys(left_layer)
            right_verified = self.qgis.preflight_source_keys(right_layer)
            if left_verified.plan.assignments or right_verified.plan.assignments:
                raise RuntimeError("One or more source UUID assignments did not persist.")
            if left_verified.plan.all_keys != left_preflight.plan.all_keys:
                raise RuntimeError("Persisted Source Layer A UUIDs differ from the preflight plan.")
            if right_verified.plan.all_keys != right_preflight.plan.all_keys:
                raise RuntimeError("Persisted Source Layer B UUIDs differ from the preflight plan.")
        except Exception as exc:
            raise PropagationError(f"Source UUID persistence failed: {exc}") from exc

        old_layer_id = workflow.derived_layer.layer_id if workflow.derived_layer else None
        try:
            self.qgis.replace_project_derived_layer(old_layer_id, new_layer)
            self._replace_derived_ref(workflow, new_layer)
            self.store.save_workflow(workflow)
            save_derived_state = getattr(self.store, "save_derived_state", None)
            if callable(save_derived_state):
                save_derived_state(
                    DerivedState(
                        workflow_id=workflow.workflow_id,
                        created_at=datetime.now(timezone.utc).isoformat(),
                        origin="rebuild",
                        rows=derived_rows,
                    )
                )
        except Exception as exc:
            raise PropagationError(f"Could not install rebuilt Layer C: {exc}") from exc

        return PropagationResult(
            workflow_id=workflow.workflow_id,
            derived_layer_id=new_layer.id(),
            derived_feature_count=feature_count,
            assigned_source_keys=assigned,
        )

    def _layer_baseline_rows(self, layer):
        method = getattr(self.qgis, "semantic_baseline_rows", None)
        if callable(method):
            return method(layer)
        return layer_baseline_rows(layer)

    def _replace_derived_ref(self, workflow: WorkflowDefinition, new_layer) -> None:
        replacement = self.qgis.make_layer_ref(new_layer, DERIVED_ROLE)
        for index, layer in enumerate(workflow.layers):
            if layer.role == DERIVED_ROLE:
                workflow.layers[index] = replacement
                return
        workflow.layers.append(replacement)
