"""QGIS-free state for one open AtOnce dock session."""

from dataclasses import dataclass, field


@dataclass
class ChangeSessionState:
    """Pending source edits and History rows; never persisted to the project."""

    changed_source_ids: set = field(default_factory=set)
    history_rows: list = field(default_factory=list)

    def record(self, source_id, rows):
        self.changed_source_ids.add(str(source_id))
        self.history_rows.extend(tuple(row) for row in rows or ())

    def mark_updated(self, source_ids):
        self.changed_source_ids.difference_update(str(item) for item in source_ids)

    def complete_update(self, source_ids, succeeded):
        if succeeded:
            self.mark_updated(source_ids)

    def clear(self):
        self.changed_source_ids.clear()
        self.history_rows.clear()
