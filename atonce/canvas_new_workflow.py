"""Explicit non-destructive New Workflow action for the compact canvas UI.

The old compact "Clear" control only cleared the builder while leaving the dock
selector/state visually attached to the previously selected workflow.  That made
it too easy to believe a new workflow was being authored while still thinking in
terms of the old persisted workflow.

Product contract:

* New Workflow starts a completely fresh, empty draft.
* The saved workflow currently selected in the combo is never deleted.
* The combo is deselected and the dock/builder workflow identity becomes None.
* The next successful Register therefore creates a fresh workflow_id.
* Rename remains a presentation change on the same workflow_id and is unrelated
  to this action.
"""


_APPLIED = False


def apply_new_workflow_action():
    """Install the compact New Workflow UX exactly once per plugin process."""

    global _APPLIED
    if _APPLIED:
        return

    from .canvas_plugin import CanvasRecoveryAtOncePlugin
    from .ui.compact_dock import CompactAtOnceDockWidget

    original_workflow_tab = CompactAtOnceDockWidget._workflow_tab

    def workflow_tab(self):
        page = original_workflow_tab(self)
        button = self.register_button
        button.setText("New Workflow")
        button.setToolTip(
            "Start a completely new empty workflow. Saved workflows are kept."
        )
        try:
            button.clicked.disconnect()
        except (TypeError, RuntimeError):
            pass
        button.clicked.connect(self.register_requested.emit)
        return page

    CompactAtOnceDockWidget._workflow_tab = workflow_tab

    # CompactAtOnceDockWidget.set_workflow historically rewrites this button's
    # label to "Clear" on every workflow render. Keep the explicit product label
    # stable whether a saved workflow is selected or the canvas is empty.
    original_set_workflow = CompactAtOnceDockWidget.set_workflow

    def set_workflow(self, workflow):
        result = original_set_workflow(self, workflow)
        if hasattr(self, "register_button"):
            self.register_button.setText("New Workflow")
            self.register_button.setToolTip(
                "Start a completely new empty workflow. Saved workflows are kept."
            )
        return result

    CompactAtOnceDockWidget.set_workflow = set_workflow

    def start_new_workflow(self):
        """Detach authoring state without deleting any persisted workflow."""

        if self.dock is None:
            return
        self._reset_change_session()
        # This helper repopulates the saved-workflow selector, deselects it, and
        # calls dock.set_workflow(None), which also clears the builder graph and
        # its persisted workflow identity.
        self._show_empty_canvas_with_selector()
        self.iface.messageBar().pushInfo(
            "AtOnce",
            "New workflow started. Saved workflows were kept unchanged.",
        )

    # Keep the existing signal wiring name for compatibility; its semantics in
    # the compact canvas are now explicitly non-destructive New Workflow.
    CanvasRecoveryAtOncePlugin._on_canvas_clear_requested = start_new_workflow

    _APPLIED = True
