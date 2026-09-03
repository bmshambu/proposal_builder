"""Orchestration for the three stages: Construct -> Map -> Reconstruct.

Keeps the web layer thin: main.py calls these functions.
"""
import glob
import json
import os
import subprocess
import sys
import time

from . import config


# ---------------------------------------------------------------- helpers
def _run(cmd, cwd=None, env=None, timeout=1800):
    """Run a subprocess, capture output. Returns (ok, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, cwd=cwd or str(config.ROOT), env=env,
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, p.stdout, p.stderr
    except Exception as e:
        return False, "", str(e)


def list_payloads():
    return sorted(glob.glob(str(config.PAYLOADS_DIR / "*.json")))


def list_decks():
    return sorted(glob.glob(str(config.DECKS_DIR / "*.pptx")))


def list_outputs():
    return sorted(glob.glob(str(config.OUTPUT_DIR / "*.pptx")))


def save_payload(name, payload):
    """Write payload json to data/payloads/<name>.json. Returns path."""
    safe = "".join(c for c in name if c.isalnum() or c in ("-", "_")).strip("_") or "payload"
    path = config.PAYLOADS_DIR / (safe + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return str(path)


def read_payload(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- Construct
def _pptx_snapshot(dirs):
    """Map of .pptx path -> mtime, scanned recursively across several dirs.
    Skips virtualenvs and the (large, static) master so they never show up as
    freshly-generated candidates."""
    snap = {}
    master = os.path.abspath(config.MASTER_PPTX)
    patterns = [os.path.join(str(config.ROOT), "*.pptx")]  # repo root, flat
    for d in dirs:
        patterns.append(os.path.join(str(d), "**", "*.pptx"))  # recursive
    for pat in patterns:
        for f in glob.glob(pat, recursive=True):
            af = os.path.abspath(f)
            if af == master or af in snap:
                continue
            if os.sep + ".venv" + os.sep in af or os.sep + "venv" + os.sep in af:
                continue
            try:
                snap[af] = os.path.getmtime(f)
            except OSError:
                pass
    return snap


def construct(payload_path, email=None):
    """Call the user's Templafy generation script for one payload.

    The script's basic usage doesn't take --output-dir, so it may write the .pptx
    to the working directory rather than DECKS_DIR. We therefore watch several
    locations for a new/updated .pptx and move it into DECKS_DIR, named to match
    the payload stem so Map can pair them automatically. Full stdout/stderr is
    saved to config.LAST_LOG so failures are visible in the UI.
    """
    if not config.GENERATE_SCRIPT.exists():
        log = ("Generation script not found at %s. Drop your "
               "generate_public_audit_template_2026.py into scripts/."
               % config.GENERATE_SCRIPT)
        config.LAST_LOG.write_text(log, encoding="utf-8")
        return {"ok": False, "log": log, "deck": None}

    stem = os.path.splitext(os.path.basename(payload_path))[0]
    watch_dirs = [config.SCRIPTS_DIR, config.DECKS_DIR, config.OUTPUT_DIR]
    before = _pptx_snapshot(watch_dirs)
    start = time.time()

    cmd = config.generate_command(sys.executable, payload_path,
                                  email or config.default_email())
    ok, out, err = _run(cmd, cwd=str(config.GENERATE_CWD),
                        env=config.subprocess_env())
    log = "$ (cwd=%s) %s\n\n[exit ok: %s]\n\n[stdout]\n%s\n[stderr]\n%s" % (
        config.GENERATE_CWD, " ".join(cmd), ok, out or "(none)", err or "(none)")
    config.LAST_LOG.write_text(log, encoding="utf-8")

    # find a .pptx that is new or was modified during the run, anywhere we watch
    after = _pptx_snapshot(watch_dirs)
    candidates = [f for f, m in after.items()
                  if m >= start - 1 and (f not in before or m > before.get(f, 0))]
    new_deck = None
    if candidates:
        newest = max(candidates, key=lambda f: after[f])
        target = os.path.abspath(config.DECKS_DIR / (stem + ".pptx"))
        if newest != target:
            try:
                os.replace(newest, target)
            except OSError:
                target = newest
        new_deck = target
    return {"ok": ok and new_deck is not None, "log": log, "deck": new_deck}


def construct_all(email=None, only_missing=True):
    """Generate a deck for every saved payload. Skips payloads that already have
    a matching deck when only_missing is True (avoids re-spending Templafy calls).
    Returns a summary dict."""
    existing = {os.path.splitext(os.path.basename(d))[0] for d in list_decks()}
    out = {"generated": [], "skipped": [], "failed": []}
    for p in list_payloads():
        stem = os.path.splitext(os.path.basename(p))[0]
        if only_missing and stem in existing:
            out["skipped"].append(stem)
            continue
        res = construct(p, email=email)
        (out["generated"] if res["ok"] else out["failed"]).append(stem)
    return out


# ---------------------------------------------------------------- Map
def run_map():
    """Run map_decks.py over all payloads + decks; return the report text."""
    if not config.MASTER_PPTX.exists():
        return {"ok": False, "log": "Master template missing at %s. Put your "
                "265-slide master there (or set MASTER_PPTX)." % config.MASTER_PPTX}
    cmd = [sys.executable, str(config.ROOT / "map_decks.py"),
           "--master", str(config.MASTER_PPTX),
           "--decks", str(config.DECKS_DIR),
           "--payloads", str(config.PAYLOADS_DIR),
           "--out", str(config.MAPPING_DIR)]
    ok, out, err = _run(cmd)
    report = ""
    if config.MAPPING_REPORT.exists():
        report = open(config.MAPPING_REPORT, encoding="utf-8").read()
    return {"ok": ok, "log": out + "\n" + err, "report": report}


def load_spec():
    if config.MAPPING_SPEC.exists():
        return json.load(open(config.MAPPING_SPEC, encoding="utf-8"))
    return None


def spec_summary():
    """Quick counts for the UI (always/conditional/unclear/never)."""
    spec = load_spec()
    if not spec:
        return None
    counts = {"always": 0, "conditional": 0, "unclear": 0, "never_used": 0}
    for r in spec.get("selection_rules", {}).values():
        counts[r.get("rule", "unclear")] = counts.get(r.get("rule", "unclear"), 0) + 1
    return {"master_slides": spec.get("master", {}).get("slides"),
            "payloads": spec.get("payload_count"), "counts": counts}


# ---------------------------------------------------------------- Harvest library
def _library_pairs():
    """(baseline_pptx, baseline_payload_path, [(deck, payload)...]) for harvesting."""
    stem = config.BASELINE_STEM
    baseline_pptx = config.DECKS_DIR / (stem + ".pptx")
    baseline_payload = config.PAYLOADS_DIR / (stem + ".json")
    pairs = []
    for deck in list_decks():
        s = os.path.splitext(os.path.basename(deck))[0]
        if s == stem:
            continue
        pj = config.PAYLOADS_DIR / (s + ".json")
        if pj.exists():
            pairs.append((deck, str(pj)))
    return baseline_pptx, baseline_payload, pairs


def build_library():
    """Harvest the per-asset slide-block library from baseline + OFAT decks."""
    import harvest
    baseline_pptx, baseline_payload, pairs = _library_pairs()
    if not os.path.exists(baseline_pptx):
        return {"ok": False, "log": "Baseline deck missing: generate %s first."
                % (config.BASELINE_STEM + ".pptx")}
    if not os.path.exists(baseline_payload):
        return {"ok": False, "log": "Baseline payload missing: %s.json" % config.BASELINE_STEM}
    if not pairs:
        return {"ok": False, "log": "No other decks to harvest — generate the OFAT decks."}
    try:
        m = harvest.build_library(str(baseline_pptx), read_payload(baseline_payload),
                                  pairs, config.active_asset_dir())
        fields = sorted({d["field"] for d in m["deltas"]})
        return {"ok": True, "asset": config.active_asset_key(),
                "log": "Harvested %d deltas across %d fields into asset '%s'."
                       % (len(m["deltas"]), len(fields), config.active_asset_key())}
    except Exception as e:
        return {"ok": False, "log": "Harvest failed: %s" % e}


def library_status():
    import harvest
    m = harvest.load_manifest(config.active_asset_dir())
    if not m:
        return None
    return {"asset": config.active_asset_key(),
            "baseline_slides": m.get("baseline_slides"),
            "deltas": len(m.get("deltas", [])),
            "fields": sorted({d["field"] for d in m.get("deltas", [])})}


# ---------------------------------------------------------------- Reconstruct
def reconstruct(payload_path):
    """Rebuild a deck for a payload from the harvested library (no Templafy)."""
    import harvest
    if not harvest.load_manifest(config.active_asset_dir()):
        return {"ok": False, "log": "No harvested library yet — click 'Build library' first."}
    payload = read_payload(payload_path)
    stem = os.path.splitext(os.path.basename(payload_path))[0]
    out_path = str(config.OUTPUT_DIR / (stem + "_rebuilt.pptx"))
    try:
        res = harvest.assemble(config.active_asset_dir(), payload, out_path, verbose=False)
        applied = ", ".join("%s=%s" % (f, v) for f, v in res["deltas_applied"]) or "(baseline only)"
        log = ("Assembled %d slides = %d baseline kept + %d harvested, %d dropped.\n"
               "Deltas applied: %s\nToken replacements: %d"
               % (res["total_slides"], res["kept"], res["added"], res["dropped"],
                  applied, res["tokens"]))
        return {"ok": True, "log": log, "out": out_path, "total": res["total_slides"]}
    except Exception as e:
        return {"ok": False, "log": "Reconstruct failed: %s" % e}


# ---------------------------------------------------------------- Diff
def suggest_diff_pairs():
    """Pair each rebuilt deck (output/<stem>_rebuilt.pptx) with its original
    (decks/<stem>.pptx) when both exist."""
    decks = {os.path.splitext(os.path.basename(d))[0]: d for d in list_decks()}
    pairs = []
    for o in list_outputs():
        base = os.path.basename(o)
        stem = os.path.splitext(base)[0]
        if stem.endswith("_rebuilt"):
            stem = stem[:-len("_rebuilt")]
        if stem in decks:
            pairs.append({"stem": stem,
                          "original": os.path.basename(decks[stem]),
                          "rebuilt": base})
    return pairs


def _resolve_deck(name):
    """Find a deck by basename in either decks/ or output/."""
    for d in (config.DECKS_DIR, config.OUTPUT_DIR):
        p = os.path.join(str(d), name)
        if os.path.exists(p):
            return p
    return None


def run_diff(original_name, rebuilt_name):
    import diff_decks as dd
    original = _resolve_deck(original_name)
    rebuilt = _resolve_deck(rebuilt_name)
    if not original or not rebuilt:
        return {"ok": False, "log": "Both files must exist in decks/ or output/."}
    try:
        return {"ok": True, "result": dd.diff(original, rebuilt)}
    except Exception as e:
        return {"ok": False, "log": "Diff failed: %s" % e}


def diff_pick_files():
    """All decks + rebuilt outputs, for either side of a comparison."""
    names = [os.path.basename(d) for d in list_decks()] + \
            [os.path.basename(o) for o in list_outputs()]
    return sorted(set(names))
