"""Workflow registration and validation service."""

from dataclasses import dataclass, field
from typing import List

from ..core.graph_execution import (
    GraphExecutionError,
    explicit_execution_config_fingerprint,
    plan_explicit_graph_execution,
)
from ..core.freeform_graph import validate_freeform_graph
from ..core.operation_registry import DEFAULT_OPERATION_REGISTRY, InferredSchema, SchemaField
from ..models.dependency_graph import NodeKind, OperationKind
from ..models.workflow import (
    DERIVED_ROLE,
    EXPLICIT_GRAPH_ORIGIN,
    FREEFORM_GRAPH_ORIGIN,
    SAME_BUSINESS_KEY,
    SOURCE_ROLE,
    WorkflowDefinition,
)


ERROR = "error"
WARNING = "warning"


@dataclass
class ValidationIssue:
    code: str
    message: str
    severity: str = ERROR


@dataclass
class ValidationResult:
    issues: List[ValidationIssue] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not any(issue.severity == ERROR for issue in self.issues)

    @property
    def summary(self) -> str:
        if self.is_valid and not self.issues:
            return "Workflow is valid"
        if self.is_valid:
            return "Workflow is valid with warnings"
        return "Workflow has validation errors"


@dataclass(frozen=True)
class ExplicitFreshness:
    state: str
    label: str
    current_fingerprint: str
    latest_fingerprint: str = ""
    latest_run: object = None

    @property
    def is_current(self) -> bool:
        return self.state == "current"


