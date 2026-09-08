"""Keep source-lineage implementation details out of the canvas user experience.

Stable feature identity is an internal AtOnce mechanism.  Users should only be
asked for consent to prepare a source for reliable change tracking; internal
field names, UUID terminology, and lineage-key implementation details stay in
the backend.
"""

from qgis.PyQt.QtWidgets import QMessageBox


_APPLIED = False


def apply_source_preparation_ui():
    """Install generic source-preparation copy without changing identity logic."""

    global _APPLIED
    if _APPLIED:
        return

    from .plugin import AtOncePlugin
    from .services.source_identity_service import SourceIdentityError

    def prepare_explicit_source_identity(self, workflow):
        preflight = self.source_identity_service.preflight(workflow)

        if preflight.blocked:
            box = QMessageBox(self.iface.mainWindow())
            box.setWindowTitle("Source preparation failed")
            box.setIcon(QMessageBox.Critical)
            box.setText(
                "AtOnce cannot prepare one or more source layers for change tracking."
            )
            box.setInformativeText(
                "Check that the source layers are still loaded, supported, and available "
                "for editing, then try Register again. No workflow output was created."
            )
            source_names = "\n".join(
                f"• {item.source_name}" for item in preflight.blocked
            )
            if source_names:
                box.setDetailedText("Source layer(s) requiring attention:\n" + source_names)
            box.addButton("OK", QMessageBox.AcceptRole)
            box.exec_()
            return False

        if not preflight.writes_required:
            return True

        affected = "\n".join(
            f"• {item.source_name}: {item.assignment_count} feature(s)"
            for item in preflight.needs_initialization
        )
        box = QMessageBox(self.iface.mainWindow())
        box.setWindowTitle("Prepare source for change tracking")
        box.setIcon(QMessageBox.Warning)
        box.setText(
            "AtOnce needs to prepare this source before the workflow can run."
        )
        box.setInformativeText(
            "This one-time preparation lets AtOnce track future source changes reliably. "
            "It is managed automatically and does not change your business attributes."
        )
        if affected:
            box.setDetailedText(
                "Source layer(s) to prepare:\n"
                + affected
                + "\n\nNo workflow output will be created if you cancel."
            )
        continue_button = box.addButton("Continue", QMessageBox.AcceptRole)
        cancel = box.addButton("Cancel", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.exec_()
        if box.clickedButton() is not continue_button:
            return False

        try:
            self.source_identity_service.initialize(preflight)
        except SourceIdentityError:
            self.iface.messageBar().pushCritical(
                "AtOnce",
                "Source preparation failed. No workflow output was created. "
                "Check the source layer and try again.",
            )
            return False
        return True

    AtOncePlugin._prepare_explicit_source_identity = prepare_explicit_source_identity
    _APPLIED = True
