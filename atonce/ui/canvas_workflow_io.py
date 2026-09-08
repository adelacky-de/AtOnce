"""Portable .atonce.json import/export and workflow-definition assembly."""

import json

from qgis.PyQt.QtWidgets import QFileDialog

from ..core.canvas_presentation import graph_with_canvas_presentation
from ..models.dependency_graph import NodeKind
from ..models.workflow import WorkflowDefinition


class CanvasWorkflowIOMixin:
    """Keep portable workflow I/O separate from canvas interaction code."""

    def _export_workflow(self):
        definition = self.definition()
        if definition is None:
            self.message_requested.emit("Add and configure workflow blocks before Export.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export AtOnce workflow",
            f"{definition.name or 'workflow'}.atonce.json",
            "AtOnce workflow (*.atonce.json)",
        )
        if not path:
            return
        if not path.lower().endswith(".atonce.json"):
            path += ".atonce.json"
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(definition.to_dict(), handle, ensure_ascii=False, indent=2)
        except OSError as exc:
            self.message_requested.emit(f"Could not export workflow: {exc}")
            return
        self.message_requested.emit("Workflow exported.")

    def _import_workflow(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import AtOnce workflow",
            "",
            "AtOnce workflow (*.atonce.json);;JSON (*.json)",
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            workflow = WorkflowDefinition.from_dict(payload)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self.message_requested.emit(f"Could not import workflow: {exc}")
            return
        self.set_workflow(workflow)
        self._imported_draft = True
        self.graph_changed.emit()
        self.message_requested.emit(
            "Workflow imported into the current canvas. Register to overwrite its persisted workflow identity."
        )

    def definition(self):
        if not self._graph.nodes or self._legacy_display:
            return None
        from ..core.freeform_workflow import build_freeform_workflow

        refs = [
            self._source_refs[str(node.metadata.get("source_lineage_id") or "")]
            for node in self._graph.nodes
            if node.kind == NodeKind.SOURCE
            and str(node.metadata.get("source_lineage_id") or "") in self._source_refs
        ]
        workflow_id = self._workflow.workflow_id if self._workflow is not None else None
        name = self.workflow_name_edit.text().strip() or (
            self._workflow.name if self._workflow is not None else "Untitled workflow"
        )
        return build_freeform_workflow(
            name=name,
            sources=refs,
            graph=graph_with_canvas_presentation(self._graph, self._presentation),
            workflow_id=workflow_id,
        )
