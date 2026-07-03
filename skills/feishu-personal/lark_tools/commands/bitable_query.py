"""
commands/bitable_query.py — client-side bitable query engine.

Filter / sort / group parsing + evaluation. Imported (re-exported) from
commands/bitable.py so existing call sites keep working transparently.

Tested by tests/test_bitable_query.py (105 unit tests).
"""

import json


BITABLE_FIELD_TYPES = {
    1: 'Text', 2: 'Number', 3: 'SingleSelect', 4: 'MultiSelect', 5: 'DateTime',
    7: 'Checkbox', 11: 'User', 13: 'Phone', 15: 'URL', 17: 'Attachment',
    18: 'SingleLink', 19: 'Lookup', 20: 'Formula', 21: 'DuplexLink',
    22: 'Location', 23: 'GroupChat', 24: 'AutoNumber',
    1001: 'CreatedTime', 1002: 'ModifiedTime', 1003: 'CreatedUser', 1004: 'ModifiedUser',
}

VIEW_TYPE_NAMES = {
    1: 'Grid', 2: 'Kanban', 3: 'Gallery', 4: 'Gantt',
    5: 'Form', 7: 'Hierarchy', 8: 'QueryForm', 11: 'Calendar',
    100: 'WidgetView',
}


# Operator allow-set per field type, lifted from the bitable JS bundle's
# `uv = {<FieldType>: <ops>}` map (PLAN_bitable_query.md §1.4). Keep this in
# sync with eval_condition — adding an op here without an evaluator branch
# silently returns False.
FIELD_TYPE_OPERATORS = {
    1:    {'is', 'isNot', 'contains', 'doesNotContain', 'isEmpty', 'isNotEmpty'},  # Text
    2:    {'is', 'isNot', 'isGreater', 'isGreaterEqual', 'isLess', 'isLessEqual', 'isEmpty', 'isNotEmpty'},  # Number
    3:    {'is', 'isNot', 'contains', 'doesNotContain', 'isEmpty', 'isNotEmpty'},  # SingleSelect
    4:    {'is', 'isNot', 'contains', 'doesNotContain', 'isEmpty', 'isNotEmpty'},  # MultiSelect
    5:    {'is', 'isGreater', 'isLess', 'isEmpty', 'isNotEmpty'},                  # DateTime
    7:    {'is'},                                                                   # Checkbox
    11:   {'is', 'isNot', 'contains', 'doesNotContain', 'isEmpty', 'isNotEmpty'},  # User
    13:   {'is', 'isNot', 'contains', 'doesNotContain', 'isEmpty', 'isNotEmpty'},  # Phone
    15:   {'is', 'isNot', 'contains', 'doesNotContain', 'isEmpty', 'isNotEmpty'},  # Url
    17:   {'isEmpty', 'isNotEmpty'},                                               # Attachment
    18:   {'is', 'isNot', 'contains', 'doesNotContain', 'isEmpty', 'isNotEmpty'},  # SingleLink
    19:   {'is', 'isNot', 'isGreater', 'isGreaterEqual', 'isLess', 'isLessEqual',  # Lookup (union)
           'contains', 'doesNotContain', 'isEmpty', 'isNotEmpty'},
    20:   {'is', 'isNot', 'isGreater', 'isGreaterEqual', 'isLess', 'isLessEqual',  # Formula (union)
           'contains', 'doesNotContain', 'isEmpty', 'isNotEmpty'},
    21:   {'is', 'isNot', 'contains', 'doesNotContain', 'isEmpty', 'isNotEmpty'},  # DuplexLink
    22:   {'is', 'isNot', 'contains', 'doesNotContain', 'isEmpty', 'isNotEmpty'},  # Location
    23:   {'is', 'isNot', 'contains', 'doesNotContain', 'isEmpty', 'isNotEmpty'},  # GroupChat
    1001: {'is', 'isGreater', 'isLess', 'isEmpty', 'isNotEmpty'},                  # CreatedTime
    1002: {'is', 'isGreater', 'isLess', 'isEmpty', 'isNotEmpty'},                  # ModifiedTime
    1003: {'is', 'isNot', 'contains', 'doesNotContain', 'isEmpty', 'isNotEmpty'},  # CreatedUser
    1004: {'is', 'isNot', 'contains', 'doesNotContain', 'isEmpty', 'isNotEmpty'},  # ModifiedUser
    1005: {'is', 'isNot', 'isGreater', 'isGreaterEqual', 'isLess', 'isLessEqual', 'isEmpty', 'isNotEmpty'},  # AutoNumber
}

