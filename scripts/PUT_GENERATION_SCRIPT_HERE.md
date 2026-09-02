# Put your Templafy generation script here

Drop **`generate_public_audit_template_2026.py`** into this folder.

The Construct stage calls it as a subprocess, roughly:

    python scripts/generate_public_audit_template_2026.py \
        --email you@kpmg.com \
        --data <payload.json> \
        --output-dir data/decks

If your script's CLI differs (flag names, or where it writes the .pptx), adjust
`generate_command()` in `app/config.py` — that's the single place the app builds
the command.

The script reads `TEMPLAFY_BASE_URL` and `TEMPLAFY_TOKEN` from `.env` at the repo
root. This file is gitignored — it never gets committed.
