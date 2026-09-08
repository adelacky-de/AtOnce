"""Presentation-state behavior for text, groups, positions and displayed fields."""

from dataclasses import replace

from qgis.PyQt.QtCore import QRectF
from qgis.PyQt.QtWidgets import QInputDialog

from ..models.dependency_graph import NodeKind


class CanvasPresentationMixin:
    """Manage presentation-only state without touching graph execution semantics."""

    def _node_position_committed(self, node_id, position):
        node = self._graph.node_map().get(str(node_id))
        if node is None:
            return
        metadata = dict(node.metadata)
        canvas = dict(metadata.get("canvas") or {})
        canvas.update({"x": float(position.x()), "y": float(position.y())})
        metadata["canvas"] = canvas
        self._graph.nodes = [
            replace(node, metadata=metadata) if item.node_id == node.node_id else item
            for item in self._graph.nodes
        ]
        self._assign_node_group(node.node_id)
        self._update_scene_bounds()
        self.graph_changed.emit()

    def _source_field_toggled(self, node_id, field, checked):
        node = self._graph.node_map().get(str(node_id))
        if node is None or node.kind != NodeKind.SOURCE:
            return
        selected = list(self._selected_source_fields(node))
        if checked and field not in selected:
            selected.append(field)
        if not checked:
            selected = [item for item in selected if item != field]
        metadata = dict(node.metadata)
        metadata["display_fields"] = selected
        self._graph.nodes = [
            replace(node, metadata=metadata) if item.node_id == node.node_id else item
            for item in self._graph.nodes
        ]
        self.graph_changed.emit()

    def _edit_text(self, text_id):
        payload = next(
            (
                item
                for item in self._presentation.get("texts", [])
                if item.get("id") == text_id
            ),
            None,
        )
        if payload is None:
            return
        text, accepted = QInputDialog.getMultiLineText(
            self, "Text block", "Text", str(payload.get("text") or "")
        )
        if accepted:
            payload["text"] = str(text)
            self._render_graph()
            self.graph_changed.emit()

    def _text_position_committed(self, text_id, position):
        payload = next(
            (
                item
                for item in self._presentation.get("texts", [])
                if item.get("id") == text_id
            ),
            None,
        )
        if payload is not None:
            payload.update({"x": float(position.x()), "y": float(position.y())})
            self._update_scene_bounds()
            self.graph_changed.emit()

    def _rename_group(self, group_id):
        group = self._group_payload(group_id)
        if group is None:
            return
        title, accepted = QInputDialog.getText(
            self,
            "Group board",
            "Name",
            text=str(group.get("title") or "Group"),
        )
        if accepted:
            group["title"] = str(title).strip() or "Group"
            self._render_graph()
            self.graph_changed.emit()

    def _group_payload(self, group_id):
        return next(
            (
                item
                for item in self._presentation.get("groups", [])
                if item.get("id") == group_id
            ),
            None,
        )

    def _group_moved_delta(self, group_id, delta):
        group = self._group_payload(group_id)
        if group is None:
            return
        for node_id in tuple(group.get("members") or ()):
            item = self._items.get(str(node_id))
            if item is not None:
                item.setPos(item.pos() + delta)
        self._refresh_connections()

    def _group_geometry_committed(self, group_id, x, y, width, height):
        group = self._group_payload(group_id)
        if group is None:
            return
        group.update(
            {
                "x": float(x),
                "y": float(y),
                "width": float(width),
                "height": float(height),
            }
        )
        for node_id in tuple(group.get("members") or ()):
            item = self._items.get(str(node_id))
            if item is not None:
                self._node_position_committed(node_id, item.pos())
        self._recompute_group_members(group_id)
        self._update_scene_bounds()
        self.graph_changed.emit()

    @staticmethod
    def _group_scene_rect(group):
        return QRectF(
            float(group.get("x", 0)),
            float(group.get("y", 0)),
            float(group.get("width", 460)),
            float(group.get("height", 260)),
        )

    def _assign_node_group(self, node_id):
        item = self._items.get(str(node_id))
        if item is None:
            return
        center = item.sceneBoundingRect().center()
        chosen = None
        for group in reversed(self._presentation.get("groups", [])):
            if self._group_scene_rect(group).contains(center):
                chosen = group
                break
        for group in self._presentation.get("groups", []):
            members = [str(value) for value in group.get("members", [])]
            if group is chosen:
                if str(node_id) not in members:
                    members.append(str(node_id))
            else:
                members = [value for value in members if value != str(node_id)]
            group["members"] = members

    def _recompute_group_members(self, group_id):
        group = self._group_payload(group_id)
        if group is None:
            return
        rect = self._group_scene_rect(group)
        group["members"] = [
            node_id
            for node_id, item in self._items.items()
            if rect.contains(item.sceneBoundingRect().center())
        ]