# Canonical operator names — same identifiers as the bitable JS bundle's `uv`
# map. The CLI surface accepts these directly via `--filter <field> <op> [value]`.
OPERATOR_NAMES = frozenset({
    'is', 'isNot',
    'isGreater', 'isGreaterEqual', 'isLess', 'isLessEqual',
    'contains', 'doesNotContain',
    'isEmpty', 'isNotEmpty',
})


def _resolve_field(field_map: dict, ident: str) -> tuple:
    """Resolve a field identifier (fieldId or name) to (fieldId, field_info).
    Prefers exact fieldId match, then exact name match.
    Raises ValueError if not found.
    """
    if ident in field_map:
        return ident, field_map[ident]
    for fid, finfo in field_map.items():
        if finfo.get('name') == ident:
            return fid, finfo
    available = ', '.join(f'{f["name"]!r}' for f in field_map.values())
    raise ValueError(f"field {ident!r} not found; available: {available}")


def parse_view_config(view_def: dict, field_map: dict) -> dict:
    """Build a display-friendly view summary from a viewMap entry."""
    prop = view_def.get('property') or {}
    vtype = view_def.get('type')
    out = {
        'name': view_def.get('name'),
        'type': VIEW_TYPE_NAMES.get(vtype) or f'Unknown({vtype})',
    }

    raw_filter = prop.get('filterInfo')
    if raw_filter and (raw_filter.get('conditions') or []):
        conds = []
        for c in raw_filter['conditions']:
            fid = c.get('fieldId')
            finfo = field_map.get(fid) or {}
            value = c.get('value') or []
            ftype = c.get('fieldType') or finfo.get('type')
            display_value = _display_filter_value(value, ftype, finfo)
            conds.append({
                'field': finfo.get('name') or fid,
                'operator': c.get('operator'),
                'value': display_value,
            })
        out['filter'] = {'conjunction': raw_filter.get('conjunction') or 'and', 'conditions': conds}
    else:
        out['filter'] = None

    sorts = []
    for s in (prop.get('sortInfo') or []):
        finfo = field_map.get(s.get('fieldId')) or {}
        sorts.append({'field': finfo.get('name') or s.get('fieldId'), 'desc': bool(s.get('desc'))})
    out['sort'] = sorts

    groups = []
    for g in (prop.get('group') or []):
        finfo = field_map.get(g.get('fieldId')) or {}
        groups.append({'field': finfo.get('name') or g.get('fieldId'), 'desc': bool(g.get('desc'))})
    out['group'] = groups

    return out


def _display_filter_value(value, ftype, field_info):
    """Translate a filter condition's raw value list to a human-readable form."""
    if not isinstance(value, list):
        return value
    om = (field_info or {}).get('optionMap') or {}
    if ftype == 3:  # SingleSelect
        translated = [om.get(v, v) for v in value]
        return translated[0] if len(translated) == 1 else translated
    if ftype == 4:  # MultiSelect
        return [om.get(v, v) for v in value]
    if len(value) == 1:
        return value[0]
    return value


def find_view(view_map: dict, ident: str) -> tuple:
    """Resolve a view identifier (viewId or name) to (viewId, view_def).
    Exact match only; raises ValueError listing available views if not found.
    """
    if ident in view_map:
        return ident, view_map[ident]
    for vid, vdef in view_map.items():
        if vdef.get('name') == ident:
            return vid, vdef
    names = ', '.join(f'{v.get("name")!r}' for v in view_map.values())
    raise ValueError(f"view {ident!r} not found; available: {names}")


