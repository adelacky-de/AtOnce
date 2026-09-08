"""Recoverable QGIS source attribute write-back for preflighted Writable changes."""

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from qgis.core import (
    QgsExpression,
    QgsFeatureRequest,
    QgsMapLayerType,
    QgsProject,
    QgsVectorDataProvider,
)

from ..core.lineage import LINEAGE_FIELD_SOURCE_KEY
from ..core.sync_planning import SourceWriteProposal, values_equal


class SourceWriteError(RuntimeError):
    """Raised when source preflight, commit or recovery cannot complete safely."""


@dataclass(frozen=True)
class PreparedSourceChange:
    # Current QGIS project binding used for provider mutation/recovery.
    layer_id: str
    layer_name: str
    feature_id: int
    source_key: str
    field_name: str
    field_index: int
    before_value: object
    after_value: object
    # Immutable historical AtOnce source identity retained in audit/semantic identity.
    source_lineage_id: str = ""

    @property
    def identity(self) -> Tuple[str, str, str]:
        return self.source_lineage_id or self.layer_id, self.source_key, self.field_name


@dataclass(frozen=True)
class SourceWritePlan:
    changes: List[PreparedSourceChange]

    @property
    def by_layer(self) -> Dict[str, List[PreparedSourceChange]]:
        grouped: Dict[str, List[PreparedSourceChange]] = {}
        for item in self.changes:
            grouped.setdefault(item.layer_id, []).append(item)
        return grouped


@dataclass(frozen=True)
class SourceWriteReceipt:
    changes: List[PreparedSourceChange]


