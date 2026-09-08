"""Visual stale-state bridge for source edits awaiting Update Changes.

The canvas warning must not depend on edits being started through AtOnce.  A
registered QGIS source can be edited through the native Toggle Editing action,
the attribute table, or the compact Changes tab.  This module therefore listens
to the source layer itself and keeps a session-only visual state:

* a live user edit immediately marks every downstream operation/output stale;
* a committed edit is recorded in the compact change session so Update Changes
  can refresh the affected branch;
* cancelling/rolling back an edit removes a live-only warning;
* registered grey/locked styling remains owned by the registered-lock patch;
* switching away and back keeps committed warnings while they are pending;
* successful Update Changes clears the temporary warning;
* AtOnce's own internal source-preparation writes are suppressed and never
  appear as user changes.
"""

_APPLIED = False


def downstream_stale_node_ids(graph, changed_source_lineage_ids):
    """Return all descendants of the changed SOURCE lineage identities."""

    changed = {
        str(item or "")
        for item in changed_source_lineage_ids
        if str(item or "")
    }
    if not changed:
        return set()

    roots = {
        node.node_id
        for node in graph.nodes
        if getattr(node.kind, "value", node.kind) == "source"
        and str((node.metadata or {}).get("source_lineage_id") or "") in changed
    }
    stale = set()
    queue = list(roots)
    visited = set(roots)
    while queue:
        current = queue.pop(0)
        for edge in graph.downstream_edges(current):
            target = str(edge.to_node)
            stale.add(target)
            if target not in visited:
                visited.add(target)
                queue.append(target)
    return stale


def _active_visual_changed_source_ids(plugin):
    session = getattr(plugin, "_change_session", None)
    committed = set(getattr(session, "changed_source_ids", ()) or ())
    live = set(getattr(plugin, "_atonce_live_dirty_source_ids", ()) or ())
    return committed | live


def _apply_pending_visual(plugin, workflow=None):
    """Render session/live source changes without erasing persisted stale state."""

    dock = getattr(plugin, "dock", None)
    builder = getattr(dock, "builder", None) if dock is not None else None
    if builder is None:
        return
    workflow = workflow or plugin._active_workflow()
    if workflow is None:
        return

    # Remove only the temporary node IDs previously owned by this bridge.  Any
    # stale-by-choice IDs loaded from persisted graph-run evidence remain intact.
    previous = set(getattr(plugin, "_atonce_pending_visual_node_ids", ()) or ())
    builder._stale_node_ids.difference_update(previous)

    changed = _active_visual_changed_source_ids(plugin)
    stale = downstream_stale_node_ids(
        workflow.effective_dependency_graph(),
        changed,
    )
    builder._stale_node_ids.update(stale)
    plugin._atonce_pending_visual_node_ids = set(stale)
    builder._render_graph()


def _field_name(layer, index):
    try:
        return str(layer.fields().at(int(index)).name())
    except Exception:
        return ""


def _attribute_commit_fields(layer, signal_args):
    """Extract changed field names from QgsVectorLayer commit signal arguments."""

    payload = signal_args[-1] if signal_args else None
    names = set()
    if isinstance(payload, dict):
        for per_feature in payload.values():
            if not isinstance(per_feature, dict):
                continue
            for index in per_feature:
                name = _field_name(layer, index)
                if name:
                    names.add(name)
    return names or {"(feature)"}


def _native_source_committed(
    plugin,
    workflow_id,
    source_lineage_id,
    layer,
    changed_fields,
):
    """Record a real QGIS source commit and immediately keep its branch stale."""

    if getattr(plugin, "_atonce_suppress_source_commit_tracking", False):
        return

    workflow = plugin._active_workflow()
    dock_workflow = getattr(getattr(plugin, "dock", None), "_workflow", None)
    if (
        workflow is None
        or str(workflow.workflow_id) != str(workflow_id)
        or dock_workflow is None
        or str(dock_workflow.workflow_id) != str(workflow_id)
    ):
        return

    source_id = str(source_lineage_id or "")
    if not source_id:
        return
    session = getattr(plugin, "_change_session", None)
    if session is None:
        return

    already_pending = source_id in set(session.changed_source_ids)
    session.changed_source_ids.add(source_id)

    # Native QGIS Toggle Editing has no AtOnce stop-handler to populate History.
    # Add one field-impact entry on its first commit.  The AtOnce-managed edit
    # path already writes richer rows after commit, so avoid duplicating those.
    if (
        not already_pending
        and not getattr(plugin, "_atonce_managed_source_edit", False)
    ):
        fields = set(changed_fields or ()) or {"(feature)"}
        rows = plugin._session_history_rows(workflow, source_id, fields)
        if rows:
            session.history_rows.extend(tuple(row) for row in rows)
            dock = getattr(plugin, "dock", None)
            if dock is not None:
                dock.append_session_history(rows)

    _apply_pending_visual(plugin, workflow)


