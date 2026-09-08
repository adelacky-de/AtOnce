"""Feature-level lineage contract and stable source-key helpers.

The helpers in this module deliberately avoid QGIS imports so the identity rules
can be unit-tested independently from provider/runtime behavior.
"""

import json
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, Mapping, Optional, Tuple
from uuid import UUID, uuid4


@dataclass(frozen=True)
class FeatureLineage:
    workflow_id: str
    source_layer_id: str
    source_feature_key: str
    current_layer_id: Optional[str] = None
    export_id: Optional[str] = None
    revision: Optional[int] = None


@dataclass(frozen=True, order=True)
class FeatureAncestor:
    """One immutable source feature contributing to a derived feature."""

    source_layer_id: str
    source_feature_key: str

    @property
    def identity(self) -> str:
        return f"{self.source_layer_id}:{self.source_feature_key}"


LINEAGE_FIELD_WORKFLOW = "_atonce_workflow_id"
# G7a's explicit graph contract names the same ownership value without the
# legacy suffix. Keep the v0.x writer field above for legacy compatibility.
LINEAGE_FIELD_WORKFLOW_EXPLICIT = "_atonce_workflow"
LINEAGE_FIELD_SOURCE_LAYER = "_atonce_source_layer"
LINEAGE_FIELD_SOURCE_KEY = "_atonce_source_key"
LINEAGE_FIELD_ANCESTORS = "_atonce_ancestors"
LINEAGE_FIELD_EXPORT = "_atonce_export_id"
LINEAGE_FIELD_REVISION = "_atonce_rev"

DERIVED_LINEAGE_FIELDS = (
    LINEAGE_FIELD_WORKFLOW,
    LINEAGE_FIELD_WORKFLOW_EXPLICIT,
    LINEAGE_FIELD_SOURCE_LAYER,
    LINEAGE_FIELD_SOURCE_KEY,
    LINEAGE_FIELD_ANCESTORS,
)

RESERVED_LINEAGE_FIELDS = (
    LINEAGE_FIELD_WORKFLOW,
    LINEAGE_FIELD_WORKFLOW_EXPLICIT,
    LINEAGE_FIELD_SOURCE_LAYER,
    LINEAGE_FIELD_SOURCE_KEY,
    LINEAGE_FIELD_ANCESTORS,
    LINEAGE_FIELD_EXPORT,
    LINEAGE_FIELD_REVISION,
)


def user_field_names(names: Iterable[str]) -> Tuple[str, ...]:
    """Return persisted field names that are safe to expose as user data."""

    return tuple(
        str(name)
        for name in names
        if str(name) and str(name) not in RESERVED_LINEAGE_FIELDS
    )


def canonical_feature_ancestors(ancestors) -> Tuple[FeatureAncestor, ...]:
    """Normalize ancestor values into deterministic, duplicate-free identities."""

    normalized = set()
    for item in ancestors or ():
        if isinstance(item, FeatureAncestor):
            layer_id, feature_key = item.source_layer_id, item.source_feature_key
        elif isinstance(item, Mapping):
            layer_id = item.get("source_layer_id") or item.get("layer")
            feature_key = item.get("source_feature_key") or item.get("key")
        else:
            try:
                layer_id, feature_key = item[0], item[1]
            except (IndexError, KeyError, TypeError):
                continue
        layer_id = str(layer_id or "").strip()
        feature_key = str(feature_key or "").strip()
        if layer_id and feature_key:
            normalized.add((layer_id, feature_key))
    return tuple(
        FeatureAncestor(layer_id, feature_key)
        for layer_id, feature_key in sorted(normalized)
    )


