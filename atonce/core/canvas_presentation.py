"""Persistence helpers for non-executable canvas presentation state.

AtOnce keeps text notes, group boards and other authoring-only presentation data
inside a reserved dependency-node metadata namespace.  The dependency graph remains
the persisted workflow container, while execution ignores this metadata entirely.
"""

from copy import deepcopy

from ..models.dependency_graph import DependencyGraph


PRESENTATION_METADATA_KEY = "_atonce_canvas_presentation"


def empty_canvas_presentation():
    return {"texts": [], "groups": []}


def normalise_canvas_presentation(value):
    payload = deepcopy(value) if isinstance(value, dict) else {}
    texts = [dict(item) for item in payload.get("texts", []) if isinstance(item, dict)]
    groups = [dict(item) for item in payload.get("groups", []) if isinstance(item, dict)]
    return {"texts": texts, "groups": groups}


def extract_canvas_presentation(graph: DependencyGraph):
    """Return the first valid persisted presentation payload from the graph."""

    for node in graph.nodes:
        payload = node.metadata.get(PRESENTATION_METADATA_KEY)
        if isinstance(payload, dict):
            return normalise_canvas_presentation(payload)
    return empty_canvas_presentation()


def graph_with_canvas_presentation(graph: DependencyGraph, presentation):
    """Return a graph copy carrying presentation metadata on every node.

    Duplicating the small payload makes the presentation resilient to deleting the
    node that happened to be first when the workflow was previously registered.
    """

    result = DependencyGraph.from_dict(graph.to_dict())
    payload = normalise_canvas_presentation(presentation)
    for node in result.nodes:
        node.metadata[PRESENTATION_METADATA_KEY] = deepcopy(payload)
    return result
