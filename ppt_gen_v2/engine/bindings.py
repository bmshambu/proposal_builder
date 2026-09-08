"""Turning declared placeholder bindings into concrete values.

A binding says *where a value comes from*, never how to guess it:

    "{{ClientName}}":  {"from": "field",   "field": "FullClientName"}
    "{{DueDate}}":     {"from": "field",   "field": "DueDate", "format": "long_comma"}
    "{{LeadPartner}}": {"from": "data",    "source": "profile", "key": "lead_partner"}
    "{{FirmName}}":    {"from": "literal", "value": "Demo LLP"}

`data` bindings are the ~8% v1 could not close without external systems. They
resolve against whatever source dicts the caller passes in; anything unwired is
reported by name, so the remaining work is a wiring list rather than research.
"""
import re

from .rules import flatten

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def date_formats(value):
    """The human formats a YYYYMMDD (or ISO) date can be rendered as."""
    s = re.sub(r"\D", "", str(value))
    if len(s) < 8:
        return {}
    y, m, d = int(s[:4]), int(s[4:6]), int(s[6:8])
    if not (1 <= m <= 12 and 1 <= d <= 31):
        return {}
    mon, ab = MONTHS[m - 1], MONTHS[m - 1][:3]
    return {
        "long_comma": "%s %d, %d" % (mon, d, y),       # November 30, 2026
        "day_month":  "%d %s %d" % (d, mon, y),        # 30 November 2026
        "abbr_comma": "%s %d, %d" % (ab, d, y),        # Nov 30, 2026
        "day_abbr":   "%d %s %d" % (d, ab, y),         # 30 Nov 2026
        "us_slash":   "%02d/%02d/%d" % (m, d, y),      # 11/30/2026
        "eu_slash":   "%02d/%02d/%d" % (d, m, y),      # 30/11/2026
        "iso":        "%d-%02d-%02d" % (y, m, d),      # 2026-11-30
        "raw":        s,
    }


def _format(value, fmt):
    if not fmt or value is None:
        return value
    if fmt in ("upper", "lower", "title"):
        return getattr(str(value), fmt)()
    variants = date_formats(value)
    if fmt in variants:
        return variants[fmt]
    raise BindingError("unknown format %r" % fmt)


class BindingError(Exception):
    pass


def name_of(key):
    """'{{ClientName}}' or 'ClientName' -> 'ClientName'."""
    return key.strip().strip("{}").strip()


def resolve(spec, answers, data_sources=None):
    """-> (values by placeholder name, [unresolved descriptions])."""
    flat = flatten(answers)
    sources = data_sources or {}
    values, unresolved = {}, []

    for key, binding in (spec or {}).items():
        name = name_of(key)
        if not isinstance(binding, dict):                 # shorthand: a literal
            values[name] = binding
            continue
        kind = binding.get("from", "field")

        if kind == "literal":
            values[name] = binding.get("value", "")
            continue

        if kind == "field":
            field = binding.get("field", name)
            if field in flat and flat[field] not in (None, ""):
                values[name] = _format(flat[field], binding.get("format"))
            elif "default" in binding:
                values[name] = binding["default"]
            else:
                unresolved.append("{{%s}} <- answers.%s (not supplied)" % (name, field))
            continue

        if kind == "data":
            src, dkey = binding.get("source"), binding.get("key", name)
            table = sources.get(src)
            if table is not None and dkey in table:
                values[name] = _format(table[dkey], binding.get("format"))
            elif "default" in binding:
                values[name] = binding["default"]
            else:
                unresolved.append("{{%s}} <- data source %r key %r (not wired)"
                                  % (name, src, dkey))
            continue

        raise BindingError("unknown binding kind %r for %s" % (kind, key))

    return values, unresolved
