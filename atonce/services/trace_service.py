"""Trace a derived/export record back to its registered source feature."""


class TraceService:
    def resolve_source(self, lineage):
        raise NotImplementedError("Lineage resolution is not implemented yet")
