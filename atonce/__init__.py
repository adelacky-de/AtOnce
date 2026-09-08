"""QGIS entry point for AtOnce."""


def classFactory(iface):  # noqa: N802 - QGIS requires this exact name
    """Create the canvas-first production plugin instance."""
    from .canvas_plugin import CanvasRecoveryAtOncePlugin
    from .canvas_acceptance_fixes import apply_canvas_acceptance_fixes
    from .canvas_output_path_guard import apply_output_path_guard
    from .canvas_task_overwrite_warning import apply_task_overwrite_confirmation
    from .canvas_registered_lock import apply_registered_canvas_lock
    from .canvas_merge_schema_fix import apply_merge_schema_fix
    from .canvas_new_workflow import apply_new_workflow_action
    from .canvas_source_preparation_ui import apply_source_preparation_ui
    from .canvas_pending_stale import apply_pending_source_stale_visuals
    from .canvas_changes_status_ui import apply_changes_status_ui
    from .canvas_gpkg_realcase import apply_gpkg_realcase_support
    from .canvas_realcase_finalizer import apply_realcase_finalizer

    apply_canvas_acceptance_fixes()
    apply_merge_schema_fix()
    apply_output_path_guard()
    # Enforce the authoring overwrite policy before installing the final canvas
    # interaction/visual lock for persisted task structure.
    apply_task_overwrite_confirmation()
    apply_registered_canvas_lock()
    # Keep persisted workflow selection and fresh-workflow authoring explicit:
    # New Workflow is non-destructive and starts with no workflow_id attached.
    apply_new_workflow_action()
    # Stable feature identity remains a backend implementation detail.  The
    # canvas asks only for generic source preparation consent.
    apply_source_preparation_ui()
    # Saved/native source edits waiting for Update Changes must be visible on
    # every dependent operation/output node.
    apply_pending_source_stale_visuals()
    # Mirror the same pending state in Changes: source status, affected outputs,
    # changed-field summary, and Update Changes enablement.
    apply_changes_status_ui()
    # Real-case beta: direct GeoPackage vector-sublayer selection, GPKG delivery,
    # source-container protection and high-contrast branding.
    apply_gpkg_realcase_support()
    # Must remain last. Reinstall the GPKG SOURCE chooser and reinsert the toolbar
    # QAction only after the final black icon is assigned, avoiding stale QGIS UI.
    apply_realcase_finalizer()
    return CanvasRecoveryAtOncePlugin(iface)
