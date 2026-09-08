"""Deterministic source-field relationships derived from a registered DAG."""

from ..models.dependency_graph import NodeKind, OperationKind


_PRESERVE = {
    OperationKind.PASSTHROUGH,
    OperationKind.FILTER,
    OperationKind.CHANGE_FIELD_TYPE,
    OperationKind.REMOVE_DUPLICATES,
    OperationKind.SORT,
    OperationKind.SELECT_BY_LOCATION,
    OperationKind.CLIP,
    OperationKind.BUFFER,
    OperationKind.REPROJECT,
}


def _kind(node):
    try:
        return OperationKind(str(node.metadata.get("operation_kind") or ""))
    except ValueError:
        return None


def _parameters(node):
    return dict(node.metadata.get("parameters") or {})


def _rows(value):
    return [dict(item) for item in value or () if isinstance(item, dict)]


def _forward_fields(kind, parameters, port, field_name):
    if kind in _PRESERVE:
        return (field_name,)
    if kind == OperationKind.FIELD_MAPPING:
        return tuple(
            str(row.get("output_field") or "")
            for row in _rows(parameters.get("mappings"))
            if str(row.get("source_field") or "") == field_name
            and str(row.get("output_field") or "")
        )
    if kind == OperationKind.KEEP_FIELDS:
        return (field_name,) if field_name in set(parameters.get("fields") or ()) else ()
    if kind == OperationKind.RENAME_FIELD:
        source = str(parameters.get("source_field") or "")
        target = str(parameters.get("target_name") or "")
        return (target,) if field_name == source and target else (field_name,)
    if kind == OperationKind.CALCULATE_FIELD:
        target = str(parameters.get("field_name") or "")
        return () if field_name == target else (field_name,)
    if kind == OperationKind.JOIN:
        return (field_name,) if port == "left" else ()
    if kind == OperationKind.MERGE:
        return (field_name,)
    return ()


def _backward_fields(kind, parameters, port, field_name):
    if kind in _PRESERVE:
        return (field_name,)
    if kind == OperationKind.FIELD_MAPPING:
        return tuple(
            str(row.get("source_field") or "")
            for row in _rows(parameters.get("mappings"))
            if str(row.get("output_field") or "") == field_name
            and str(row.get("source_field") or "")
        )
    if kind == OperationKind.KEEP_FIELDS:
        return (field_name,) if field_name in set(parameters.get("fields") or ()) else ()
    if kind == OperationKind.RENAME_FIELD:
        source = str(parameters.get("source_field") or "")
        target = str(parameters.get("target_name") or "")
        if field_name == target and source:
            return (source,)
        return () if field_name == source and source != target else (field_name,)
    if kind == OperationKind.CALCULATE_FIELD:
        target = str(parameters.get("field_name") or "")
        return () if field_name == target else (field_name,)
    if kind == OperationKind.JOIN:
        return (field_name,) if port == "left" else ()
    if kind == OperationKind.MERGE:
        return (field_name,)
    return ()


def _cross_fields(kind, parameters, own_port, field_name):
    if kind == OperationKind.JOIN:
        left = str(parameters.get("left_field") or "")
        right = str(parameters.get("right_field") or "")
        if own_port == "left" and field_name == left and right:
            return (("right", right),)
        if own_port == "right" and field_name == right and left:
            return (("left", left),)
    if kind == OperationKind.COMPARE_CHANGES:
        previous = str(parameters.get("previous_key_field") or "")
        current = str(parameters.get("current_key_field") or "")
        compared = {str(item) for item in parameters.get("compare_fields") or ()}
        if own_port == "previous":
            if field_name == previous and current:
                return (("current", current),)
            if field_name in compared:
                return (("current", field_name),)
        if own_port == "current":
            if field_name == current and previous:
                return (("previous", previous),)
            if field_name in compared:
                return (("previous", field_name),)
    if kind == OperationKind.MERGE and not field_name.startswith("("):
        return (("input", field_name),)
    return ()


def source_field_relationships(workflow, changed_lineage_id, changed_fields):
    """Return History rows proven by field transforms and multi-input contracts."""

    graph = workflow.effective_dependency_graph()
    node_map = graph.node_map()
    source_nodes = {
        str(node.metadata.get("source_lineage_id") or ""): node.node_id
        for node in graph.nodes
        if node.kind == NodeKind.SOURCE
    }
    source_refs = {item.stable_id: item for item in workflow.source_layers}
    changed_lineage_id = str(changed_lineage_id or "")
    changed_ref = source_refs.get(changed_lineage_id)
    changed_name = changed_ref.name if changed_ref is not None else changed_lineage_id
    start_node_id = source_nodes.get(changed_lineage_id)

    def source_ancestors(node_id, field_name, seen):
        state = (node_id, field_name)
        if state in seen:
            return set()
        seen = {*seen, state}
        node = node_map.get(node_id)
        if node is None:
            return set()
        if node.kind == NodeKind.SOURCE:
            lineage = str(node.metadata.get("source_lineage_id") or "")
            return {(lineage, field_name)} if lineage else set()
        if node.kind != NodeKind.DERIVED:
            return set()
        kind = _kind(node)
        parameters = _parameters(node)
        result = set()
        for edge in graph.incoming_edges(node_id):
            port = str(edge.parameters.get("target_port") or "input")
            for parent_field in _backward_fields(kind, parameters, port, field_name):
                result.update(source_ancestors(edge.from_node, parent_field, seen))
        return result

    def related_sources(field_name):
        if not start_node_id:
            return set()
        found = set()
        queue = [(start_node_id, field_name)]
        visited = set()
        while queue:
            parent_id, parent_field = queue.pop(0)
            state = (parent_id, parent_field)
            if state in visited:
                continue
            visited.add(state)
            for edge in graph.downstream_edges(parent_id):
                operation = node_map.get(edge.to_node)
                if operation is None or operation.kind != NodeKind.DERIVED:
                    continue
                kind = _kind(operation)
                parameters = _parameters(operation)
                own_port = str(edge.parameters.get("target_port") or "input")
                for other_port, other_field in _cross_fields(
                    kind, parameters, own_port, parent_field
                ):
                    for other_edge in graph.incoming_edges(operation.node_id):
                        port = str(other_edge.parameters.get("target_port") or "input")
                        if other_edge.edge_id == edge.edge_id or port != other_port:
                            continue
                        found.update(
                            source_ancestors(other_edge.from_node, other_field, set())
                        )
                for output_field in _forward_fields(
                    kind, parameters, own_port, parent_field
                ):
                    queue.append((operation.node_id, output_field))
        return {
            (lineage, source_field)
            for lineage, source_field in found
            if lineage and lineage != changed_lineage_id
        }

    rows = []
    for field_name in sorted(str(item) for item in changed_fields):
        relationships = sorted(related_sources(field_name))
        if not relationships:
            rows.append((changed_name, field_name, "—", "—"))
            continue
        for lineage, source_field in relationships:
            source = source_refs.get(lineage)
            rows.append(
                (
                    changed_name,
                    field_name,
                    source.name if source is not None else lineage,
                    source_field,
                )
            )
    return rows
