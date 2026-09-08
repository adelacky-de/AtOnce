"""Dedicated executor for the explicit G7a/G7b/G7c graph slices."""

from datetime import datetime, timezone
from typing import Iterable, Optional
from uuid import uuid4

from ..core.derived_diff import compare_derived_snapshots
from ..core.graph_execution import (
    GraphExecutionError,
    GraphExecutionPlan,
    explicit_execution_config_fingerprint,
    plan_explicit_graph_execution,
)
from ..core.freeform_graph import (
    FreeformGraphError,
    _delivery_is_raster,
    plan_freeform_graph_execution,
)
from ..infrastructure.derived_runtime import layer_baseline_rows
from ..models.graph_node_materialization import GraphNodeMaterializationState
from ..models.graph_run_state import GraphRunState
from ..models.dependency_graph import NodeKind, OperationKind
from ..models.workflow import FREEFORM_GRAPH_ORIGIN

__all__ = [
    "GraphExecutionPlan",
    "GraphExecutionService",
    "GraphExecutionServiceError",
]


class GraphExecutionServiceError(RuntimeError):
    """Raised when an explicit graph run cannot complete safely."""


class GraphExecutionService:
    """Execute the supported explicit graph transforms as one safe run."""

    def __init__(self, store, qgis_gateway, workflow_service, export_service):
        self.store = store
        self.qgis = qgis_gateway
        self.workflow_service = workflow_service
        self.export_service = export_service

    def execute(
        self,
        workflow_id: Optional[str] = None,
        changed_source_lineage_ids: Iterable[str] = (),
        selected_edge_ids: Optional[Iterable[str]] = None,
    ):
        workflow = self.store.load_workflow(workflow_id)
        if workflow is None:
            raise GraphExecutionServiceError("No registered AtOnce workflow is available.")
        if workflow.effective_dependency_graph_origin() == FREEFORM_GRAPH_ORIGIN:
            return self._execute_freeform(
                workflow,
                changed_source_lineage_ids,
                selected_edge_ids,
            )
        try:
            plan = plan_explicit_graph_execution(
                workflow,
                changed_source_lineage_ids,
                selected_edge_ids,
            )
        except GraphExecutionError as exc:
            raise GraphExecutionServiceError(str(exc)) from exc

        validation = self.workflow_service.validate(workflow)
        if not validation.is_valid:
            errors = [issue.message for issue in validation.issues if issue.severity == "error"]
            raise GraphExecutionServiceError("Workflow validation failed: " + " | ".join(errors))

        run_id = str(uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        executable_steps = tuple(
            step
            for step in plan.transform_steps
            if step.target_node_id in plan.refreshed_node_ids
        )
        if not executable_steps:
            graph_state = self._record_run(workflow, plan, run_id, created_at, None)
            return {
                "workflow_id": workflow.workflow_id,
                "run_id": run_id,
                "derived_node_id": plan.derived_node_id,
                "derived_layer_id": None,
                "derived_feature_count": 0,
                "export_revision_number": None,
                "graph_run_state": graph_state,
            }

        graph = workflow.effective_dependency_graph()
        node_map = graph.node_map()
        parent_source_lineage_ids = plan.parent_source_lineage_ids or (plan.source_lineage_id,)
        parent_layers = []
        for lineage_id in parent_source_lineage_ids:
            source_ref = workflow.source_by_lineage_id(lineage_id)
            if source_ref is None:
                raise GraphExecutionServiceError(
                    f"Source lineage {lineage_id!r} is not registered in the workflow."
                )
            source_layer = self.qgis.resolve_layer(source_ref.current_layer_id)
            if source_layer is None:
                raise GraphExecutionServiceError(
                    f"Registered source binding for lineage {lineage_id!r} is missing."
                )
            if not self.qgis.is_vector_layer(source_layer):
                raise GraphExecutionServiceError(
                    f"Explicit {plan.operation.value.upper()} source {lineage_id!r} is not a vector layer."
                )
            parent_layers.append(source_layer)

        for source_layer in parent_layers:
            self._require_stable_source_keys(source_layer)

        states = {}
        old_layers = {}
        old_layer_ids = {}
        for step in executable_steps:
            state = self._load_state(workflow.workflow_id, step.target_node_id)
            states[step.target_node_id] = state
            old_layer_ids[step.target_node_id] = state.layer_id if state is not None else None
            old_layer = None
            if state is not None and state.layer_id:
                old_layer = self.qgis.resolve_layer(state.layer_id)
                if old_layer is not None:
                    self._check_divergence(node_map[step.target_node_id], state, old_layer)
            old_layers[step.target_node_id] = old_layer

        candidates = {}
        feature_counts = {}
        candidate_rows = {}
        for step in executable_steps:
            target = node_map[step.target_node_id]
            if step.operation.value == "filter":
                parent_layer = candidates.get(step.parent_node_id)
                if parent_layer is None:
                    parent_layer = parent_layers[0]
                expression = str(step.parameters.get("expression") or "").strip()
                self._validate_filter(expression, parent_layer)
                self._require_stable_source_keys(parent_layer)
                try:
                    candidate, feature_count = self.qgis.build_filtered_memory_layer(
                        workflow.workflow_id,
                        step.target_node_id,
                        target.name,
                        parent_layer,
                        plan.source_lineage_id,
                        expression,
                    )
                except Exception as exc:
                    raise GraphExecutionServiceError(
                        "FILTER candidate build failed: " + str(exc)
                    ) from exc
            else:
                try:
                    preflight = getattr(self.qgis, "preflight_merge_layers", None)
                    if callable(preflight):
                        preflight(parent_layers)
                    candidate, feature_count = self.qgis.build_merged_memory_layer(
                        workflow.workflow_id,
                        step.target_node_id,
                        target.name,
                        parent_layers,
                        parent_source_lineage_ids,
                    )
                except Exception as exc:
                    raise GraphExecutionServiceError(
                        "MERGE candidate build failed: " + str(exc)
                    ) from exc
            candidates[step.target_node_id] = candidate
            feature_counts[step.target_node_id] = feature_count
            try:
                candidate_rows[step.target_node_id] = self._layer_baseline_rows(candidate)
            except Exception as exc:
                raise GraphExecutionServiceError(
                    f"{step.operation.value.upper()} candidate baseline failed: {exc}"
                ) from exc

        installed = False
        output_revision_completed = False
        graph_run_recorded = False
        attempted_nodes = []
        try:
            for step in executable_steps:
                node_id = step.target_node_id
                attempted_nodes.append(node_id)
                candidate = candidates[node_id]
                self._install(old_layer_ids[node_id], candidate)
                installed = True
                new_state = GraphNodeMaterializationState(
                    workflow_id=workflow.workflow_id,
                    node_id=node_id,
                    layer_id=candidate.id(),
                    created_at=created_at,
                    origin=f"explicit-{step.operation.value}",
                    rows=candidate_rows[node_id],
                )
                self.store.save_graph_node_materialization(new_state)

            revision_number = None
            if plan.delivery_ids:
                final_candidate = candidates[executable_steps[-1].target_node_id]
                result = self.export_service.export_forward_deliveries_from_layer(
                    workflow,
                    final_candidate,
                    plan.delivery_ids,
                )
                revision_number = result.revision.revision_number
                output_revision_completed = True
            graph_state = self._record_run(
                workflow,
                plan,
                run_id,
                created_at,
                revision_number,
            )
            graph_run_recorded = True
            return {
                "workflow_id": workflow.workflow_id,
                "run_id": run_id,
                "derived_node_id": plan.derived_node_id,
                "derived_layer_id": candidates[executable_steps[-1].target_node_id].id(),
                "derived_feature_count": feature_counts[executable_steps[-1].target_node_id],
                "derived_layer_ids": {
                    node_id: candidates[node_id].id()
                    for node_id in candidates
                },
                "export_revision_number": revision_number,
                "graph_run_state": graph_state,
            }
        except Exception as exc:
            # Once the forward-delivery service returns, its output transaction
            # and ExportRevision are durable. Rolling only the derived node back
            # after that point would leave old upstream evidence beside new
            # outputs. Preserve the completed execution and make the missing
            # final graph evidence explicit instead.
            operation_label = (
                "FILTER chain"
                if len(executable_steps) > 1
                else plan.operation.value.upper()
            )
            if output_revision_completed:
                if graph_run_recorded:
                    message = (
                        f"Explicit {operation_label} execution and downstream revision completed, "
                        "but final graph-run finalization could not complete. "
                        "Do not retry this run blindly; inspect the completed revision "
                        "and materialization first."
                    )
                else:
                    message = (
                        f"Explicit {operation_label} execution and downstream revision completed, "
                        "but final graph-run evidence could not be persisted. "
                        "Do not retry this run blindly; inspect the completed revision "
                        "and materialization first."
                    )
                raise GraphExecutionServiceError(message) from exc
            if attempted_nodes:
                self._restore_materializations(
                    workflow.workflow_id,
                    attempted_nodes,
                    old_layers,
                    old_layer_ids,
                    states,
                    candidates,
                )
            if isinstance(exc, GraphExecutionServiceError):
                raise
            raise GraphExecutionServiceError(
                f"Explicit {operation_label} run failed and prior materialization was restored: {exc}"
            ) from exc

    def _execute_freeform(
        self,
        workflow,
        changed_source_lineage_ids,
        selected_edge_ids,
    ):
        """Execute a complete free-form graph through ordered candidates.

        This path is intentionally separate from the accepted G7 planner.  The
        registry decides which operation adapters are available; unsupported
        operations fail before any candidate is installed.
        """

        graph = workflow.effective_dependency_graph()
        source_nodes = {
            str(node.metadata.get("source_lineage_id") or ""): node.node_id
            for node in graph.nodes
            if node.kind == NodeKind.SOURCE
        }
        requested = {str(item) for item in changed_source_lineage_ids if str(item)}
        missing = requested - set(source_nodes)
        if missing:
            raise GraphExecutionServiceError(
                "Changed source lineage is not present in the free-form graph: "
                + ", ".join(sorted(missing))
            )
        validation = self.workflow_service.validate(workflow)
        if not validation.is_valid:
            errors = [issue.message for issue in validation.issues if issue.severity == "error"]
            raise GraphExecutionServiceError("Workflow validation failed: " + " | ".join(errors))
        try:
            plan = plan_freeform_graph_execution(
                graph,
                {source_nodes[lineage_id] for lineage_id in requested},
                selected_edge_ids,
            )
        except FreeformGraphError as exc:
            raise GraphExecutionServiceError(str(exc)) from exc

        node_map = graph.node_map()
        executable_steps = tuple(
            step
            for step in plan.transform_steps
            if step.node_id in plan.propagation.affected_node_ids
            and step.node_id not in plan.propagation.stale_by_choice_node_ids
        )
        if not executable_steps:
            state = self._record_freeform_run(workflow, plan, str(uuid4()), None)
            return {
                "workflow_id": workflow.workflow_id,
                "run_id": state.run_id,
                "derived_layer_ids": {},
                "export_revision_number": None,
                "graph_run_state": state,
            }

        # Preflight only source layers that the selected candidate chain will
        # actually consume.  An unrelated branch must remain unaffected by a
        # selective run, including when that branch still needs source-key
        # initialization or has an unavailable binding.
        required_source_node_ids = {
            edge.from_node
            for step in executable_steps
            for edge in step.input_edges
            if node_map.get(edge.from_node) is not None
            and node_map[edge.from_node].kind == NodeKind.SOURCE
        }
        source_layers = {}
        for source_node_id in sorted(required_source_node_ids):
            node = node_map[source_node_id]
            lineage_id = str(node.metadata.get("source_lineage_id") or "")
            source_ref = workflow.source_by_lineage_id(lineage_id)
            if source_ref is None:
                raise GraphExecutionServiceError(
                    f"Source node {node.node_id!r} has no registered lineage {lineage_id!r}."
                )
            layer = self.qgis.resolve_layer(source_ref.current_layer_id)
            if layer is None:
                raise GraphExecutionServiceError(
                    f"Registered free-form source '{source_ref.name}' is missing."
                )
            if getattr(self.qgis, "is_raster_layer", lambda _layer: False)(layer):
                source_layers[source_node_id] = layer
                continue
            if not self.qgis.is_vector_layer(layer):
                raise GraphExecutionServiceError(
                    f"Registered free-form source '{source_ref.name}' is missing or not supported data."
                )
            self._require_stable_source_keys(layer)
            source_layers[source_node_id] = layer

        created_at = datetime.now(timezone.utc).isoformat()
        states = {}
        old_layers = {}
        old_layer_ids = {}
        for step in executable_steps:
            state = self._load_state(workflow.workflow_id, step.node_id)
            states[step.node_id] = state
            old_layer_ids[step.node_id] = state.layer_id if state is not None else None
            old_layer = self.qgis.resolve_layer(state.layer_id) if state and state.layer_id else None
            if state is not None and old_layer is not None:
                self._check_divergence(node_map[step.node_id], state, old_layer)
            old_layers[step.node_id] = old_layer

        candidates = {}
        feature_counts = {}
        candidate_rows = {}
        for step in executable_steps:
            target = node_map[step.node_id]
            input_edges_by_port = step.input_edges_by_port or {
                "input": tuple(step.input_edges)
            }
            input_layers_by_port = {}
            input_lineages_by_port = {}
            for port_id, port_edges in input_edges_by_port.items():
                layers = []
                lineages = []
                for edge in port_edges:
                    parent = candidates.get(edge.from_node) or source_layers.get(edge.from_node)
                    if parent is None:
                        parent_state = self._load_state(workflow.workflow_id, edge.from_node)
                        if parent_state is not None:
                            parent = self.qgis.resolve_layer(parent_state.layer_id)
                    if parent is None:
                        raise GraphExecutionServiceError(
                            f"Upstream node {edge.from_node!r} is not materialized for {target.name!r}."
                        )
                    layers.append(parent)
                    parent_node = node_map[edge.from_node]
                    lineages.append(
                        str(parent_node.metadata.get("source_lineage_id") or "")
                        if parent_node.kind == NodeKind.SOURCE
                        else ""
                    )
                if len(layers) == 1:
                    input_layers_by_port[port_id] = layers[0]
                    input_lineages_by_port[port_id] = lineages[0]
                else:
                    input_layers_by_port[port_id] = tuple(layers)
                    input_lineages_by_port[port_id] = tuple(lineages)

            def one_input(port_id="input"):
                value = input_layers_by_port.get(port_id)
                if isinstance(value, tuple):
                    if len(value) != 1:
                        raise ValueError(
                            f"Operation {target.name!r} input {port_id!r} requires one layer."
                        )
                    return value[0]
                return value

            def one_lineage(port_id="input"):
                value = input_lineages_by_port.get(port_id, "")
                if isinstance(value, tuple):
                    return value[0] if value else ""
                return value

            operation = step.operation
            try:
                if operation == OperationKind.FILTER:
                    expression = str(step.parameters.get("expression") or "").strip()
                    self._validate_filter(expression, one_input())
                    candidate, count = self.qgis.build_filtered_memory_layer(
                        workflow.workflow_id,
                        step.node_id,
                        target.name,
                        one_input(),
                        one_lineage(),
                        expression,
                    )
                elif operation == OperationKind.PASSTHROUGH:
                    candidate, count = self.qgis.build_passthrough_memory_layer(
                        workflow.workflow_id,
                        step.node_id,
                        target.name,
                        one_input(),
                        one_lineage(),
                    )
                elif operation == OperationKind.MERGE:
                    merge_layers = tuple(input_layers_by_port.get("input", ()))
                    merge_lineages = tuple(input_lineages_by_port.get("input", ()))
                    if len(merge_layers) < 2:
                        raise ValueError(
                            "MERGE requires at least two inputs."
                        )
                    candidate, count = self.qgis.build_merged_memory_layer(
                        workflow.workflow_id,
                        step.node_id,
                        target.name,
                        merge_layers,
                        merge_lineages,
                    )
                elif operation == OperationKind.JOIN:
                    join_layers = (
                        input_layers_by_port["left"],
                        input_layers_by_port["right"],
                    )
                    join_lineages = (
                        input_lineages_by_port.get("left", ""),
                        input_lineages_by_port.get("right", ""),
                    )
                    candidate, count = self.qgis.build_join_memory_layer(
                        workflow.workflow_id,
                        step.node_id,
                        target.name,
                        join_layers,
                        join_lineages,
                        step.parameters,
                    )
                elif operation == OperationKind.COMPARE_CHANGES:
                    compare_layers = (
                        input_layers_by_port["previous"],
                        input_layers_by_port["current"],
                    )
                    compare_lineages = (
                        input_lineages_by_port.get("previous", ""),
                        input_lineages_by_port.get("current", ""),
                    )
                    candidate, count = self.qgis.build_compare_changes_memory_layer(
                        workflow.workflow_id,
                        step.node_id,
                        target.name,
                        compare_layers,
                        compare_lineages,
                        step.parameters,
                    )
                elif operation == OperationKind.CALCULATE_FIELD:
                    candidate, count = self.qgis.build_calculate_field_memory_layer(
                        workflow.workflow_id,
                        step.node_id,
                        target.name,
                        one_input(),
                        one_lineage(),
                        step.parameters,
                    )
                elif operation == OperationKind.FIELD_MAPPING:
                    candidate, count = self.qgis.build_field_mapping_memory_layer(
                        workflow.workflow_id,
                        step.node_id,
                        target.name,
                        one_input(),
                        one_lineage(),
                        step.parameters,
                    )
                elif operation == OperationKind.KEEP_FIELDS:
                    candidate, count = self.qgis.build_keep_fields_memory_layer(
                        workflow.workflow_id,
                        step.node_id,
                        target.name,
                        one_input(),
                        one_lineage(),
                        step.parameters,
                    )
                elif operation == OperationKind.RENAME_FIELD:
                    candidate, count = self.qgis.build_rename_field_memory_layer(
                        workflow.workflow_id,
                        step.node_id,
                        target.name,
                        one_input(),
                        one_lineage(),
                        step.parameters,
                    )
                elif operation == OperationKind.CHANGE_FIELD_TYPE:
                    candidate, count = self.qgis.build_change_field_type_memory_layer(
                        workflow.workflow_id,
                        step.node_id,
                        target.name,
                        one_input(),
                        one_lineage(),
                        step.parameters,
                    )
                elif operation == OperationKind.REMOVE_DUPLICATES:
                    candidate, count = self.qgis.build_remove_duplicates_memory_layer(
                        workflow.workflow_id,
                        step.node_id,
                        target.name,
                        one_input(),
                        one_lineage(),
                        step.parameters,
                    )
                elif operation == OperationKind.SORT:
                    candidate, count = self.qgis.build_sort_memory_layer(
                        workflow.workflow_id,
                        step.node_id,
                        target.name,
                        one_input(),
                        one_lineage(),
                        step.parameters,
                    )
                elif operation == OperationKind.AGGREGATE:
                    candidate, count = self.qgis.build_aggregate_memory_layer(
                        workflow.workflow_id,
                        step.node_id,
                        target.name,
                        one_input(),
                        one_lineage(),
                        step.parameters,
                    )
                elif operation == OperationKind.SELECT_BY_LOCATION:
                    select_layers = (
                        input_layers_by_port["target"],
                        input_layers_by_port["predicate"],
                    )
                    select_lineages = (
                        input_lineages_by_port.get("target", ""),
                        input_lineages_by_port.get("predicate", ""),
                    )
                    candidate, count = self.qgis.build_select_by_location_memory_layer(
                        workflow.workflow_id,
                        step.node_id,
                        target.name,
                        select_layers,
                        select_lineages,
                        step.parameters,
                    )
                elif operation == OperationKind.SPATIAL_JOIN:
                    spatial_join_layers = (
                        input_layers_by_port["target"],
                        input_layers_by_port["join"],
                    )
                    spatial_join_lineages = (
                        input_lineages_by_port.get("target", ""),
                        input_lineages_by_port.get("join", ""),
                    )
                    candidate, count = self.qgis.build_spatial_join_memory_layer(
                        workflow.workflow_id,
                        step.node_id,
                        target.name,
                        spatial_join_layers,
                        spatial_join_lineages,
                        step.parameters,
                    )
                elif operation == OperationKind.CLIP:
                    clip_layers = (
                        input_layers_by_port["input"],
                        input_layers_by_port["overlay"],
                    )
                    clip_lineages = (
                        input_lineages_by_port.get("input", ""),
                        input_lineages_by_port.get("overlay", ""),
                    )
                    candidate, count = self.qgis.build_clip_memory_layer(
                        workflow.workflow_id,
                        step.node_id,
                        target.name,
                        clip_layers,
                        clip_lineages,
                        step.parameters,
                    )
                elif operation == OperationKind.BUFFER:
                    candidate, count = self.qgis.build_buffer_memory_layer(
                        workflow.workflow_id,
                        step.node_id,
                        target.name,
                        one_input(),
                        one_lineage(),
                        step.parameters,
                    )
                elif operation == OperationKind.DISSOLVE:
                    candidate, count = self.qgis.build_dissolve_memory_layer(
                        workflow.workflow_id,
                        step.node_id,
                        target.name,
                        one_input(),
                        one_lineage(),
                        step.parameters,
                    )
                elif operation == OperationKind.REPROJECT:
                    candidate, count = self.qgis.build_reproject_memory_layer(
                        workflow.workflow_id,
                        step.node_id,
                        target.name,
                        one_input(),
                        one_lineage(),
                        step.parameters,
                    )
                elif operation == OperationKind.RASTER_REPROJECT:
                    parent = one_input()
                    if not getattr(self.qgis, "is_raster_layer", lambda _layer: False)(parent):
                        raise ValueError("RASTER_REPROJECT requires a raster input.")
                    candidate, count = self.qgis.build_reprojected_raster_layer(
                        workflow.workflow_id,
                        step.node_id,
                        target.name,
                        parent,
                        step.parameters,
                    )
                elif operation == OperationKind.RASTER_CONVERT:
                    parent = one_input()
                    if not getattr(self.qgis, "is_raster_layer", lambda _layer: False)(parent):
                        raise ValueError("RASTER_CONVERT requires a raster input.")
                    candidate, count = self.qgis.build_converted_raster_layer(
                        workflow.workflow_id,
                        step.node_id,
                        target.name,
                        parent,
                        step.parameters,
                    )
                else:
                    raise ValueError(
                        f"{operation.value.upper()} is registered but its QGIS executor is not enabled yet."
                    )
            except Exception as exc:
                raise GraphExecutionServiceError(
                    f"{operation.value.upper()} candidate build failed: {exc}"
                ) from exc
            candidates[step.node_id] = candidate
            feature_counts[step.node_id] = count
            try:
                if getattr(self.qgis, "is_raster_layer", lambda _layer: False)(candidate):
                    candidate_rows[step.node_id] = []
                else:
                    candidate_rows[step.node_id] = self._layer_baseline_rows(candidate)
            except Exception as exc:
                raise GraphExecutionServiceError(
                    f"{operation.value.upper()} candidate baseline failed: {exc}"
                ) from exc

        output_revision_completed = False
        attempted_nodes = []
        run_id = str(uuid4())
        try:
            for step in executable_steps:
                attempted_nodes.append(step.node_id)
                candidate = candidates[step.node_id]
                self._install(old_layer_ids[step.node_id], candidate)
                self.store.save_graph_node_materialization(
                    GraphNodeMaterializationState(
                        workflow_id=workflow.workflow_id,
                        node_id=step.node_id,
                        layer_id=candidate.id(),
                        created_at=created_at,
                        origin=f"freeform-{step.operation.value}",
                        rows=candidate_rows[step.node_id],
                    )
                )

            layers_by_delivery = {}
            delivery_ids = []
            missed_raster = []
            for step in executable_steps:
                for edge in graph.downstream_edges(step.node_id):
                    if edge.to_node in plan.propagation.stale_by_choice_node_ids:
                        continue
                    target = node_map[edge.to_node]
                    if target.kind != NodeKind.DELIVERY:
                        continue
                    if edge.edge_id not in plan.propagation.selected_edge_ids:
                        if _delivery_is_raster(graph, target):
                            missed_raster.append(target.name or target.node_id)
                        continue
                    delivery_id = str(target.metadata.get("delivery_id") or "")
                    if delivery_id:
                        layers_by_delivery[delivery_id] = candidates[step.node_id]
                        delivery_ids.append(delivery_id)

            if missed_raster:
                raise GraphExecutionServiceError(
                    "Raster OUTPUT was not selected for export: "
                    + ", ".join(missed_raster)
                    + ". Raster deliveries are written on Register/Run and must not be skipped."
                )

            selected_delivery_without_id = []
            for step in executable_steps:
                for edge in graph.downstream_edges(step.node_id):
                    if edge.edge_id not in plan.propagation.selected_edge_ids:
                        continue
                    if edge.to_node in plan.propagation.stale_by_choice_node_ids:
                        continue
                    target = node_map[edge.to_node]
                    if target.kind != NodeKind.DELIVERY:
                        continue
                    if not str(target.metadata.get("delivery_id") or ""):
                        selected_delivery_without_id.append(target.name or target.node_id)
            if selected_delivery_without_id:
                raise GraphExecutionServiceError(
                    "OUTPUT is selected but has no delivery_id; cannot write the file: "
                    + ", ".join(selected_delivery_without_id)
                )

            revision_number = None
            if delivery_ids:
                exporter = getattr(self.export_service, "export_forward_deliveries_from_layers", None)
                if not callable(exporter):
                    raise GraphExecutionServiceError(
                        "The export service cannot atomically export free-form terminal branches."
                    )
                result = exporter(workflow, layers_by_delivery, delivery_ids)
                revision_number = result.revision.revision_number
                output_revision_completed = True
            state = self._record_freeform_run(workflow, plan, run_id, revision_number)
            return {
                "workflow_id": workflow.workflow_id,
                "run_id": run_id,
                "derived_layer_ids": {node_id: layer.id() for node_id, layer in candidates.items()},
                "derived_feature_counts": feature_counts,
                "export_revision_number": revision_number,
                "graph_run_state": state,
            }
        except Exception as exc:
            if output_revision_completed:
                raise GraphExecutionServiceError(
                    "Free-form execution and downstream revision completed, but final graph-run "
                    "evidence could not be persisted. Do not retry blindly; inspect the completed "
                    "revision and materialization first."
                ) from exc
            if attempted_nodes:
                self._restore_materializations(
                    workflow.workflow_id,
                    attempted_nodes,
                    old_layers,
                    old_layer_ids,
                    states,
                    candidates,
                )
            raise GraphExecutionServiceError(
                "Free-form run failed and prior materialization was restored: " + str(exc)
            ) from exc

    def _record_freeform_run(self, workflow, plan, run_id, revision_number):
        refreshed = set()
        for step in plan.transform_steps:
            if (
                step.node_id in plan.propagation.affected_node_ids
                and step.node_id not in plan.propagation.stale_by_choice_node_ids
            ):
                refreshed.add(step.node_id)
        if revision_number is not None:
            refreshed.update(
                edge.to_node
                for edge in plan.graph.edges
                if edge.edge_id in plan.propagation.selected_edge_ids
                and edge.to_node in {item.node_id for item in plan.graph.nodes if item.kind == NodeKind.DELIVERY}
                and edge.to_node not in plan.propagation.stale_by_choice_node_ids
            )
        state = GraphRunState(
            workflow_id=workflow.workflow_id,
            run_id=run_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            reason="freeform-dag-run",
            changed_node_ids=list(plan.propagation.changed_node_ids),
            selected_edge_ids=list(plan.propagation.selected_edge_ids),
            skipped_edge_ids=list(plan.propagation.skipped_edge_ids),
            refreshed_node_ids=sorted(refreshed),
            stale_node_ids=list(plan.propagation.stale_by_choice_node_ids),
            export_revision_number=revision_number,
            source_sync_id="",
            workflow_config_fingerprint=explicit_execution_config_fingerprint(workflow),
        )
        self.store.save_graph_run_state(state)
        return state

    def _validate_filter(self, expression, source_layer):
        if not expression:
            raise GraphExecutionServiceError("FILTER expression is required.")
        validator = getattr(self.qgis, "validate_expression", None)
        if not callable(validator):
            return
        try:
            valid, error = validator(expression, source_layer)
        except TypeError:
            valid, error = validator(expression)
        if not valid:
            raise GraphExecutionServiceError(f"Invalid FILTER expression: {error}")

    def _require_stable_source_keys(self, source_layer):
        method = getattr(self.qgis, "preflight_existing_source_keys", None)
        if callable(method):
            try:
                method(source_layer)
            except Exception as exc:
                raise GraphExecutionServiceError(str(exc)) from exc

    def _layer_baseline_rows(self, layer):
        method = getattr(self.qgis, "semantic_baseline_rows", None)
        if callable(method):
            return method(layer)
        return layer_baseline_rows(layer)

    def _load_state(self, workflow_id, node_id):
        loader = getattr(self.store, "load_graph_node_materialization", None)
        if not callable(loader):
            raise GraphExecutionServiceError("ProjectStore does not support explicit node state.")
        return loader(workflow_id, node_id)

    def _check_divergence(self, node, state, layer):
        if getattr(self.qgis, "is_raster_layer", lambda _layer: False)(layer):
            return
        try:
            current_rows = self._layer_baseline_rows(layer)
            diff = compare_derived_snapshots(state.rows, current_rows)
        except Exception as exc:
            raise GraphExecutionServiceError(
                f"Could not inspect explicit derived node '{node.node_id}' safely: {exc}"
            ) from exc
        if diff.has_changes:
            raise GraphExecutionServiceError(
                f"Explicit derived node '{node.name}' ({node.node_id}) has manual changes. "
                f"Rebuild is blocked: {diff.summary()}"
            )

    def _install(self, old_layer_id, candidate):
        method = getattr(self.qgis, "replace_graph_node_layer", None)
        if callable(method):
            method(old_layer_id, candidate)
            return
        project = getattr(self.qgis, "project", None)
        if project is not None:
            if project.addMapLayer(candidate) is None:
                raise GraphExecutionServiceError(
                    "Could not add the explicit derived layer to the QGIS project."
                )
            if old_layer_id and project.mapLayer(old_layer_id):
                project.removeMapLayer(old_layer_id)
            return
        raise GraphExecutionServiceError("QGIS gateway cannot install an explicit graph layer.")

    def _restore(self, old_layer, candidate, old_layer_id, workflow_id, node_id, old_state):
        restore = getattr(self.qgis, "restore_graph_node_layer", None)
        if callable(restore):
            restore(old_layer, candidate.id())
        else:
            remove = getattr(self.qgis, "remove_project_layer", None)
            if callable(remove):
                remove(candidate.id())
            if old_layer is not None:
                project = getattr(self.qgis, "project", None)
                if project is not None and project.mapLayer(old_layer.id()) is None:
                    project.addMapLayer(old_layer)
        if old_state is None:
            delete = getattr(self.store, "delete_graph_node_materialization", None)
            if callable(delete):
                delete(workflow_id, node_id)
        else:
            self.store.save_graph_node_materialization(old_state)

    def _restore_materializations(
        self,
        workflow_id,
        attempted_nodes,
        old_layers,
        old_layer_ids,
        old_states,
        candidates,
    ):
        for node_id in reversed(attempted_nodes):
            self._restore(
                old_layers.get(node_id),
                candidates[node_id],
                old_layer_ids.get(node_id),
                workflow_id,
                node_id,
                old_states.get(node_id),
            )

    def _record_run(self, workflow, plan, run_id, created_at, revision_number):
        state = GraphRunState(
            workflow_id=workflow.workflow_id,
            run_id=run_id,
            created_at=created_at,
            reason=(
                "explicit-filter-chain-run"
                if len(plan.transform_steps) > 1
                else f"explicit-{plan.operation.value}-run"
            ),
            changed_node_ids=list(plan.propagation.changed_node_ids),
            selected_edge_ids=list(plan.propagation.selected_edge_ids),
            skipped_edge_ids=list(plan.propagation.skipped_edge_ids),
            refreshed_node_ids=list(plan.refreshed_node_ids),
            stale_node_ids=list(plan.propagation.stale_by_choice_node_ids),
            export_revision_number=revision_number,
            source_sync_id="",
            workflow_config_fingerprint=explicit_execution_config_fingerprint(workflow),
        )
        self.store.save_graph_run_state(state)
        return state
