"""Pending-change status UI for the compact Changes tab.

The compact tab originally listed registered sources/outputs only.  This bridge
keeps that static inventory but adds the operational state users need while
editing:

* source tabs show Editing / Changed / Up to date;
* saved changed fields are summarized without exposing lineage internals;
* affected output tabs show Pending / Needs update / Up to date;
* Update Changes is enabled only when a saved source change affects an included
  output.  Unsaved edits deliberately cannot be propagated.
"""

_APPLIED = False


def _kind_value(node):
    return str(getattr(getattr(node, "kind", ""), "value", getattr(node, "kind", "")))


def _source_names(workflow):
    return {
        str(source.stable_id): str(source.name or "Source")
        for source in (workflow.source_layers if workflow is not None else ())
    }


def _history_fields_by_source(workflow, history_rows):
    """Map registered source lineage IDs to user-facing changed field names."""

    names = _source_names(workflow)
    by_id = {source_id: set() for source_id in names}
    for row in history_rows or ():
        if not row:
            continue
        changed_layer = str(row[0] or "")
        changed_field = str(row[1] or "") if len(row) > 1 else ""
        if not changed_field:
            continue
        for source_id, source_name in names.items():
            if changed_layer in {source_name, source_id}:
                by_id[source_id].add(changed_field)
    return by_id


def changes_status_snapshot(workflow, committed_ids=(), live_ids=(), history_rows=()):
    """Return QGIS-free source/output status used by the compact UI."""

    if workflow is None:
        return {"sources": {}, "outputs": {}, "can_update": False}

    from .canvas_pending_stale import downstream_stale_node_ids

    committed = {str(item) for item in committed_ids or () if str(item)}
    live = {str(item) for item in live_ids or () if str(item)}
    fields_by_source = _history_fields_by_source(workflow, history_rows)
    graph = workflow.effective_dependency_graph()
    committed_stale = downstream_stale_node_ids(graph, committed)
    live_stale = downstream_stale_node_ids(graph, live)

    source_states = {}
    for source in workflow.source_layers:
        source_id = str(source.stable_id)
        if source_id in committed:
            state = "changed"
        elif source_id in live:
            state = "editing"
        else:
            state = "current"
        source_states[source_id] = {
            "state": state,
            "fields": tuple(sorted(fields_by_source.get(source_id, set()))),
        }

    output_states = {}
    can_update = False
    for node in graph.nodes:
        if _kind_value(node) != "delivery":
            continue
        included = bool((node.metadata or {}).get("include_in_changes", True))
        if node.node_id in committed_stale:
            state = "needs_update"
            if included:
                can_update = True
        elif node.node_id in live_stale:
            state = "pending_save"
        else:
            state = "current"
        output_states[str(node.node_id)] = {
            "state": state,
            "included": included,
        }

    return {
        "sources": source_states,
        "outputs": output_states,
        "can_update": can_update,
    }


def _ensure_page_label(page, attribute_name, object_name="AtOnceMuted"):
    label = getattr(page, attribute_name, None)
    if label is not None:
        return label
    from qgis.PyQt.QtWidgets import QLabel

    label = QLabel("", page)
    label.setObjectName(object_name)
    label.setWordWrap(True)
    layout = page.layout()
    if layout is not None:
        # Existing pages end in addStretch(1).  Insert status immediately before
        # that spacer so it stays with the layer description at the top.
        layout.insertWidget(max(0, layout.count() - 1), label)
    setattr(page, attribute_name, label)
    return label


