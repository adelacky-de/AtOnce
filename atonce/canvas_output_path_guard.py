"""Protect delivery files from cross-workflow overwrite.

A workflow may refresh its own configured output path, but two different
workflow identities must not silently own the same delivery file.  This guard
runs at registration so the collision is reported before execution or output
mutation.
"""

import os


_APPLIED = False


def _normalized_path(path):
    text = str(path or "").strip()
    if not text:
        return ""
    return os.path.abspath(os.path.expanduser(text)).casefold()


def apply_output_path_guard():
    """Reject output paths already owned by another registered workflow."""

    global _APPLIED
    if _APPLIED:
        return

    from .services.workflow_service import (
        ValidationIssue,
        ValidationResult,
        WorkflowService,
    )

    original_register = WorkflowService.register

    def register(self, definition):
        owned = {}
        for existing in self.load_all():
            if existing is None or existing.workflow_id == definition.workflow_id:
                continue
            for delivery in getattr(existing, "forward_deliveries", ()) or ():
                path = str(getattr(delivery, "path", "") or "").strip()
                key = _normalized_path(path)
                if key:
                    owned.setdefault(key, (existing.name, path))

        collisions = []
        for delivery in getattr(definition, "forward_deliveries", ()) or ():
            path = str(getattr(delivery, "path", "") or "").strip()
            key = _normalized_path(path)
            if not key or key not in owned:
                continue
            owner_name, owner_path = owned[key]
            collisions.append(
                ValidationIssue(
                    "output_path_owned_by_other_workflow",
                    f"Output path '{path or owner_path}' is already used by workflow "
                    f"'{owner_name}'. Choose a different output path so one workflow "
                    "cannot overwrite another workflow's result.",
                )
            )

        if collisions:
            return ValidationResult(collisions)
        return original_register(self, definition)

    WorkflowService.register = register
    _APPLIED = True