class SourceWriter:
    def __init__(self, project: Optional[QgsProject] = None):
        self.project = project or QgsProject.instance()

    def preflight(
        self,
        proposals: Iterable[SourceWriteProposal],
        source_bindings: Optional[Dict[str, str]] = None,
    ) -> SourceWritePlan:
        """Resolve stable lineage proposals onto current QGIS layer bindings."""

        prepared: List[PreparedSourceChange] = []
        seen = set()
        bindings = dict(source_bindings or {})

        for proposal in proposals:
            identity = proposal.identity
            if identity in seen:
                continue
            seen.add(identity)

            bound_layer_id = bindings.get(proposal.source_layer_id, proposal.source_layer_id)
            layer = self.project.mapLayer(bound_layer_id)
            if layer is None or layer.type() != QgsMapLayerType.VectorLayer:
                raise SourceWriteError(
                    f"Source layer '{proposal.source_layer_name}' is missing or is not a vector layer."
                )
            self._require_writable_layer(layer)

            field_index = layer.fields().indexOf(proposal.source_field_name)
            if field_index < 0:
                raise SourceWriteError(
                    f"Source field '{proposal.source_field_name}' no longer exists on '{layer.name()}'."
                )
            if proposal.source_field_name.startswith("_atonce_"):
                raise SourceWriteError("AtOnce internal metadata can never be written back as business data.")

            feature = self._feature_by_source_key(layer, proposal.source_feature_key)
            if feature is None:
                raise SourceWriteError(
                    f"Exact source feature {proposal.source_feature_key!r} could not be resolved on '{layer.name()}'."
                )
            current = feature[proposal.source_field_name]
            if not values_equal(current, proposal.expected_current_value):
                raise SourceWriteError(
                    f"Source changed after scan for '{layer.name()}' / {proposal.source_field_name}; "
                    "rescan before applying."
                )

            prepared.append(
                PreparedSourceChange(
                    layer_id=layer.id(),
                    layer_name=layer.name(),
                    feature_id=int(feature.id()),
                    source_key=proposal.source_feature_key,
                    field_name=proposal.source_field_name,
                    field_index=field_index,
                    before_value=current,
                    after_value=proposal.new_value,
                    source_lineage_id=proposal.source_layer_id,
                )
            )

        if not prepared:
            raise SourceWriteError("No source changes remain after write preflight.")
        prepared.sort(key=lambda item: item.identity)
        return SourceWritePlan(prepared)

    def apply(self, plan: SourceWritePlan) -> SourceWriteReceipt:
        committed: List[PreparedSourceChange] = []
        try:
            for layer_id, changes in plan.by_layer.items():
                layer = self.project.mapLayer(layer_id)
                if layer is None:
                    raise SourceWriteError(f"Source layer {layer_id!r} disappeared before commit.")
                self._require_writable_layer(layer)
                self._verify_before_images(layer, changes)
                self._commit_layer(layer, changes, use_after=True)
                committed.extend(changes)
        except Exception as exc:
            recovery_errors = self._restore_changes(committed)
            if recovery_errors:
                error = SourceWriteError(
                    f"Source write failed and recovery was incomplete: {exc}. "
                    f"Recovery errors: {' | '.join(recovery_errors)}"
                )
                error.recovery_incomplete = True
                raise error from exc
            if isinstance(exc, SourceWriteError):
                raise
            raise SourceWriteError(f"Source write failed; committed changes were restored: {exc}") from exc

        return SourceWriteReceipt(list(committed))

    def restore(self, receipt: SourceWriteReceipt) -> None:
        errors = self._restore_changes(receipt.changes)
        if errors:
            raise SourceWriteError("Could not fully restore source before-images: " + " | ".join(errors))

    def _restore_changes(self, changes: Iterable[PreparedSourceChange]) -> List[str]:
        grouped: Dict[str, List[PreparedSourceChange]] = {}
        for item in changes:
            grouped.setdefault(item.layer_id, []).append(item)
        errors = []
        for layer_id, items in reversed(list(grouped.items())):
            layer = self.project.mapLayer(layer_id)
            if layer is None:
                errors.append(f"source layer {layer_id!r} is missing during recovery")
                continue
            try:
                self._require_writable_layer(layer)
                for item in items:
                    feature = self._feature_by_source_key(layer, item.source_key)
                    if feature is None:
                        raise SourceWriteError(
                            f"feature {item.source_key!r} disappeared during recovery"
                        )
                    if not values_equal(feature[item.field_name], item.after_value):
                        raise SourceWriteError(
                            f"field {item.field_name!r} changed again before recovery"
                        )
                self._commit_layer(layer, items, use_after=False)
            except Exception as exc:
                errors.append(f"{layer.name()}: {exc}")
        return errors

    def _verify_before_images(self, layer, changes: Iterable[PreparedSourceChange]) -> None:
        for item in changes:
            feature = self._feature_by_source_key(layer, item.source_key)
            if feature is None:
                raise SourceWriteError(
                    f"Exact source feature {item.source_key!r} disappeared before commit."
                )
            current = feature[item.field_name]
            if not values_equal(current, item.before_value):
                raise SourceWriteError(
                    f"Source changed after preflight for '{layer.name()}' / {item.field_name}; batch blocked."
                )

    @staticmethod
    def _require_writable_layer(layer) -> None:
        if layer.isEditable():
            raise SourceWriteError(
                f"Source layer '{layer.name()}' already has an active edit session. Save or roll it back first."
            )
        is_read_only = getattr(layer, "isReadOnly", None)
        if callable(is_read_only) and is_read_only():
            raise SourceWriteError(f"Source layer '{layer.name()}' is read-only.")
        capabilities = layer.dataProvider().capabilities()
        if not (capabilities & QgsVectorDataProvider.ChangeAttributeValues):
            raise SourceWriteError(
                f"Source layer '{layer.name()}' does not support attribute updates."
            )

    def _commit_layer(self, layer, changes: Iterable[PreparedSourceChange], use_after: bool) -> None:
        if not layer.startEditing():
            raise SourceWriteError(f"Could not start edit session for '{layer.name()}'.")
        try:
            for item in changes:
                value = item.after_value if use_after else item.before_value
                field_index = layer.fields().indexOf(item.field_name)
                if field_index < 0:
                    raise SourceWriteError(
                        f"Field '{item.field_name}' disappeared from '{layer.name()}' during commit."
                    )
                feature = self._feature_by_source_key(layer, item.source_key)
                if feature is None:
                    raise SourceWriteError(
                        f"Feature {item.source_key!r} disappeared from '{layer.name()}' during commit."
                    )
                if not layer.changeAttributeValue(feature.id(), field_index, value):
                    raise SourceWriteError(
                        f"Could not update '{item.field_name}' on '{layer.name()}'."
                    )
            if not layer.commitChanges():
                details = "; ".join(layer.commitErrors()) or "unknown provider error"
                raise SourceWriteError(
                    f"Could not commit source changes for '{layer.name()}': {details}"
                )
        except Exception:
            if layer.isEditable():
                layer.rollBack()
            raise

    @staticmethod
    def _feature_by_source_key(layer, source_key):
        key_index = layer.fields().indexOf(LINEAGE_FIELD_SOURCE_KEY)
        if key_index < 0:
            return None
        quoted = QgsExpression.quotedString(str(source_key))
        request = QgsFeatureRequest().setFilterExpression(
            f'"{LINEAGE_FIELD_SOURCE_KEY}" = {quoted}'
        )
        matches = list(layer.getFeatures(request))
        return matches[0] if len(matches) == 1 else None