def _parse_bool(s: str) -> bool:
    v = s.strip().lower()
    if v in ('true', '1', 'yes', 'y'):
        return True
    if v in ('false', '0', 'no', 'n'):
        return False
    raise ValueError(f"expected boolean (true/false/1/0/yes/no), got {s!r}")


def _parse_datetime_to_ms(s: str) -> int:
    """Accept 'YYYY-MM-DD', ISO 8601 timestamp, or raw epoch (sec or ms).
    Returns unix milliseconds in UTC.
    """
    s = s.strip()
    if s.isdigit():
        # Heuristic: <13 digits assumed seconds; >=13 assumed ms.
        n = int(s)
        return n if len(s) >= 13 else n * 1000
    from datetime import datetime, timezone
    for fmt in ('%Y-%m-%d', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M:%SZ',
                '%Y-%m-%d %H:%M:%S'):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    raise ValueError(f"date {s!r} not recognized; use YYYY-MM-DD or epoch (s/ms)")


def _select_opt_ids(value_part: str, finfo: dict, multi: bool) -> list:
    """Translate user-typed opt_name(s) to opt_id(s). With `multi=True`, accept
    a comma-separated list of names.
    """
    name_to_id = {n: i for i, n in (finfo.get('optionMap') or {}).items()}
    if not name_to_id:
        raise ValueError(
            f"field {finfo.get('name')!r} has no options defined; cannot filter"
        )
    names = [s.strip() for s in value_part.split(',')] if multi else [value_part]
    opt_ids = []
    for name in names:
        opt_id = name_to_id.get(name)
        if opt_id is None:
            available = ', '.join(repr(n) for n in name_to_id)
            raise ValueError(
                f"option {name!r} not found in field {finfo.get('name')!r}; "
                f"available: {available}"
            )
        opt_ids.append(opt_id)
    return opt_ids


def parse_filter_arg(parts, field_map: dict) -> dict:
    """Parse a positional filter spec into a server-shape condition dict.

    `parts` is a list of 2 or 3 strings:
      [field, op]              — for isEmpty / isNotEmpty
      [field, op, value]       — for everything else

    `op` is one of OPERATOR_NAMES (canonical bundle name like 'isGreater').
    `field` is a fieldId or field name.

    Returns: {fieldId, fieldType, operator, value}. `value` is [] for
    isEmpty/isNotEmpty.

    Raises ValueError on:
      - bad arity (not 2 or 3)
      - unknown operator
      - unknown field
      - operator not allowed for field type (per FIELD_TYPE_OPERATORS)
      - value missing for value-requiring op (or present for isEmpty/isNotEmpty)
      - value type conversion failure (e.g., Number receiving 'abc')
    """
    if not isinstance(parts, (list, tuple)) or len(parts) not in (2, 3):
        raise ValueError(
            f"--filter expects <field> <op> [value]; got {parts!r}"
        )
    field_part = parts[0]
    op_name = parts[1]
    value_part = parts[2] if len(parts) == 3 else None

    if op_name not in OPERATOR_NAMES:
        raise ValueError(
            f"unknown operator {op_name!r}; allowed: "
            f"{', '.join(sorted(OPERATOR_NAMES))}"
        )

    fid, finfo = _resolve_field(field_map, field_part)
    ftype = finfo.get('type')

    # Operator allow-set check.
    allowed = FIELD_TYPE_OPERATORS.get(ftype)
    if allowed is None:
        type_name = BITABLE_FIELD_TYPES.get(ftype, f'type{ftype}')
        raise ValueError(
            f"filtering on {type_name} field is not supported (field "
            f"{finfo.get('name')!r})"
        )
    if op_name not in allowed:
        type_name = BITABLE_FIELD_TYPES.get(ftype, f'type{ftype}')
        raise ValueError(
            f"operator {op_name!r} not allowed for field {finfo.get('name')!r} "
            f"(type {type_name}); allowed: {', '.join(sorted(allowed))}"
        )

    # isEmpty / isNotEmpty: no value (and value must NOT have been supplied).
    if op_name in ('isEmpty', 'isNotEmpty'):
        if value_part is not None:
            raise ValueError(
                f"operator {op_name!r} takes no value; got {value_part!r}"
            )
        return {'fieldId': fid, 'fieldType': ftype, 'operator': op_name, 'value': []}

    if value_part is None:
        raise ValueError(f"operator {op_name!r} requires a value")

    # Value conversion per field type.
    if ftype in (1, 13, 15, 22, 18, 21):  # Text-like
        value = [value_part]
    elif ftype == 2:  # Number
        try:
            value = [float(value_part)]
        except ValueError:
            raise ValueError(
                f"field {finfo.get('name')!r} is Number; expected numeric "
                f"value, got {value_part!r}"
            )
    elif ftype == 3:  # SingleSelect — single value only
        value = _select_opt_ids(value_part, finfo, multi=False)
    elif ftype == 4:  # MultiSelect — comma-multi for contains/doesNotContain
        multi = op_name in ('contains', 'doesNotContain')
        value = _select_opt_ids(value_part, finfo, multi=multi)
    elif ftype == 7:  # Checkbox
        value = [_parse_bool(value_part)]
    elif ftype in (5, 1001, 1002):  # DateTime / CreatedTime / ModifiedTime
        value = [_parse_datetime_to_ms(value_part)]
    elif ftype == 1005:  # AutoNumber
        try:
            value = [float(value_part)]
        except ValueError:
            raise ValueError(
                f"field {finfo.get('name')!r} is AutoNumber; expected numeric "
                f"value, got {value_part!r}"
            )
    elif ftype in (11, 23, 1003, 1004):  # User / GroupChat / CreatedUser / ModifiedUser
        # Tier 1: accept raw user_id only — name→id reverse lookup is Tier 2.
        value = [value_part]
    elif ftype in (19, 20):  # Lookup / Formula — pass-through string; numeric coerced if possible
        try:
            value = [float(value_part)]
        except ValueError:
            value = [value_part]
    else:
        type_name = BITABLE_FIELD_TYPES.get(ftype, f'type{ftype}')
        raise ValueError(
            f"value parsing for {type_name} field not implemented "
            f"(field {finfo.get('name')!r})"
        )

    return {'fieldId': fid, 'fieldType': ftype, 'operator': op_name, 'value': value}


