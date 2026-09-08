"""QGIS-free helpers for concise operation-relevant canvas field summaries."""

import re


_QUOTED_FIELD = re.compile(r'"((?:""|[^"])*)"')


def expression_field_names(expression):
    """Return QGIS-style quoted field names in first-seen order."""

    result = []
    for match in _QUOTED_FIELD.finditer(str(expression or "")):
        name = match.group(1).replace('""', '"')
        if name and name not in result:
            result.append(name)
    return tuple(result)


def _list_value(value):
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    return tuple(str(item).strip() for item in value if str(item).strip())


def _structured_input_fields(rows, keys=("field",)):
    result = []
    allowed = {str(key) for key in keys}
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        for key in allowed:
            text = str(row.get(key) or "").strip()
            if text and text not in result:
                result.append(text)
    return tuple(result)


def operation_input_fields(operation_kind, parameters, target_port="input"):
    """Return fields consumed from one named input port.

    This is presentation metadata only. It follows explicit operation parameters
    and never guesses by matching arbitrary field names.
    """

    kind = str(operation_kind or "")
    values = dict(parameters or {})
    port = str(target_port or "input")

    if kind == "filter":
        return expression_field_names(values.get("expression"))

    if kind == "join":
        if port == "left":
            key = str(values.get("left_field") or "").strip()
            return (key,) if key else ()
        if port == "right":
            key = str(values.get("right_field") or "").strip()
            copied = _list_value(values.get("right_fields"))
            return tuple(dict.fromkeys(([key] if key else []) + list(copied)))
        return ()

    if kind == "compare_changes":
        compared = _list_value(values.get("compare_fields"))
        if port == "previous":
            key = str(values.get("previous_key_field") or "").strip()
        elif port == "current":
            key = str(values.get("current_key_field") or "").strip()
        else:
            key = ""
        return tuple(dict.fromkeys(([key] if key else []) + list(compared)))

    if kind == "spatial_join":
        return _list_value(values.get("fields")) if port == "join" else ()

    if kind == "field_mapping":
        return tuple(
            dict.fromkeys(
                str(row.get("source_field") or "").strip()
                for row in values.get("mappings") or ()
                if isinstance(row, dict) and str(row.get("source_field") or "").strip()
            )
        )

    if kind == "rename_field":
        value = str(values.get("source_field") or "").strip()
        return (value,) if value else ()

    if kind == "calculate_field":
        result = list(expression_field_names(values.get("expression")))
        if not bool(values.get("create_field", True)):
            field = str(values.get("field_name") or "").strip()
            if field and field not in result:
                result.insert(0, field)
        return tuple(result)

    if kind in {"keep_fields", "remove_duplicates", "dissolve"}:
        return _list_value(values.get("fields"))

    if kind == "change_field_type":
        value = str(values.get("field") or values.get("source_field") or "").strip()
        return (value,) if value else ()

    if kind == "sort":
        return _structured_input_fields(values.get("sort_fields"), ("field",))

    if kind == "aggregate":
        fields = list(_list_value(values.get("group_fields")))
        for field in _structured_input_fields(values.get("aggregations"), ("field",)):
            if field not in fields:
                fields.append(field)
        return tuple(fields)

    return ()


def operation_field_summary(operation_kind, parameters):
    """Return short field-only labels for an operation block."""

    kind = str(operation_kind or "")
    values = dict(parameters or {})
    if kind == "join":
        left = str(values.get("left_field") or "").strip()
        right = str(values.get("right_field") or "").strip()
        return (f"{left} ↔ {right}",) if left or right else ()
    if kind == "compare_changes":
        previous = str(values.get("previous_key_field") or "").strip()
        current = str(values.get("current_key_field") or "").strip()
        return (f"{previous} ↔ {current}",) if previous or current else ()
    if kind == "field_mapping":
        result = []
        for row in values.get("mappings") or ():
            if not isinstance(row, dict):
                continue
            source = str(row.get("source_field") or "").strip()
            target = str(row.get("output_field") or row.get("target_name") or "").strip()
            if source or target:
                result.append(f"{source} → {target}")
        return tuple(result)
    if kind == "rename_field":
        source = str(values.get("source_field") or "").strip()
        target = str(values.get("target_name") or "").strip()
        return (f"{source} → {target}",) if source or target else ()

    fields = []
    for port in (
        "input",
        "left",
        "right",
        "previous",
        "current",
        "target",
        "join",
        "overlay",
        "predicate",
    ):
        for field in operation_input_fields(kind, values, port):
            if field not in fields:
                fields.append(field)
    return tuple(fields[:4])