def _native_source_live_changed(plugin, workflow_id, source_lineage_id):
    """Show the warning while a native QGIS edit is still unsaved."""

    if getattr(plugin, "_atonce_suppress_source_commit_tracking", False):
        return
    workflow = plugin._active_workflow()
    dock_workflow = getattr(getattr(plugin, "dock", None), "_workflow", None)
    if (
        workflow is None
        or str(workflow.workflow_id) != str(workflow_id)
        or dock_workflow is None
        or str(dock_workflow.workflow_id) != str(workflow_id)
    ):
        return
    dirty = getattr(plugin, "_atonce_live_dirty_source_ids", None)
    if dirty is None:
        dirty = set()
        plugin._atonce_live_dirty_source_ids = dirty
    dirty.add(str(source_lineage_id))
    _apply_pending_visual(plugin, workflow)


def _native_source_edit_stopped(plugin, workflow_id, source_lineage_id):
    """Clear live-only dirtiness; committed state, if any, remains pending."""

    dirty = getattr(plugin, "_atonce_live_dirty_source_ids", None)
    if dirty is not None:
        dirty.discard(str(source_lineage_id))
    workflow = plugin._active_workflow()
    if workflow is not None and str(workflow.workflow_id) == str(workflow_id):
        _apply_pending_visual(plugin, workflow)


def _unbind_native_source_signals(plugin):
    bindings = getattr(plugin, "_atonce_source_commit_bindings", {}) or {}
    for record in bindings.values():
        layer = record.get("layer")
        for signal_name, callback in record.get("callbacks", ()):
            signal = getattr(layer, signal_name, None) if layer is not None else None
            if signal is None or not hasattr(signal, "disconnect"):
                continue
            try:
                signal.disconnect(callback)
            except (TypeError, RuntimeError):
                pass
    plugin._atonce_source_commit_bindings = {}


def _bind_native_source_signals(plugin, workflow=None):
    """Listen to user edits no matter which QGIS edit control starts them."""

    _unbind_native_source_signals(plugin)
    workflow = workflow or plugin._active_workflow()
    dock_workflow = getattr(getattr(plugin, "dock", None), "_workflow", None)
    if (
        workflow is None
        or dock_workflow is None
        or str(dock_workflow.workflow_id) != str(workflow.workflow_id)
    ):
        return

    bindings = {}
    for source in workflow.source_layers:
        layer = plugin.qgis.resolve_layer(source.current_layer_id)
        if layer is None:
            continue
        workflow_id = str(workflow.workflow_id)
        source_id = str(source.stable_id)
        callbacks = []

        def connect(signal_name, callback):
            signal = getattr(layer, signal_name, None)
            if signal is None or not hasattr(signal, "connect"):
                return
            try:
                signal.connect(callback)
                callbacks.append((signal_name, callback))
            except (TypeError, RuntimeError):
                pass

        # Live edit signals make the red warning visible immediately, even before
        # the user toggles editing off.  They do NOT make Update Changes runnable
        # until QGIS emits a real committed-* signal.
        connect(
            "attributeValueChanged",
            lambda *_args, wid=workflow_id, sid=source_id: _native_source_live_changed(
                plugin, wid, sid
            ),
        )
        connect(
            "geometryChanged",
            lambda *_args, wid=workflow_id, sid=source_id: _native_source_live_changed(
                plugin, wid, sid
            ),
        )
        connect(
            "featureAdded",
            lambda *_args, wid=workflow_id, sid=source_id: _native_source_live_changed(
                plugin, wid, sid
            ),
        )
        connect(
            "featureDeleted",
            lambda *_args, wid=workflow_id, sid=source_id: _native_source_live_changed(
                plugin, wid, sid
            ),
        )

        connect(
            "committedAttributeValuesChanges",
            lambda *args, wid=workflow_id, sid=source_id, lyr=layer: _native_source_committed(
                plugin,
                wid,
                sid,
                lyr,
                _attribute_commit_fields(lyr, args),
            ),
        )
        connect(
            "committedGeometriesChanges",
            lambda *_args, wid=workflow_id, sid=source_id, lyr=layer: _native_source_committed(
                plugin, wid, sid, lyr, {"(geometry)"}
            ),
        )
        connect(
            "committedFeaturesAdded",
            lambda *_args, wid=workflow_id, sid=source_id, lyr=layer: _native_source_committed(
                plugin, wid, sid, lyr, {"(feature)"}
            ),
        )
        connect(
            "committedFeaturesRemoved",
            lambda *_args, wid=workflow_id, sid=source_id, lyr=layer: _native_source_committed(
                plugin, wid, sid, lyr, {"(feature)"}
            ),
        )
        connect(
            "editingStopped",
            lambda *args, wid=workflow_id, sid=source_id: _native_source_edit_stopped(
                plugin, wid, sid
            ),
        )

        bindings[str(source.current_layer_id)] = {
            "layer": layer,
            "callbacks": callbacks,
        }
    plugin._atonce_source_commit_bindings = bindings