def parse_sort_arg(expr: str, field_map: dict) -> dict:
    """Parse `field` or `field:asc|desc` into a sort dict."""
    if ':' in expr:
        field_part, dir_part = expr.split(':', 1)
        field_part = field_part.strip()
        dir_part = dir_part.strip().lower()
        if dir_part not in ('asc', 'desc'):
            raise ValueError(f"sort direction must be 'asc' or 'desc', got {dir_part!r}")
        desc = (dir_part == 'desc')
    else:
        field_part = expr.strip()
        desc = False
    fid, _ = _resolve_field(field_map, field_part)
    return {'fieldId': fid, 'desc': desc}


def parse_group_arg(expr: str, field_map: dict) -> dict:
    fid, _ = _resolve_field(field_map, expr.strip())
    return {'fieldId': fid, 'desc': False}


def _is_null(v) -> bool:
    """Mirror of bundle's `i0()`: strict null. Empty string / 0 / False are NOT null."""
    return v is None


def _ci_text(s) -> str:
    """Lowercase + trim, None-safe. Mirror of bundle's `.toLowerCase().trim()`."""
    if not isinstance(s, str):
        return ''
    return s.strip().lower()


def _text_contains(haystack, needle) -> bool:
    """Bundle's l0(): case-insensitive substring."""
    if _is_null(haystack):
        return False
    return _ci_text(needle) in _ci_text(haystack)


