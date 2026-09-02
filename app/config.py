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
LAST_LOG = DATA / "last_generation.log"      # stdout/stderr of the most recent run

# --- Templafy generation script (yours, dropped into scripts/) ---------------
# The Construct stage shells out to this. Adjust GENERATE_SCRIPT / GENERATE_ARGS
# to match your script's actual CLI if it differs from the assumed one.
GENERATE_SCRIPT = pathlib.Path(
    os.environ.get("GENERATE_SCRIPT",
                   SCRIPTS_DIR / "generate_public_audit_template_2026.py"))

# The script is run from GENERATE_CWD (the scripts/ folder, where you placed
# .env) so its relative paths / dotenv behave exactly like running it standalone.
GENERATE_CWD = SCRIPTS_DIR

# Command for one generation run. Matches the script's documented basic usage
# (--email --data). We deliberately do NOT pass --output-dir: the script writes
# the .pptx to its working dir, and services.construct() auto-detects and moves
# it into data/decks/. Add flags here only if your script needs them.
def generate_command(python_exe, payload_path, email):
    return [
        python_exe, str(GENERATE_SCRIPT),
        "--email", email,
        "--data", str(payload_path),
    ]


def _parse_env_file(path):
    """Minimal KEY=VALUE .env parser (no dependency). Returns a dict."""
    out = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k:
                out[k] = v
    except OSError:
        pass
    return out


def subprocess_env():
    """os.environ merged with any .env found at the repo root and in scripts/,
    so TEMPLAFY_BASE_URL / TEMPLAFY_TOKEN reach the script regardless of cwd or
    which folder the .env lives in (scripts/.env wins if both exist)."""
    env = dict(os.environ)
    for p in (ROOT / ".env", SCRIPTS_DIR / ".env"):
        env.update(_parse_env_file(p))
    return env


def default_email():
    """The Templafy user email used for doc-gen. Set PROPOSAL_EMAIL in .env
    (repo root or scripts/) to your real Templafy login email. The UI email
    field overrides this per run when filled."""
    return subprocess_env().get("PROPOSAL_EMAIL") or "you@kpmg.com"
