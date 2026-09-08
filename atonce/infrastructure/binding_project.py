"""QgsProject-compatible resolver for stable source lineage IDs.

Historical AtOnce source IDs must always resolve through the workflow's current
binding first. This remains true even when the old QGIS layer is still loaded in
the project after an explicit relink.
"""


class SourceBindingProject:
    def __init__(self, project, store):
        self._project = project
        self._store = store

    def mapLayer(self, layer_id):  # noqa: N802 - QgsProject API compatibility
        if not layer_id:
            return None

        workflow = self._store.load_workflow()
        if workflow is not None:
            source = workflow.source_by_lineage_id(str(layer_id))
            if source is not None and source.current_layer_id:
                return self._project.mapLayer(source.current_layer_id)

        # IDs that are not immutable AtOnce source identities are normal current
        # QGIS layer IDs (derived layer, prepared source commit/recovery binding,
        # etc.) and resolve directly.
        return self._project.mapLayer(layer_id)

    def __getattr__(self, name):
        return getattr(self._project, name)
