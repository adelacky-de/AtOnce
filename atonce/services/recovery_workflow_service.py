"""Workflow service guardrails introduced by source recovery generations."""

from .workflow_service import WorkflowService


class RecoveryWorkflowService(WorkflowService):
    """Preserve source-authority generation across ordinary configuration edits."""

    def register(self, definition):
        existing = self.store.load_workflow(definition.workflow_id)
        if existing is not None:
            definition.lineage_generation = existing.lineage_generation
        return super().register(definition)
