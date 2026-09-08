"""Boundary for spreadsheet snapshots and returned XLSX comparison."""


class XlsxGateway:
    def write_linked_export(self, export_ref, rows):
        raise NotImplementedError

    def read_linked_export(self, export_ref):
        raise NotImplementedError

    def compare_with_snapshot(self, export_ref):
        raise NotImplementedError