def eval_condition(raw_record: dict, cond: dict, field_map: dict) -> bool:
    """Evaluate a single filter condition against a raw record dict.

    Branches mirror the JS bundle's per-type dispatch (see PLAN §1.4).
    Adding a new operator without a branch here will fall through the type
    branch and raise — DO NOT silently return False for unknown ops.
    """
    if not isinstance(raw_record, dict):
        return False
    fid = cond['fieldId']
    cell = raw_record.get(fid)
    raw_val = cell.get('value') if isinstance(cell, dict) else cell
    op = cond.get('operator', 'is')
    target = cond.get('value') or []
    ftype = field_map.get(fid, {}).get('type')

    # Universal emptiness — applies to every type.
    if op == 'isEmpty':
        return _is_null(raw_val)
    if op == 'isNotEmpty':
        return not _is_null(raw_val)

    # ── Text-like: Text(1) / Phone(13) / Url(15) / Location(22) / SingleLink(18) / DuplexLink(21) ──
    if ftype in (1, 13, 15, 22, 18, 21):
        a = target[0] if target else ''
        if op == 'is':             return not _is_null(raw_val) and _ci_text(raw_val) == _ci_text(a)
        if op == 'isNot':          return _is_null(raw_val) or _ci_text(raw_val) != _ci_text(a)
        if op == 'contains':       return _text_contains(raw_val, a)
        if op == 'doesNotContain': return _is_null(raw_val) or not _text_contains(raw_val, a)

    # ── Number(2) ──
    if ftype == 2:
        if _is_null(raw_val):
            # Bundle: isNot/doesNotContain on null returns True (vacuously "not equal").
            return op in ('isNot', 'doesNotContain')
        try:
            n = float(raw_val)
            a = float(target[0]) if target else 0.0
        except (ValueError, TypeError):
            return False
        if op == 'is':             return n == a
        if op == 'isNot':          return n != a
        if op == 'isGreater':      return n > a
        if op == 'isGreaterEqual': return n >= a
        if op == 'isLess':         return n < a
        if op == 'isLessEqual':    return n <= a

    # ── AutoNumber(1005): cell.value is [{sequence: "1", number: "1"}] ──
    if ftype == 1005:
        seq = None
        if isinstance(raw_val, list) and raw_val:
            try:
                seq = float(raw_val[0].get('sequence'))
            except (ValueError, TypeError, AttributeError):
                seq = None
        if seq is None:
            return op == 'isNot'
        try:
            a = float(target[0]) if target else 0.0
        except (ValueError, TypeError):
            return False
        if op == 'is':             return seq == a
        if op == 'isNot':          return seq != a
        if op == 'isGreater':      return seq > a
        if op == 'isGreaterEqual': return seq >= a
        if op == 'isLess':         return seq < a
        if op == 'isLessEqual':    return seq <= a

    # ── Checkbox(7): missing cell → false ──
    if ftype == 7:
        if op == 'is':
            a = target[0] if target else False
            return bool(raw_val) == bool(a)

    # ── SingleSelect(3): cell.value is opt_id (str), target is [opt_id, ...] ──
    if ftype == 3:
        if op == 'is':                  return not _is_null(raw_val) and raw_val in target
        if op == 'isNot':               return _is_null(raw_val) or raw_val not in target
        if op == 'contains':            return not _is_null(raw_val) and raw_val in target
        if op == 'doesNotContain':      return _is_null(raw_val) or raw_val not in target

    # ── MultiSelect(4): cell.value is [opt_id, ...], target is [opt_id, ...] ──
    if ftype == 4:
        rv = raw_val if isinstance(raw_val, list) else ([] if _is_null(raw_val) else [raw_val])
        if op == 'is':             return set(rv) == set(target)
        if op == 'isNot':          return set(rv) != set(target)
        if op == 'contains':       return any(v in rv for v in target)
        if op == 'doesNotContain': return all(v not in rv for v in target)

    # ── DateTime(5) / CreatedTime(1001) / ModifiedTime(1002): cell.value is unix_ms ──
    if ftype in (5, 1001, 1002):
        if _is_null(raw_val):
            return False
        try:
            n = int(raw_val)
            a = int(target[0]) if target else 0
        except (ValueError, TypeError):
            return False
        # 'is' = same UTC day. Tier 1 approximation; see SKILL.md caveat about
        # timezone drift across DST boundaries / non-UTC bases.
        if op == 'is':         return (n // 86_400_000) == (a // 86_400_000)
        if op == 'isGreater':  return n > a
        if op == 'isLess':     return n < a

    # ── User(11) / GroupChat(23) / CreatedUser(1003) / ModifiedUser(1004) ──
    # cell.value is typically a list of user_id; target is [user_id, ...].
    if ftype in (11, 23, 1003, 1004):
        if isinstance(raw_val, list):
            rv = raw_val
        elif _is_null(raw_val):
            rv = []
        else:
            rv = [raw_val]
        if op == 'is':             return set(rv) == set(target)
        if op == 'isNot':          return set(rv) != set(target)
        if op == 'contains':       return any(v in rv for v in target)
        if op == 'doesNotContain': return all(v not in rv for v in target)

    # ── Lookup(19) / Formula(20): pass-through best-effort ──
    if ftype in (19, 20):
        a = target[0] if target else ''
        if op == 'is':             return raw_val == a
        if op == 'isNot':          return raw_val != a
        if op == 'contains':       return _text_contains(raw_val, a)
        if op == 'doesNotContain': return _is_null(raw_val) or not _text_contains(raw_val, a)
        try:
            n = float(raw_val) if not _is_null(raw_val) else None
            an = float(a) if a not in ('', None) else 0.0
        except (ValueError, TypeError):
            return False
        if n is None:
            return False
        if op == 'isGreater':      return n > an
        if op == 'isGreaterEqual': return n >= an
        if op == 'isLess':         return n < an
        if op == 'isLessEqual':    return n <= an

    raise ValueError(
        f"operator {op!r} for field type {ftype} not implemented "
        f"(field: {(field_map.get(fid) or {}).get('name')})"
    )


def eval_filter(raw_record: dict, filter_info: dict, field_map: dict) -> bool:
    conditions = filter_info.get('conditions') or []
    if not conditions:
        return True
    conjunction = (filter_info.get('conjunction') or 'and').lower()
    if conjunction == 'or':
        return any(eval_condition(raw_record, c, field_map) for c in conditions)
    return all(eval_condition(raw_record, c, field_map) for c in conditions)


def apply_sort(items: list, sort_info: list, field_map: dict) -> list:
    """Stable multi-key sort over [(rid, raw_record), ...]. Nulls always last,
    regardless of asc/desc — partition then sort each side independently so
    `reverse=True` doesn't flip the null-handling.
    """
    def cell_value(rec, fid):
        cell = rec.get(fid) if isinstance(rec, dict) else None
        return cell.get('value') if isinstance(cell, dict) else cell

    def value_key(v):
        if isinstance(v, (int, float)):
            return (0, v)
        if isinstance(v, list):
            return (1, json.dumps(v, ensure_ascii=False, sort_keys=True))
        return (1, str(v))

    for s in reversed(sort_info):
        fid = s['fieldId']
        desc = bool(s.get('desc'))
        non_null, nulls = [], []
        for it in items:
            v = cell_value(it[1], fid)
            (nulls if v is None else non_null).append(it)
        non_null.sort(key=lambda it: value_key(cell_value(it[1], fid)), reverse=desc)
        items = non_null + nulls
    return items


def apply_group(rows: list, gfid: str, field_map: dict) -> list:
    """Bucket rows by display-side value; ordered by SingleSelect display order
    when applicable, otherwise by first-seen insertion order. (空) bucket last.
    """
    finfo = field_map.get(gfid) or {}
    fname = finfo.get('name') or gfid
    buckets = {}
    for row in rows:
        key = row.get(fname)
        if key is None or key == '':
            key = '(空)'
        else:
            key = str(key) if not isinstance(key, str) else key
        buckets.setdefault(key, []).append(row)

    option_order = list((finfo.get('optionMap') or {}).values())
    if option_order:
        ordered_keys = [k for k in option_order if k in buckets]
        ordered_keys += [k for k in buckets if k != '(空)' and k not in option_order]
    else:
        ordered_keys = [k for k in buckets if k != '(空)']
    if '(空)' in buckets:
        ordered_keys.append('(空)')

    return [{'key': k, 'count': len(buckets[k]), 'records': buckets[k]} for k in ordered_keys]
