"""Persistence for AtOnce workflow, dependency graph, export, derived and audit state."""

import json
from pathlib import Path
from typing import List, Optional

from qgis.core import QgsProject

from ..models.derived_state import DerivedState
from ..models.export_revision import ExportRevision
from ..models.graph_node_materialization import GraphNodeMaterializationState
from ..models.graph_run_state import GraphRunState
from ..models.sync_audit import SyncAuditRecord
from ..models.workflow import WorkflowDefinition

PROJECT_SCOPE = "atonce"
WORKFLOWS_KEY = "workflows_json"
EXPORT_REVISIONS_KEY = "export_revisions_json"
DERIVED_STATES_KEY = "derived_states_json"
SYNC_AUDITS_KEY = "sync_audits_json"
GRAPH_RUN_STATES_KEY = "graph_run_states_json"
GRAPH_NODE_MATERIALIZATIONS_KEY = "graph_node_materializations_json"
ACTIVE_WORKFLOW_ID_KEY = "active_workflow_id"
SCHEMA_VERSION_KEY = "schema_version"
STORE_SCHEMA_VERSION = 9


class ProjectStore:
    """Read/write serializable AtOnce state from the active QGIS project."""

    def __init__(self, project: Optional[QgsProject] = None):
        self.project = project or QgsProject.instance()

    def load_workflows(self) -> List[WorkflowDefinition]:
        raw, ok = self.project.readEntry(PROJECT_SCOPE, WORKFLOWS_KEY, "")
        if not ok or not raw:
            return []

        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return []

        if not isinstance(payload, list):
            return []

        workflows = []
        for item in payload:
            if isinstance(item, dict):
                workflows.append(WorkflowDefinition.from_dict(item))
        return workflows

    def load_workflow(self, workflow_id: Optional[str] = None) -> Optional[WorkflowDefinition]:
        workflows = self.load_workflows()
        if workflow_id is None:
            return workflows[0] if workflows else None
        return next((item for item in workflows if item.workflow_id == workflow_id), None)

    def _stored_active_workflow_id(self) -> Optional[str]:
        raw, ok = self.project.readEntry(PROJECT_SCOPE, ACTIVE_WORKFLOW_ID_KEY, "")
        if not ok:
            return None
        value = str(raw or "").strip()
        return value or None

    def load_active_workflow_id(self) -> Optional[str]:
        """Return the selected workflow ID with safe legacy/fallback behavior."""

        workflows = self.load_workflows()
        if not workflows:
            if self._stored_active_workflow_id() is not None:
                self.set_active_workflow_id(None)
            return None
        known_ids = {item.workflow_id for item in workflows}
        stored = self._stored_active_workflow_id()
        if stored in known_ids:
            return stored

        fallback = workflows[0].workflow_id
        if stored is not None:
            # Repair a stale selection without reordering workflows_json.
            self.set_active_workflow_id(fallback)
        return fallback

    def set_active_workflow_id(self, workflow_id: Optional[str]) -> None:
        """Persist workflow selection as additive project metadata only."""

        wanted = str(workflow_id or "").strip()
        known_ids = {item.workflow_id for item in self.load_workflows()}
        if wanted and wanted not in known_ids:
            raise ValueError(f"Workflow {wanted!r} is not registered in this project.")
        self.project.writeEntry(PROJECT_SCOPE, ACTIVE_WORKFLOW_ID_KEY, wanted)
        self.project.setDirty(True)

    def load_active_workflow(self) -> Optional[WorkflowDefinition]:
        """Load the selected workflow, falling back to the first old-project entry."""

        active_id = self.load_active_workflow_id()
        return self.load_workflow(active_id) if active_id else None

    def save_workflow(self, workflow: WorkflowDefinition) -> None:
        graph_errors = workflow.effective_dependency_graph().validate()
        if graph_errors:
            raise ValueError(
                "Cannot persist invalid dependency graph: " + " | ".join(graph_errors)
            )

        workflows = self.load_workflows()
        replaced = False
        for index, existing in enumerate(workflows):
            if existing.workflow_id == workflow.workflow_id:
                workflows[index] = workflow
                replaced = True
                break
        if not replaced:
            workflows.append(workflow)

        payload = json.dumps([item.to_dict() for item in workflows], ensure_ascii=False)
        self.project.writeEntry(PROJECT_SCOPE, WORKFLOWS_KEY, payload)
        self.project.writeEntry(PROJECT_SCOPE, SCHEMA_VERSION_KEY, STORE_SCHEMA_VERSION)
        self.project.setDirty(True)

    def load_export_revisions(self, workflow_id: Optional[str] = None) -> List[ExportRevision]:
        raw, ok = self.project.readEntry(PROJECT_SCOPE, EXPORT_REVISIONS_KEY, "")
        if not ok or not raw:
            return []

        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if not isinstance(payload, list):
            return []

        revisions = [
            ExportRevision.from_dict(item)
            for item in payload
            if isinstance(item, dict)
        ]
        if workflow_id is not None:
            revisions = [item for item in revisions if item.workflow_id == workflow_id]
        return sorted(revisions, key=lambda item: item.revision_number)

    def latest_export_revision(self, workflow_id: str) -> Optional[ExportRevision]:
        revisions = self.load_export_revisions(workflow_id)
        return revisions[-1] if revisions else None

    def next_export_revision_number(self, workflow_id: str) -> int:
        latest = self.latest_export_revision(workflow_id)
        return (latest.revision_number + 1) if latest else 1

    def save_export_revision(self, revision: ExportRevision) -> None:
        if revision.status != "complete":
            raise ValueError("Only completed downstream revisions may be persisted.")

        revisions = self.load_export_revisions()
        if any(item.revision_id == revision.revision_id for item in revisions):
            raise ValueError(f"Export revision {revision.revision_id!r} already exists.")
        revisions.append(revision)

        payload = json.dumps([item.to_dict() for item in revisions], ensure_ascii=False)
        self.project.writeEntry(PROJECT_SCOPE, EXPORT_REVISIONS_KEY, payload)
        self.project.writeEntry(PROJECT_SCOPE, SCHEMA_VERSION_KEY, STORE_SCHEMA_VERSION)
        self.project.setDirty(True)

    def load_derived_states(self) -> List[DerivedState]:
        raw, ok = self.project.readEntry(PROJECT_SCOPE, DERIVED_STATES_KEY, "")
        if not ok or not raw:
            return []
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if not isinstance(payload, list):
            return []
        return [
            DerivedState.from_dict(item)
            for item in payload
            if isinstance(item, dict)
        ]

    def load_derived_state(self, workflow_id: str) -> Optional[DerivedState]:
        states = self.load_derived_states()
        return next((item for item in states if item.workflow_id == workflow_id), None)

    def save_derived_state(self, state: DerivedState) -> None:
        """Upsert the authoritative semantic baseline for the current Layer C."""

        states = self.load_derived_states()
        replaced = False
        for index, existing in enumerate(states):
            if existing.workflow_id == state.workflow_id:
                states[index] = state
                replaced = True
                break
        if not replaced:
            states.append(state)
        payload = json.dumps([item.to_dict() for item in states], ensure_ascii=False)
        self.project.writeEntry(PROJECT_SCOPE, DERIVED_STATES_KEY, payload)
        self.project.writeEntry(PROJECT_SCOPE, SCHEMA_VERSION_KEY, STORE_SCHEMA_VERSION)
        self.project.setDirty(True)

    def load_graph_node_materializations(
        self,
        workflow_id: Optional[str] = None,
    ) -> List[GraphNodeMaterializationState]:
        raw, ok = self.project.readEntry(PROJECT_SCOPE, GRAPH_NODE_MATERIALIZATIONS_KEY, "")
        if not ok or not raw:
            return []
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if not isinstance(payload, list):
            return []
        states = [
            GraphNodeMaterializationState.from_dict(item)
            for item in payload
            if isinstance(item, dict)
        ]
        if workflow_id is not None:
            states = [item for item in states if item.workflow_id == workflow_id]
        return states

    def load_graph_node_materialization(
        self,
        workflow_id: str,
        node_id: str,
    ) -> Optional[GraphNodeMaterializationState]:
        return next(
            (
                item
                for item in self.load_graph_node_materializations(workflow_id)
                if item.node_id == node_id
            ),
            None,
        )

    def save_graph_node_materialization(self, state: GraphNodeMaterializationState) -> None:
        """Upsert explicit graph evidence without touching legacy DerivedState."""

        states = self.load_graph_node_materializations()
        for index, existing in enumerate(states):
            if (existing.workflow_id, existing.node_id) == (state.workflow_id, state.node_id):
                states[index] = state
                break
        else:
            states.append(state)
        payload = json.dumps([item.to_dict() for item in states], ensure_ascii=False)
        self.project.writeEntry(PROJECT_SCOPE, GRAPH_NODE_MATERIALIZATIONS_KEY, payload)
        self.project.writeEntry(PROJECT_SCOPE, SCHEMA_VERSION_KEY, STORE_SCHEMA_VERSION)
        self.project.setDirty(True)

    def delete_graph_node_materialization(self, workflow_id: str, node_id: str) -> None:
        states = [
            item
            for item in self.load_graph_node_materializations()
            if (item.workflow_id, item.node_id) != (workflow_id, node_id)
        ]
        payload = json.dumps([item.to_dict() for item in states], ensure_ascii=False)
        self.project.writeEntry(PROJECT_SCOPE, GRAPH_NODE_MATERIALIZATIONS_KEY, payload)
        self.project.writeEntry(PROJECT_SCOPE, SCHEMA_VERSION_KEY, STORE_SCHEMA_VERSION)
        self.project.setDirty(True)

    def load_sync_audits(self, workflow_id: Optional[str] = None) -> List[SyncAuditRecord]:
        raw, ok = self.project.readEntry(PROJECT_SCOPE, SYNC_AUDITS_KEY, "")
        if not ok or not raw:
            return []
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if not isinstance(payload, list):
            return []
        audits = [
            SyncAuditRecord.from_dict(item)
            for item in payload
            if isinstance(item, dict)
        ]
        if workflow_id is not None:
            audits = [item for item in audits if item.workflow_id == workflow_id]
        return audits

    def save_sync_audit(self, audit: SyncAuditRecord) -> None:
        """Upsert one sync audit so pending state survives later failures/reopen."""

        audits = self.load_sync_audits()
        replaced = False
        for index, existing in enumerate(audits):
            if existing.sync_id == audit.sync_id:
                audits[index] = audit
                replaced = True
                break
        if not replaced:
            audits.append(audit)
        payload = json.dumps([item.to_dict() for item in audits], ensure_ascii=False)
        self.project.writeEntry(PROJECT_SCOPE, SYNC_AUDITS_KEY, payload)
        self.project.writeEntry(PROJECT_SCOPE, SCHEMA_VERSION_KEY, STORE_SCHEMA_VERSION)
        self.project.setDirty(True)

    def latest_sync_audit(self, workflow_id: str) -> Optional[SyncAuditRecord]:
        audits = self.load_sync_audits(workflow_id)
        return audits[-1] if audits else None

    def load_graph_run_states(self, workflow_id: Optional[str] = None) -> List[GraphRunState]:
        raw, ok = self.project.readEntry(PROJECT_SCOPE, GRAPH_RUN_STATES_KEY, "")
        if not ok or not raw:
            return []
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if not isinstance(payload, list):
            return []
        states = [
            GraphRunState.from_dict(item)
            for item in payload
            if isinstance(item, dict)
        ]
        if workflow_id is not None:
            states = [item for item in states if item.workflow_id == workflow_id]
        return states

    def latest_graph_run_state(self, workflow_id: str) -> Optional[GraphRunState]:
        states = self.load_graph_run_states(workflow_id)
        return states[-1] if states else None

    def save_graph_run_state(self, state: GraphRunState) -> None:
        """Append one completed selective propagation record."""

        states = self.load_graph_run_states()
        if any(item.run_id == state.run_id for item in states):
            raise ValueError(f"Graph run {state.run_id!r} already exists.")
        states.append(state)
        payload = json.dumps([item.to_dict() for item in states], ensure_ascii=False)
        self.project.writeEntry(PROJECT_SCOPE, GRAPH_RUN_STATES_KEY, payload)
        self.project.writeEntry(PROJECT_SCOPE, SCHEMA_VERSION_KEY, STORE_SCHEMA_VERSION)
        self.project.setDirty(True)

    def output_fingerprint(self, workflow_id: str, path: str) -> Optional[str]:
        """Return the newest recorded fingerprint for an AtOnce-owned output path."""

        candidate = str(Path(path).expanduser().resolve())
        for revision in reversed(self.load_export_revisions(workflow_id)):
            if revision.gpkg_refreshed and str(Path(revision.gpkg_path).expanduser().resolve()) == candidate:
                return revision.effective_gpkg_fingerprint or None
            for output in revision.outputs:
                if str(Path(output.path).expanduser().resolve()) == candidate:
                    return output.sha256 or None
            for output in revision.delivery_outputs:
                if str(Path(output.path).expanduser().resolve()) == candidate:
                    return output.bundle_sha256 or output.sha256 or None
        return None

    def path_is_owned_output(self, workflow_id: str, path: str) -> bool:
        """Return whether a completed revision proves this path is AtOnce-owned."""

        return self.output_fingerprint(workflow_id, path) is not None