def encode_feature_ancestors(ancestors) -> str:
    """Serialize multi-parent lineage as deterministic JSON for QGIS fields."""

    values = canonical_feature_ancestors(ancestors)
    return json.dumps(
        [
            {
                "source_layer_id": item.source_layer_id,
                "source_feature_key": item.source_feature_key,
            }
            for item in values
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) if values else ""


def decode_feature_ancestors(value) -> Tuple[FeatureAncestor, ...]:
    """Decode current JSON ancestry and the pre-review dissolve delimiter format."""

    if value is None:
        return ()
    text = str(value).strip()
    if not text:
        return ()
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        payload = [
            item.split("|", 1)
            for item in text.split(";")
            if "|" in item
        ]
    if not isinstance(payload, (list, tuple)):
        return ()
    return canonical_feature_ancestors(payload)


def feature_ancestors(values: Mapping[str, object], fallback_layer="", fallback_key="") -> Tuple[FeatureAncestor, ...]:
    """Read durable ancestry, falling back to the normal single-parent fields."""

    ancestors = decode_feature_ancestors(values.get(LINEAGE_FIELD_ANCESTORS))
    if ancestors:
        return ancestors
    layer_id = str(values.get(LINEAGE_FIELD_SOURCE_LAYER) or fallback_layer or "").strip()
    feature_key = str(values.get(LINEAGE_FIELD_SOURCE_KEY) or fallback_key or "").strip()
    return canonical_feature_ancestors(((layer_id, feature_key),))


def derived_feature_identity(
    values: Mapping[str, object],
    fallback_layer="",
    fallback_key="",
    operation_context="",
) -> str:
    """Return a deterministic identity for raw or derived feature records.

    Raw-source uniqueness remains keyed by source layer and UUID.  Derived
    records may legitimately share that primary UUID, so canonical ancestry is
    the identity whenever it is present.  ``operation_context`` is optional
    presentation/execution context for operations that can emit two rows with
    the same contributor set.
    """

    ancestors = feature_ancestors(values, fallback_layer, fallback_key)
    if ancestors:
        identity = "ancestors:" + "|".join(item.identity for item in ancestors)
    else:
        identity = f"{str(fallback_layer or '').strip()}:{str(fallback_key or '').strip()}"
    context = str(operation_context or "").strip()
    return f"{identity}|operation:{context}" if context else identity


class SourceKeyError(ValueError):
    """Raised when existing source identity cannot be trusted safely."""


@dataclass(frozen=True)
class SourceKeyPlan:
    """Preflight result for one source layer.

    ``existing`` contains canonical UUID strings that must remain unchanged.
    ``assignments`` contains newly generated UUIDs for currently blank features.
    The provider mutation happens later, only after every source passes preflight.
    """

    existing: Dict[int, str] = field(default_factory=dict)
    assignments: Dict[int, str] = field(default_factory=dict)

    @property
    def all_keys(self) -> Dict[int, str]:
        values = dict(self.existing)
        values.update(self.assignments)
        return values


def canonical_source_key(value) -> Optional[str]:
    """Return a canonical UUID string, ``None`` for blank, or raise if malformed."""

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = UUID(text)
    except (TypeError, ValueError, AttributeError) as exc:
        raise SourceKeyError(f"Invalid AtOnce source key: {text!r}") from exc
    return str(parsed)


def plan_source_keys(
    feature_values: Iterable[Tuple[int, object]],
    key_factory: Callable[[], object] = uuid4,
) -> SourceKeyPlan:
    """Validate existing keys and plan UUIDs for blank source features.

    Existing non-empty keys are immutable. Duplicate or malformed keys block the
    entire preflight rather than being silently rewritten. New keys are generated
    only in memory here; callers apply them after all source layers have passed.
    """

    existing: Dict[int, str] = {}
    missing = []
    seen = {}

    for feature_id, raw_value in feature_values:
        key = canonical_source_key(raw_value)
        if key is None:
            missing.append(feature_id)
            continue
        if key in seen:
            raise SourceKeyError(
                "Duplicate AtOnce source key "
                f"{key!r} on feature IDs {seen[key]} and {feature_id}."
            )
        seen[key] = feature_id
        existing[feature_id] = key

    assignments: Dict[int, str] = {}
    reserved = set(seen)
    for feature_id in missing:
        while True:
            generated = canonical_source_key(key_factory())
            if generated and generated not in reserved:
                break
        reserved.add(generated)
        assignments[feature_id] = generated

    return SourceKeyPlan(existing=existing, assignments=assignments)