def _render_changes_status(dock, workflow, committed_ids=(), live_ids=(), history_rows=()):
    snapshot = changes_status_snapshot(
        workflow,
        committed_ids=committed_ids,
        live_ids=live_ids,
        history_rows=history_rows,
    )

    source_names = _source_names(workflow)
    for index in range(getattr(dock.source_stack, "count", lambda: 0)()):
        page = dock.source_stack.widget(index)
        source_id = str(page.property("source_lineage_id") or "")
        source_name = source_names.get(source_id, dock.source_tabs.tabText(index) or "Source")
        state = snapshot["sources"].get(source_id, {"state": "current", "fields": ()})
        status = _ensure_page_label(page, "_atonce_pending_status")
        fields = _ensure_page_label(page, "_atonce_pending_fields")

        if state["state"] == "changed":
            dock.source_tabs.setTabText(index, f"{source_name} • Changed")
            status.setText("Saved changes are waiting for downstream update.")
            changed_fields = tuple(item for item in state["fields"] if item)
            fields.setText(
                "Changed fields: " + ", ".join(changed_fields)
                if changed_fields
                else "Saved feature changes detected."
            )
        elif state["state"] == "editing":
            dock.source_tabs.setTabText(index, f"{source_name} • Editing")
            status.setText("Unsaved edits detected. Save the source layer before updating downstream.")
            fields.setText("Changed fields will be confirmed when the edits are saved.")
        else:
            dock.source_tabs.setTabText(index, source_name)
            status.setText("Up to date")
            fields.setText("")

    node_map = (
        workflow.effective_dependency_graph().node_map()
        if workflow is not None
        else {}
    )
    for index in range(getattr(dock.output_stack, "count", lambda: 0)()):
        page = dock.output_stack.widget(index)
        node_id = str(page.property("output_node_id") or "")
        node = node_map.get(node_id)
        output_name = str(getattr(node, "name", "") or dock.output_tabs.tabText(index) or "Output")
        state = snapshot["outputs"].get(
            node_id,
            {"state": "current", "included": True},
        )
        status = _ensure_page_label(page, "_atonce_pending_status")
        included_text = "Included in Changes" if state["included"] else "Excluded from Changes"

        if state["state"] == "needs_update":
            dock.output_tabs.setTabText(index, f"{output_name} ! Needs update")
            status.setText(
                "Needs update — saved upstream changes are waiting. " + included_text + "."
            )
        elif state["state"] == "pending_save":
            dock.output_tabs.setTabText(index, f"{output_name} • Pending")
            status.setText(
                "Unsaved upstream edits detected. Save the source layer before Update Changes. "
                + included_text
                + "."
            )
        else:
            dock.output_tabs.setTabText(index, output_name)
            status.setText("Up to date · " + included_text)

    dock.update_changes_button.setEnabled(bool(snapshot["can_update"]))
    dock.update_changes_button.setToolTip(
        "Refresh outputs affected by saved source changes"
        if snapshot["can_update"]
        else "Save a source change that affects an included output first"
    )
    dock._atonce_changes_snapshot = snapshot
    return snapshot


def apply_changes_status_ui():
    """Install Changes-tab status synchronization exactly once per process."""

    global _APPLIED
    if _APPLIED:
        return

    from . import canvas_pending_stale
    from .ui.compact_dock import CompactAtOnceDockWidget

    original_populate = CompactAtOnceDockWidget._populate_change_layers

    def set_pending_change_status(
        self,
        workflow,
        committed_ids=(),
        live_ids=(),
        history_rows=(),
    ):
        return _render_changes_status(
            self,
            workflow,
            committed_ids=committed_ids,
            live_ids=live_ids,
            history_rows=history_rows,
        )

    CompactAtOnceDockWidget.set_pending_change_status = set_pending_change_status

    def populate_change_layers(self, workflow):
        result = original_populate(self, workflow)
        # The inventory itself is neutral.  Pending state is applied by the
        # source-stale bridge immediately after workflow render / edit signals.
        self.set_pending_change_status(workflow)
        return result

    CompactAtOnceDockWidget._populate_change_layers = populate_change_layers

    original_apply_pending_visual = canvas_pending_stale._apply_pending_visual

    def apply_pending_visual(plugin, workflow=None):
        result = original_apply_pending_visual(plugin, workflow)
        dock = getattr(plugin, "dock", None)
        workflow = workflow or plugin._active_workflow()
        if dock is not None and hasattr(dock, "set_pending_change_status"):
            session = getattr(plugin, "_change_session", None)
            committed = set(getattr(session, "changed_source_ids", ()) or ())
            history_rows = tuple(getattr(session, "history_rows", ()) or ())
            live = set(getattr(plugin, "_atonce_live_dirty_source_ids", ()) or ())
            dock.set_pending_change_status(
                workflow,
                committed_ids=committed,
                live_ids=live,
                history_rows=history_rows,
            )
        return result

    # Native QGIS edit callbacks resolve this module global at invocation time,
    # so replacing it here updates both toolbar Toggle Editing and AtOnce edits.
    canvas_pending_stale._apply_pending_visual = apply_pending_visual
    _APPLIED = True
