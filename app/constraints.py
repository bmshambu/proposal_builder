"""
Sync the form's enum options from the generation script's `--list-constraints`,
so the dropdowns always match exactly what Templafy will accept.

Flow:
  sync()   -> runs `python scripts/generate_...py --list-constraints`, saves the
              raw output to data/constraints_raw.txt, parses it, and writes the
              normalized result to data/constraints.json.
  load()   -> returns the parsed dict (or None).
  effective_options() -> merges parsed values over the built-in defaults in
              schema.py; the form and /api/subsectors use this.

The parser is defensive: it tries JSON first, then a text/`Literal[...]` fallback.
If it cannot parse anything, defaults are kept and the raw output is left for you
to inspect (and adjust the parser or schema.py accordingly).
"""
import json
import re
import subprocess
import sys

from . import config, schema

RAW = config.DATA / "constraints_raw.txt"
PARSED = config.DATA / "constraints.json"

# normalized name -> exact Templafy payload key it corresponds to
_KEY_HINTS = {
    "audit_tax": "audit_only_or_is_tax",
    "new_client": "new_audit_client",
    "sectors": "industry_sector_is_this_proposal",
    "subsector": "select_industry_sub_sector",
    "cities": "appropriate_city",
}


# ---------------------------------------------------------------- run + cache
def sync():
    if not config.GENERATE_SCRIPT.exists():
        return {"ok": False, "msg": "Generation script not found in scripts/."}
    cmd = [sys.executable, str(config.GENERATE_SCRIPT), "--list-constraints"]
    try:
        p = subprocess.run(cmd, cwd=str(config.ROOT), capture_output=True,
                           text=True, timeout=120)
    except Exception as e:
        return {"ok": False, "msg": "Failed to run script: %s" % e}

    raw = (p.stdout or "")
    if p.stderr:
        raw += "\n\n[stderr]\n" + p.stderr
    RAW.write_text(raw, encoding="utf-8")

    parsed = parse_constraints(p.stdout or "")
    if parsed:
        PARSED.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
        fields = [k for k in parsed if k != "subsectors_by_sector"]
        n_sub = len(parsed.get("subsectors_by_sector", {}))
        return {"ok": True, "parsed": parsed,
                "msg": "Synced from script: %s%s."
                       % (", ".join(fields) or "no flat enums",
                          (" + %d sector→sub-sector maps" % n_sub) if n_sub else "")}
    return {"ok": False,
            "msg": "Script ran but output couldn't be parsed. Raw output saved to "
                   "data/constraints_raw.txt — using built-in defaults for now."}


def load():
    if PARSED.exists():
        try:
            return json.load(open(PARSED, encoding="utf-8"))
        except Exception:
            return None
    return None


# ---------------------------------------------------------------- parsing
def parse_constraints(text):
    """Return normalized {audit_tax, new_client, sectors, subsector, cities,
    subsectors_by_sector} — only the keys it could find."""
    text = text.strip()
    result = {}

    # 1) JSON path -------------------------------------------------------------
    data = None
    try:
        data = json.loads(text)
    except Exception:
        # maybe there's a JSON object embedded in surrounding text
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
    if isinstance(data, (dict, list)):
        lists = _collect_lists(data)          # [(key, [str,...])]
        for norm, hint in _KEY_HINTS.items():
            for key, vals in lists:
                if hint in key.lower():
                    result[norm] = vals
                    break
        sub_map = _collect_sector_map(data)
        if sub_map:
            result["subsectors_by_sector"] = sub_map
        if result:
            return result

    # 2) Text / Literal[...] fallback -----------------------------------------
    #    matches:  Some_Field_Name ... Literal[ "a", "b", ... ]
    for m in re.finditer(r'([A-Za-z0-9_ ]+?)\s*:?\s*(?:Optional\[)?Literal\[(.*?)\]',
                         text, re.DOTALL):
        field = m.group(1).strip().split()[-1] if m.group(1).strip() else ""
        vals = re.findall(r'"([^"]+)"', m.group(2))
        if not vals:
            continue
        for norm, hint in _KEY_HINTS.items():
            if hint in field.lower():
                result[norm] = vals
    # sector -> (sub-sectors) tuples, e.g.  "Technology": ("Hardware", "Software")
    sub_map = {}
    for m in re.finditer(r'"([^"]+)"\s*:\s*\(([^)]*)\)', text):
        sector = m.group(1)
        subs = re.findall(r'"([^"]+)"', m.group(2))
        if subs and sector in (result.get("sectors") or schema.SECTORS):
            sub_map[sector] = subs
    if sub_map:
        result["subsectors_by_sector"] = sub_map
    return result


def _collect_lists(obj, key=""):
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_collect_lists(v, str(k)))
    elif isinstance(obj, list):
        if obj and all(isinstance(x, str) for x in obj):
            out.append((key, obj))
        else:
            for x in obj:
                out.extend(_collect_lists(x, key))
    return out


def _collect_sector_map(obj):
    """Find a dict mapping known sector names -> list/tuple of sub-sector strings."""
    found = {}

    def walk(o):
        if isinstance(o, dict):
            hits = {k: v for k, v in o.items()
                    if isinstance(v, list) and v and all(isinstance(x, str) for x in v)}
            # heuristic: at least a couple of keys look like sectors
            if sum(1 for k in hits if k in schema.SECTORS) >= 2:
                for k, v in hits.items():
                    found[k] = v
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(obj)
    return found


# ---------------------------------------------------------------- effective
def effective_options():
    loaded = load() or {}

    def g(name, default):
        v = loaded.get(name)
        return v if isinstance(v, list) and v else default

    subs = loaded.get("subsectors_by_sector")
    if not isinstance(subs, dict) or not subs:
        subs = schema.SUBSECTORS_BY_SECTOR
    return {
        "audit_tax": g("audit_tax", schema.AUDIT_TAX_OPTIONS),
        "new_client": g("new_client", schema.NEW_CLIENT_OPTIONS),
        "sectors": g("sectors", schema.SECTORS),
        "subsectors_by_sector": subs,
        "cities": g("cities", schema.CITIES),
        "source": "script" if loaded else "defaults",
    }
