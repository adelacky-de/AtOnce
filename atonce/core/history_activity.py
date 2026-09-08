"""Pure counters for persisted History activity."""


def sync_activity_counts(audits):
    """Return activity counts that can be reconstructed from persisted sync audits."""

    audits = list(audits or [])
    return {
        "syncs": len(audits),
        "changes": sum(len(audit.changes) for audit in audits),
        "refreshes": sum(
            1 for audit in audits if audit.refresh_revision_number is not None
        ),
    }