class WorkflowService:
    def __init__(self, store, qgis_gateway):
        self.store = store
        self.qgis = qgis_gateway

    def register(self, definition: WorkflowDefinition) -> ValidationResult:
        existing = self.store.load_workflow(definition.workflow_id)
        if existing is not None:
            old_ids = {item.stable_id for item in existing.source_layers}
            new_ids = {item.stable_id for item in definition.source_layers}
            freeform_edit = (
                existing.effective_dependency_graph_origin() == FREEFORM_GRAPH_ORIGIN
                and definition.effective_dependency_graph_origin() == FREEFORM_GRAPH_ORIGIN
            )
            if old_ids != new_ids and not freeform_edit:
                return ValidationResult(
                    [
                        ValidationIssue(
                            "source_identity_change_requires_replace",
                            "Source lineage identity cannot be changed through Edit workflow. "
                            "Use Relink Source for the same lineage or Replace Source for a new authoritative dataset.",
                        )
                    ]
                )

        result = self.validate(definition)
        if result.is_valid:
            self.store.save_workflow(definition)
            set_active = getattr(self.store, "set_active_workflow_id", None)
            if callable(set_active):
                set_active(definition.workflow_id)
        return result

    def load_primary(self):
        return self.store.load_workflow()

    def load_all(self):
        loader = getattr(self.store, "load_workflows", None)
        if callable(loader):
            return loader()
        primary = self.store.load_workflow()
        return [primary] if primary is not None else []

    def load_active(self):
        loader = getattr(self.store, "load_active_workflow", None)
        if callable(loader):
            return loader()
        return self.load_primary()

    def set_active(self, workflow_id):
        setter = getattr(self.store, "set_active_workflow_id", None)
        if not callable(setter):
            workflow = self.store.load_workflow(workflow_id)
            if workflow is None:
                raise ValueError(f"Workflow {workflow_id!r} is not registered.")
            return workflow
        setter(workflow_id)
        loader = getattr(self.store, "load_active_workflow", None)
        return loader() if callable(loader) else self.store.load_workflow(workflow_id)

    def explicit_freshness(self, definition):
        if definition.effective_dependency_graph_origin() not in {
            EXPLICIT_GRAPH_ORIGIN,
            FREEFORM_GRAPH_ORIGIN,
        }:
            return None
        current = explicit_execution_config_fingerprint(definition)
        latest = self.store.latest_graph_run_state(definition.workflow_id)
        latest_fingerprint = str(
            getattr(latest, "workflow_config_fingerprint", "") or ""
        )
        if latest is None:
            return ExplicitFreshness("not_run", "Not run yet · run required", current)
        if not latest_fingerprint:
            return ExplicitFreshness(
                "pre_g8b",
                "Run required to confirm current configuration",
                current,
                latest_run=latest,
            )
        if latest_fingerprint == current:
            return ExplicitFreshness(
                "current",
                "Current relative to latest run",
                current,
                latest_fingerprint,
                latest,
            )
        return ExplicitFreshness(
            "changed",
            "Configuration changed · run required",
            current,
            latest_fingerprint,
            latest,
        )

    @staticmethod
    def type_label(definition):
        """Return a user-facing workflow type without exposing internal IDs."""

        if definition.effective_dependency_graph_origin() not in {
            EXPLICIT_GRAPH_ORIGIN,
            FREEFORM_GRAPH_ORIGIN,
        }:
            return "Legacy A/B workflow"
        graph = definition.effective_dependency_graph()
        derived_ids = {
            node.node_id for node in graph.nodes if node.kind == NodeKind.DERIVED
        }
        transforms = [
            edge
            for edge in graph.edges
            if edge.to_node in derived_ids
        ]
        if len(derived_ids) == 2 and transforms and all(
            edge.operation == OperationKind.FILTER for edge in transforms
        ):
            return "FILTER chain"
        if any(edge.operation == OperationKind.MERGE for edge in transforms):
            return "MERGE"
        if any(edge.operation == OperationKind.FILTER for edge in transforms):
            return "FILTER"
        return "Explicit workflow"

    def validate(self, definition: WorkflowDefinition) -> ValidationResult:
        if definition.effective_dependency_graph_origin() in {
            EXPLICIT_GRAPH_ORIGIN,
            FREEFORM_GRAPH_ORIGIN,
        }:
            return self._validate_explicit(definition)
        issues = self.validate_structure(definition).issues

        for source in definition.source_layers:
            layer = self.qgis.resolve_layer(source.current_layer_id)
            if layer is None:
                issues.append(
                    ValidationIssue(
                        "source_missing",
                        f"Source layer '{source.name}' is missing from the current QGIS project. "
                        "Relink the same source or explicitly replace the authoritative source.",
                    )
                )
                continue
            if not self.qgis.is_vector_layer(layer):
                issues.append(
                    ValidationIssue(
                        "source_not_vector",
                        f"Source layer '{source.name}' is not a vector layer.",
                    )
                )
            if source.source_uri and layer.source() != source.source_uri:
                issues.append(
                    ValidationIssue(
                        "source_uri_changed",
                        f"Source layer '{source.name}' has a different data source URI than when bound. "
                        "Use Relink Source to confirm moved/reloaded lineage.",
                        WARNING,
                    )
                )

        mapping = definition.primary_field_mapping
        if mapping is not None:
            left_ref = definition.source_by_lineage_id(mapping.left_layer_id)
            right_ref = definition.source_by_lineage_id(mapping.right_layer_id)
            left_layer = self.qgis.resolve_layer(left_ref.current_layer_id) if left_ref else None
            right_layer = self.qgis.resolve_layer(right_ref.current_layer_id) if right_ref else None

            if left_layer is not None and not self.qgis.has_field(left_layer, mapping.left_field):
                issues.append(
                    ValidationIssue(
                        "mapping_left_field_missing",
                        f"Mapped field '{mapping.left_field}' no longer exists on the first source layer.",
                    )
                )
            if right_layer is not None and not self.qgis.has_field(right_layer, mapping.right_field):
                issues.append(
                    ValidationIssue(
                        "mapping_right_field_missing",
                        f"Mapped field '{mapping.right_field}' no longer exists on the second source layer.",
                    )
                )

        derived = definition.derived_layer
        if derived and derived.layer_id:
            layer = self.qgis.resolve_layer(derived.layer_id)
            if layer is None:
                issues.append(
                    ValidationIssue(
                        "derived_missing",
                        f"Derived layer '{derived.name}' is missing from the current QGIS project. "
                        "Rebuild Layer C from the registered sources.",
                        WARNING,
                    )
                )
            elif not self.qgis.is_vector_layer(layer):
                issues.append(
                    ValidationIssue(
                        "derived_not_vector",
                        f"Derived layer '{derived.name}' is not a vector layer.",
                    )
                )

        for export in definition.exports:
            ok, error = self.qgis.validate_expression(export.filter_expression)
            if not ok:
                issues.append(
                    ValidationIssue(
                        "invalid_filter",
                        f"{export.name} has an invalid QGIS filter expression: {error}",
                    )
                )

        return ValidationResult(issues)

    def _validate_explicit(self, definition: WorkflowDefinition) -> ValidationResult:
        if definition.effective_dependency_graph_origin() == FREEFORM_GRAPH_ORIGIN:
            return self._validate_freeform(definition)
        issues = self.validate_explicit_structure(definition).issues
        for source in definition.source_layers:
            layer = self.qgis.resolve_layer(source.current_layer_id)
            if layer is None:
                issues.append(
                    ValidationIssue(
                        "source_missing",
                        f"Source layer '{source.name}' is missing from the current QGIS project.",
                    )
                )
            elif not self.qgis.is_vector_layer(layer):
                issues.append(
                    ValidationIssue(
                        "source_not_vector",
                        f"Source layer '{source.name}' is not a vector layer.",
                    )
                )

        graph = definition.effective_dependency_graph()
        node_map = graph.node_map()
        merge_edges = [
            edge
            for edge in graph.edges
            if edge.operation == OperationKind.MERGE
            and edge.to_node in node_map
            and node_map[edge.to_node].kind == NodeKind.DERIVED
        ]
        merge_preflight = getattr(self.qgis, "preflight_merge_layers", None)
        if merge_edges and callable(merge_preflight):
            merge_layers = []
            for edge in merge_edges:
                parent = node_map.get(edge.from_node)
                lineage_id = str(parent.metadata.get("source_lineage_id") or "") if parent else ""
                source_ref = definition.source_by_lineage_id(lineage_id)
                layer = self.qgis.resolve_layer(source_ref.current_layer_id) if source_ref else None
                if layer is None:
                    merge_layers = []
                    break
                merge_layers.append(layer)
            if len(merge_layers) == len(merge_edges):
                try:
                    merge_preflight(merge_layers)
                except Exception as exc:
                    issues.append(
                        ValidationIssue(
                            "merge_compatibility",
                            f"MERGE source compatibility check failed: {exc}",
                        )
                    )
        for edge in graph.edges:
            if (
                edge.to_node not in node_map
                or node_map[edge.to_node].kind.value != "derived"
                or edge.operation.value != "filter"
            ):
                continue
            source_ref = self._filter_source_ref(definition, graph, edge.from_node)
            layer = self.qgis.resolve_layer(source_ref.current_layer_id) if source_ref else None
            ok, error = self._validate_expression(edge.parameters.get("expression", ""), layer)
            if not ok:
                issues.append(
                    ValidationIssue(
                        "invalid_graph_filter",
                        f"{edge.edge_id} has an invalid FILTER expression: {error}",
                    )
                )
        return ValidationResult(issues)

    def _validate_freeform(self, definition: WorkflowDefinition) -> ValidationResult:
        issues: List[ValidationIssue] = []
        if not definition.workflow_id.strip():
            issues.append(ValidationIssue("workflow_id_missing", "Workflow ID is missing."))
        if not definition.name.strip():
            issues.append(ValidationIssue("workflow_name_missing", "Workflow name is required."))
        for error in validate_freeform_graph(
            definition.effective_dependency_graph(), require_complete=True
        ):
            issues.append(ValidationIssue("freeform_graph", error))
        graph = definition.effective_dependency_graph()
        graph_sources = [node for node in graph.nodes if node.kind == NodeKind.SOURCE]
        registered = {item.stable_id for item in definition.source_layers if item.stable_id}
        graph_lineage_values = [
            str(node.metadata.get("source_lineage_id") or "") for node in graph_sources
        ]
        if len([value for value in graph_lineage_values if value]) != len(
            {value for value in graph_lineage_values if value}
        ):
            issues.append(
                ValidationIssue(
                    "duplicate_source_lineage",
                    "Free-form SOURCE blocks must use unique source lineage identities.",
                )
            )
        graph_lineages = {
            value for value in graph_lineage_values if value
        }
        if graph_lineages - registered:
            issues.append(
                ValidationIssue(
                    "freeform_source_identity",
                    "Every free-form SOURCE block must reference a registered source lineage.",
                )
            )
        if registered - graph_lineages:
            issues.append(
                ValidationIssue(
                    "freeform_source_identity",
                    "Every registered source must have a corresponding SOURCE block.",
                )
            )
        resolved_layers = {}
        for source in definition.source_layers:
            layer = self.qgis.resolve_layer(source.current_layer_id)
            source_node = next(
                (
                    node
                    for node in graph_sources
                    if str(node.metadata.get("source_lineage_id") or "") == source.stable_id
                ),
                None,
            )
            if source_node is not None and layer is not None:
                resolved_layers[source_node.node_id] = layer
            if layer is None:
                issues.append(
                    ValidationIssue(
                        "source_missing",
                        f"Source layer '{source.name}' is missing from the current QGIS project.",
                    )
                )
            elif getattr(self.qgis, "is_raster_layer", lambda _layer: False)(layer):
                if source_node is not None:
                    data_type = str(source_node.metadata.get("data_type") or "raster")
                    if data_type != "raster":
                        issues.append(
                            ValidationIssue(
                                "source_data_type",
                                f"Source layer '{source.name}' is raster but marked as {data_type}.",
                            )
                        )
            elif not self.qgis.is_vector_layer(layer):
                issues.append(
                    ValidationIssue(
                        "source_not_supported",
                        f"Source layer '{source.name}' must be a loaded vector or raster layer.",
                    )
                )
        schema_by_node = {}
        for source_node in graph_sources:
            layer = resolved_layers.get(source_node.node_id)
            if layer is not None and self.qgis.is_vector_layer(layer):
                field_specs = getattr(self.qgis, "field_specs", None)
                if callable(field_specs):
                    schema_by_node[source_node.node_id] = tuple(field_specs(layer))
                else:
                    schema_by_node[source_node.node_id] = tuple(
                        SchemaField(field.name(), str(field.type()))
                        for field in layer.fields()
                    )

        try:
            ordered_nodes = graph.topological_order()
        except ValueError:
            ordered_nodes = [node.node_id for node in graph.nodes]
        node_map = graph.node_map()
        for node_id in ordered_nodes:
            node = node_map.get(node_id)
            if node is None or node.kind != NodeKind.DERIVED:
                continue
            if node.kind != NodeKind.DERIVED:
                continue
            operation = str(node.metadata.get("operation_kind") or "")
            operation_definition = DEFAULT_OPERATION_REGISTRY.get(operation)
            if operation_definition is None:
                continue
            # Resolve lane data type from SOURCE ancestors for accepted_data_types.
            data_type = "vector"
            frontier = [node.node_id]
            seen = set()
            while frontier:
                current = frontier.pop(0)
                if current in seen:
                    continue
                seen.add(current)
                current_node = node_map.get(current)
                if current_node is None:
                    continue
                if current_node.kind == NodeKind.SOURCE:
                    data_type = str(current_node.metadata.get("data_type") or "vector")
                    break
                for edge in graph.incoming_edges(current):
                    frontier.append(edge.from_node)
            accepted = tuple(getattr(operation_definition, "accepted_data_types", ("vector",)) or ("vector",))
            if data_type not in accepted:
                issues.append(
                    ValidationIssue(
                        "operation_data_type",
                        f"{node.name}: {operation_definition.title} accepts "
                        f"{', '.join(accepted)} inputs, not {data_type}.",
                    )
                )
            inputs = {}
            input_schemas = {}
            for edge in graph.incoming_edges(node.node_id):
                port = str(edge.parameters.get("target_port") or "input")
                parent = resolved_layers.get(edge.from_node)
                if parent is None:
                    state_loader = getattr(self.store, "load_graph_node_materialization", None)
                    state = state_loader(definition.workflow_id, edge.from_node) if callable(state_loader) else None
                    if state is not None:
                        parent = self.qgis.resolve_layer(state.layer_id)
                if parent is None and edge.from_node in schema_by_node:
                    parent = InferredSchema(schema_by_node[edge.from_node])
                if parent is not None:
                    is_inferred = isinstance(parent, InferredSchema)
                    is_raster = (
                        not is_inferred
                        and hasattr(self.qgis, "is_raster_layer")
                        and self.qgis.is_raster_layer(parent)
                    )
                    if (
                        not is_inferred
                        and not is_raster
                        and edge.from_node not in schema_by_node
                        and hasattr(parent, "fields")
                    ):
                        field_specs = getattr(self.qgis, "field_specs", None)
                        schema_by_node[edge.from_node] = (
                            tuple(field_specs(parent))
                            if callable(field_specs)
                            else tuple(
                                SchemaField(field.name(), str(field.type()))
                                for field in parent.fields()
                            )
                        )
                    if port in inputs:
                        inputs[port] = (
                            (*inputs[port], parent)
                            if isinstance(inputs[port], tuple)
                            else (inputs[port], parent)
                        )
                    else:
                        inputs[port] = parent
                schema = schema_by_node.get(edge.from_node)
                if schema is not None:
                    if port in input_schemas:
                        input_schemas[port] = (
                            (*input_schemas[port], schema)
                            if isinstance(input_schemas[port], tuple)
                            else (input_schemas[port], schema)
                        )
                    else:
                        input_schemas[port] = schema

            def contains_inferred(value):
                if isinstance(value, tuple):
                    return any(contains_inferred(item) for item in value)
                return isinstance(value, InferredSchema)

            if data_type == "raster":
                for error in operation_definition.validate_parameters(
                    node.metadata.get("parameters") or {}, {}, None
                ):
                    issues.append(ValidationIssue("freeform_parameters", f"{node.name}: {error}"))
                continue
            validation_qgis = self.qgis if not any(
                contains_inferred(value) for value in inputs.values()
            ) else None
            for error in operation_definition.validate_parameters(
                node.metadata.get("parameters") or {}, inputs, validation_qgis
            ):
                issues.append(ValidationIssue("freeform_parameters", f"{node.name}: {error}"))
            try:
                schema_by_node[node.node_id] = operation_definition.infer_output_schema(
                    input_schemas,
                    node.metadata.get("parameters") or {},
                )
            except Exception as exc:
                issues.append(
                    ValidationIssue(
                        "freeform_schema",
                        f"{node.name}: output schema inference failed: {exc}",
                    )
                )
        delivery_ids = [item.delivery_id for item in definition.forward_deliveries]
        if len(delivery_ids) != len(set(delivery_ids)) or any(not item for item in delivery_ids):
            issues.append(ValidationIssue("duplicate_delivery_id", "Each output needs a unique delivery ID."))
        for delivery in definition.forward_deliveries:
            if delivery.format not in {"geojson", "shapefile", "kml", "kmz", "gpkg", "geotiff"}:
                issues.append(
                    ValidationIssue(
                        "delivery_format",
                        "Free-form outputs support GeoJSON, Shapefile, KML, KMZ, GeoPackage and GeoTIFF.",
                    )
                )
            if not delivery.path.strip():
                issues.append(
                    ValidationIssue(
                        "delivery_path",
                        f"Output '{delivery.name or delivery.delivery_id}' needs a destination path.",
                    )
                )
        paths = [str(item.path).strip().casefold() for item in definition.forward_deliveries if str(item.path).strip()]
        if len(paths) != len(set(paths)):
            issues.append(
                ValidationIssue(
                    "duplicate_output_path",
                    "Free-form output destination paths must be unique before Plan.",
                )
            )
        return ValidationResult(issues)

    @staticmethod
    def _filter_source_ref(definition, graph, parent_node_id):
        node_map = graph.node_map()
        current = parent_node_id
        visited = set()
        while current not in visited:
            visited.add(current)
            node = node_map.get(current)
            if node is None:
                return None
            if node.kind == NodeKind.SOURCE:
                return definition.source_by_lineage_id(
                    str(node.metadata.get("source_lineage_id") or "")
                )
            parents = [
                edge.from_node
                for edge in graph.edges
                if edge.to_node == current and edge.operation == OperationKind.FILTER
            ]
            if len(parents) != 1:
                return None
            current = parents[0]
        return None

    def _validate_expression(self, expression, layer):
        validator = getattr(self.qgis, "validate_expression", None)
        if not callable(validator):
            return True, ""
        try:
            return validator(str(expression or ""), layer)
        except TypeError:
            return validator(str(expression or ""))

    @staticmethod
    def validate_explicit_structure(definition: WorkflowDefinition) -> ValidationResult:
        issues: List[ValidationIssue] = []
        if not definition.workflow_id.strip():
            issues.append(ValidationIssue("workflow_id_missing", "Workflow ID is missing."))
        if not definition.name.strip():
            issues.append(ValidationIssue("workflow_name_missing", "Workflow name is required."))

        sources = definition.source_layers
        graph = definition.effective_dependency_graph()
        node_map = graph.node_map()
        merge_graph = any(
            edge.operation.value == "merge"
            and edge.to_node in node_map
            and node_map[edge.to_node].kind.value == "derived"
            for edge in graph.edges
        )
        expected_source_count = 2 if merge_graph else 1
        if len(sources) != expected_source_count:
            issues.append(
                ValidationIssue(
                    "explicit_source_count",
                    (
                        "G7b MERGE requires exactly two registered source layers."
                        if merge_graph
                        else "G7a FILTER requires exactly one registered source layer."
                    ),
                )
            )
        stable_ids = [item.stable_id for item in sources if item.stable_id]
        if len(stable_ids) != len(set(stable_ids)):
            issues.append(
                ValidationIssue(
                    "duplicate_source_lineage",
                    "Explicit source lineage identities must be unique.",
                )
            )
        for source in sources:
            if not source.stable_id:
                issues.append(
                    ValidationIssue(
                        "source_lineage_id_missing",
                        f"Source layer '{source.name}' has no immutable AtOnce lineage ID.",
                    )
                )
            if not source.current_layer_id:
                issues.append(
                    ValidationIssue(
                        "source_id_missing",
                        f"Source layer '{source.name}' has no current QGIS binding ID.",
                    )
                )

        if definition.gpkg_path.strip() or definition.exports:
            issues.append(
                ValidationIssue(
                    "explicit_legacy_outputs",
                    "Explicit graphs support forward GeoJSON, Shapefile, KML, KMZ, GeoPackage and GeoTIFF; "
                    "legacy GeoPackage/XLSX outputs remain on legacy workflows.",
                )
            )

        try:
            if sources:
                plan_explicit_graph_execution(
                    definition,
                    {source.stable_id for source in sources if source.stable_id},
                )
        except GraphExecutionError as exc:
            issues.append(ValidationIssue("explicit_graph_shape", str(exc)))

        delivery_ids = [item.delivery_id for item in definition.forward_deliveries]
        if len(delivery_ids) != len(set(delivery_ids)) or any(not item for item in delivery_ids):
            issues.append(ValidationIssue("duplicate_delivery_id", "Each forward delivery needs a unique ID."))
        paths = []
        for delivery in definition.forward_deliveries:
            if delivery.format not in {"geojson", "shapefile", "kml", "kmz", "gpkg", "geotiff"}:
                issues.append(
                    ValidationIssue(
                        "delivery_format",
                        "Forward deliveries support GeoJSON, Shapefile, KML, KMZ, GeoPackage and GeoTIFF.",
                    )
                )
            if not delivery.name.strip() or not delivery.path.strip():
                issues.append(ValidationIssue("delivery_required", "Each forward delivery needs a name and path."))
            else:
                paths.append(delivery.path.strip().lower())
        if len(paths) != len(set(paths)):
            issues.append(ValidationIssue("duplicate_output_path", "Forward delivery paths must be unique."))
        return ValidationResult(issues)

    @staticmethod
    def validate_structure(definition: WorkflowDefinition) -> ValidationResult:
        issues: List[ValidationIssue] = []

        if not definition.workflow_id.strip():
            issues.append(ValidationIssue("workflow_id_missing", "Workflow ID is missing."))
        if not definition.name.strip():
            issues.append(ValidationIssue("workflow_name_missing", "Workflow name is required."))

        sources = [layer for layer in definition.layers if layer.role == SOURCE_ROLE]
        derived_layers = [layer for layer in definition.layers if layer.role == DERIVED_ROLE]

        if len(sources) < 2:
            issues.append(ValidationIssue("sources_minimum", "At least two source layers are required."))

        binding_ids = [item.current_layer_id for item in sources if item.current_layer_id]
        if len(binding_ids) != len(set(binding_ids)):
            issues.append(
                ValidationIssue("duplicate_source", "The same current QGIS layer cannot bind two source roles.")
            )

        lineage_ids = [item.stable_id for item in sources if item.stable_id]
        if len(lineage_ids) != len(set(lineage_ids)):
            issues.append(
                ValidationIssue(
                    "duplicate_source_lineage",
                    "Each registered source role needs a distinct immutable AtOnce lineage identity.",
                )
            )

        for source in sources:
            if not source.current_layer_id:
                issues.append(
                    ValidationIssue(
                        "source_id_missing",
                        f"Source layer '{source.name}' has no current QGIS binding ID.",
                    )
                )
            if not source.stable_id:
                issues.append(
                    ValidationIssue(
                        "source_lineage_id_missing",
                        f"Source layer '{source.name}' has no immutable AtOnce lineage ID.",
                    )
                )

        if len(sources) >= 2:
            if len(definition.field_mappings) != 1:
                issues.append(
                    ValidationIssue(
                        "field_mapping_required",
                        "Confirm one field relationship between Source Layer A and Source Layer B.",
                    )
                )
            else:
                mapping = definition.field_mappings[0]
                expected_ids = {sources[0].stable_id, sources[1].stable_id}
                mapped_ids = {mapping.left_layer_id, mapping.right_layer_id}

                if not mapping.confirmed:
                    issues.append(
                        ValidationIssue(
                            "field_mapping_unconfirmed",
                            "The source field relationship must be confirmed by the user.",
                        )
                    )
                if mapping.relationship_type != SAME_BUSINESS_KEY:
                    issues.append(
                        ValidationIssue(
                            "field_mapping_type",
                            "The first AtOnce workflow supports only a same-business-key relationship.",
                        )
                    )
                if not mapping.left_layer_id or not mapping.right_layer_id:
                    issues.append(
                        ValidationIssue(
                            "field_mapping_layer_missing",
                            "Both sides of the source field relationship need a stable source lineage ID.",
                        )
                    )
                elif mapping.left_layer_id == mapping.right_layer_id:
                    issues.append(
                        ValidationIssue(
                            "field_mapping_same_layer",
                            "A source field relationship must connect two different source identities.",
                        )
                    )
                elif mapped_ids != expected_ids:
                    issues.append(
                        ValidationIssue(
                            "field_mapping_wrong_layers",
                            "The field relationship must connect the two registered stable source identities exactly.",
                        )
                    )
                if not mapping.left_field.strip() or not mapping.right_field.strip():
                    issues.append(
                        ValidationIssue(
                            "field_mapping_field_missing",
                            "Choose one business-key field from each source layer.",
                        )
                    )

        if len(derived_layers) != 1:
            issues.append(ValidationIssue("derived_count", "Exactly one derived Layer C target is required."))
        else:
            derived = derived_layers[0]
            if not derived.name.strip():
                issues.append(ValidationIssue("derived_name_missing", "Derived Layer C needs a name."))
            if derived.layer_id and derived.layer_id in binding_ids:
                issues.append(
                    ValidationIssue("derived_is_source", "Layer C cannot be the same QGIS layer as a registered source.")
                )

        if not definition.gpkg_path.strip():
            issues.append(ValidationIssue("gpkg_path_missing", "GeoPackage output path is required."))
        elif not definition.gpkg_path.lower().endswith(".gpkg"):
            issues.append(
                ValidationIssue(
                    "gpkg_extension",
                    "GeoPackage output should use a .gpkg filename.",
                    WARNING,
                )
            )

        if len(definition.exports) < 2:
            issues.append(ValidationIssue("exports_minimum", "At least two linked XLSX outputs are required."))

        export_ids = [item.export_id for item in definition.exports]
        if len(export_ids) != len(set(export_ids)):
            issues.append(ValidationIssue("duplicate_export_id", "Each XLSX export needs a unique export ID."))

        normalized_paths = []
        for export in definition.exports:
            if not export.name.strip():
                issues.append(ValidationIssue("export_name_missing", "Each XLSX export needs a display name."))
            if not export.path.strip():
                issues.append(
                    ValidationIssue("export_path_missing", f"{export.name or 'An XLSX export'} needs a path.")
                )
            else:
                path = export.path.strip().lower()
                normalized_paths.append(path)
                if not path.endswith(".xlsx"):
                    issues.append(
                        ValidationIssue(
                            "xlsx_extension",
                            f"{export.name} should use a .xlsx filename.",
                            WARNING,
                        )
                    )

        if len(normalized_paths) != len(set(normalized_paths)):
            issues.append(
                ValidationIssue("duplicate_export_path", "Two linked XLSX outputs cannot use the same path.")
            )

        delivery_ids = [item.delivery_id for item in definition.forward_deliveries]
        if len(delivery_ids) != len(set(delivery_ids)) or any(not item for item in delivery_ids):
            issues.append(ValidationIssue("duplicate_delivery_id", "Each forward delivery needs a unique ID."))
        for delivery in definition.forward_deliveries:
            if delivery.format not in {"geojson", "shapefile", "kml", "kmz", "gpkg", "geotiff"}:
                issues.append(ValidationIssue("delivery_format", "Forward deliveries support GeoJSON, Shapefile, KML, KMZ, GeoPackage, and GeoTIFF."))
            if not delivery.name.strip() or not delivery.path.strip():
                issues.append(ValidationIssue("delivery_required", "Each forward delivery needs a name and path."))
            elif delivery.format == "geojson" and not delivery.path.lower().endswith(".geojson"):
                issues.append(ValidationIssue("geojson_extension", f"{delivery.name} should use a .geojson filename.", WARNING))
            elif delivery.format == "shapefile" and not delivery.path.lower().endswith(".shp"):
                issues.append(ValidationIssue("shapefile_extension", f"{delivery.name} should use a .shp filename.", WARNING))
            elif delivery.format == "kml" and not delivery.path.lower().endswith(".kml"):
                issues.append(ValidationIssue("kml_extension", f"{delivery.name} should use a .kml filename.", WARNING))
            elif delivery.format == "kmz" and not delivery.path.lower().endswith(".kmz"):
                issues.append(ValidationIssue("kmz_extension", f"{delivery.name} should use a .kmz filename.", WARNING))
            elif delivery.format == "gpkg" and not delivery.path.lower().endswith(".gpkg"):
                issues.append(ValidationIssue("gpkg_extension", f"{delivery.name} should use a .gpkg filename.", WARNING))
            elif delivery.format == "geotiff" and not (
                delivery.path.lower().endswith(".tif") or delivery.path.lower().endswith(".tiff")
            ):
                issues.append(ValidationIssue("geotiff_extension", f"{delivery.name} should use a .tif/.tiff filename.", WARNING))
            normalized_paths.append(delivery.path.strip().lower())
        if len(normalized_paths) != len(set(normalized_paths)):
            issues.append(ValidationIssue("duplicate_output_path", "Downstream outputs cannot use the same path."))

        return ValidationResult(issues)
