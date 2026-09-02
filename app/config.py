"""Central paths + settings for the proposal-builder app."""
import os
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
# make the engine modules (pptx_forensics, map_decks, reconstruct) importable
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# --- directories -------------------------------------------------------------
DATA = ROOT / "data"
MASTER_DIR = DATA / "master"
PAYLOADS_DIR = DATA / "payloads"
DECKS_DIR = DATA / "decks"
MAPPING_DIR = DATA / "mapping"
OUTPUT_DIR = DATA / "output"
SCRIPTS_DIR = ROOT / "scripts"

for d in (PAYLOADS_DIR, DECKS_DIR, MAPPING_DIR, OUTPUT_DIR, MASTER_DIR, SCRIPTS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# --- key files (override via env if you name them differently) ---------------
MASTER_PPTX = pathlib.Path(os.environ.get("MASTER_PPTX", MASTER_DIR / "master.pptx"))
MAPPING_SPEC = MAPPING_DIR / "mapping_spec.json"
MAPPING_REPORT = MAPPING_DIR / "mapping_report.md"
TOKEN_MAP = DATA / "token_map.json"          # optional {find_text: payload_field}

# --- Templafy generation script (yours, dropped into scripts/) ---------------
# The Construct stage shells out to this. Adjust GENERATE_SCRIPT / GENERATE_ARGS
# to match your script's actual CLI if it differs from the assumed one.
GENERATE_SCRIPT = pathlib.Path(
    os.environ.get("GENERATE_SCRIPT",
                   SCRIPTS_DIR / "generate_public_audit_template_2026.py"))
DEFAULT_EMAIL = os.environ.get("PROPOSAL_EMAIL", "you@kpmg.com")

# Argument template for one generation run. {py} {script} {email} {data} {outdir}
# are substituted. Everything is passed as a list (no shell) for safety.
def generate_command(python_exe, payload_path, outdir, email):
    return [
        python_exe, str(GENERATE_SCRIPT),
        "--email", email,
        "--data", str(payload_path),
        "--output-dir", str(outdir),
    ]