def apply_pending_source_stale_visuals():
    """Install native/source-button stale rendering exactly once per process."""

    global _APPLIED
    if _APPLIED:
        return

    from .canvas_plugin import CanvasRecoveryAtOncePlugin

    original_source_edit = CanvasRecoveryAtOncePlugin._on_source_edit_requested
    original_reload = CanvasRecoveryAtOncePlugin._reload_workflow
    original_update = CanvasRecoveryAtOncePlugin._on_update_changes_requested
    original_prepare = CanvasRecoveryAtOncePlugin._prepare_explicit_source_identity
    original_reset = CanvasRecoveryAtOncePlugin._reset_change_session
    original_unload = CanvasRecoveryAtOncePlugin.unload

    def source_edit_requested(self, source_lineage_id):
        workflow = self._active_workflow()
        source = (
            workflow.source_by_lineage_id(str(source_lineage_id or ""))
            if workflow is not None
            else None
        )
        layer = self.qgis.resolve_layer(source.current_layer_id) if source is not None else None
        was_editable = bool(layer is not None and layer.isEditable())

        previous = getattr(self, "_atonce_managed_source_edit", False)
        self._atonce_managed_source_edit = True
        try:
            result = original_source_edit(self, source_lineage_id)
        finally:
            self._atonce_managed_source_edit = previous

        # Starting edit mode alone is not a change.  On stop/save the native
        # commit listener has already recorded the source; this call guarantees
        # the visual is refreshed even on providers with limited commit signals.
        if was_editable and layer is not None and not layer.isEditable():
            session = getattr(self, "_change_session", None)
            changed = set(getattr(session, "changed_source_ids", ()) or ())
            if str(source_lineage_id or "") in changed:
                _apply_pending_visual(self, workflow)
        return result

    def prepare_source_identity(self, workflow):
        # Source-key preparation is an AtOnce implementation detail, not a user
        # data edit.  Ignore any QGIS edit/commit signals it emits.
        previous = getattr(self, "_atonce_suppress_source_commit_tracking", False)
        self._atonce_suppress_source_commit_tracking = True
        try:
            return original_prepare(self, workflow)
        finally:
            self._atonce_suppress_source_commit_tracking = previous

    def reload_workflow(self):
        result = original_reload(self)
        workflow = self._active_workflow()
        _bind_native_source_signals(self, workflow)
        _apply_pending_visual(self, workflow)
        return result

    def update_changes_requested(self):
        result = original_update(self)
        session = getattr(self, "_change_session", None)
        changed = set(getattr(session, "changed_source_ids", ()) or ())
        if not changed:
            # Execution reloads before complete_update clears the session. Reload
            # once more so graph-run evidence replaces the temporary warning.
            original_reload(self)
            _bind_native_source_signals(self, self._active_workflow())
            _apply_pending_visual(self, self._active_workflow())
        else:
            _apply_pending_visual(self)
        return result

    def reset_change_session(self):
        result = original_reset(self)
        self._atonce_live_dirty_source_ids = set()
        self._atonce_pending_visual_node_ids = set()
        return result

    def unload(self):
        _unbind_native_source_signals(self)
        return original_unload(self)

    CanvasRecoveryAtOncePlugin._on_source_edit_requested = source_edit_requested
    CanvasRecoveryAtOncePlugin._prepare_explicit_source_identity = prepare_source_identity
    CanvasRecoveryAtOncePlugin._reload_workflow = reload_workflow
    CanvasRecoveryAtOncePlugin._on_update_changes_requested = update_changes_requested
    CanvasRecoveryAtOncePlugin._reset_change_session = reset_change_session
    CanvasRecoveryAtOncePlugin.unload = unload
    _APPLIED = True
