# Proposal Builder POC — replacing Templafy, headless & deterministic

A small FastAPI app that walks the four stages of building branded proposal
decks **without** Templafy, using Templafy's own mechanism (superset master →
delete unselected slides → inject tokens). The source deck is deterministic
(no AI component), so rebuilt decks can be diffed against Templafy output to
validate — bit-for-bit reproducibility is the goal.

```
Construct  ──▶   Map    ──▶  Reconstruct  ──▶    Diff
 build a         learn        rebuild a deck       validate:
 payload +       which        for a payload        original (Templafy)
 generate a      fields keep  from the rules       vs rebuild —
 Templafy deck   which slides only — no Templafy   same slides? same text?
 (uses Templafy) (+ tokens)   (Templafy-free)      (proves the POC)
```

## 1. Put your files in place

| File | Where |
|---|---|
| 265-slide raw master template | `data/master/master.pptx` |
| Your Templafy generation script | `scripts/generate_public_audit_template_2026.py` |
| Templafy credentials | `.env` at repo root (`TEMPLAFY_BASE_URL`, `TEMPLAFY_TOKEN`) |

(Everything under `data/`, the script, and `.env` are gitignored — nothing
confidential is committed.)

## 2. Install + run

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1        # PowerShell  (bash: source .venv/Scripts/activate)
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000

## 3. Test walkthrough (end to end)

**Pre-flight**
1. Drop your `master.pptx` and generation script in the two locations above; fill `.env`.
2. Open **Construct**. The setup check on the **Dashboard** should show both files ✓.
3. Click **Sync from --list-constraints** — the "Enum options" pill turns green
   (*synced from your script*), so every dropdown now serves exactly the values
   Templafy accepts. (If it can't parse, it keeps defaults and saves the raw
   output to `data/constraints_raw.txt`.)

**Stage 1 — Construct (uses Templafy)**
4. 16 OFAT payloads are already in `data/payloads/` (`00_baseline` … `15_city_chicago`),
   each differing from the baseline by one field. They appear in the Payloads list.
   *(To add more: fill the form → **Save payload**. To make one now: form → **Save & generate deck**.)*
5. Click **Generate all** (batch bar). With *only missing* checked it generates a
   deck per payload into `data/decks/`, auto-named to match. Wait for the summary
   flash. This is the only stage that spends Templafy calls.

**Stage 2 — Map (learn the rules)**
6. Open **Map** → **Run mapping**. Read `mapping_report.md`:
   - **Match quality** — trust *creationId* matches.
   - **Selection rules** — "always kept" = skeleton; "conditional (high confidence)"
     = a real rule (each boolean toggle should resolve to *kept when X=true*);
     "unclear" = add more payloads.
   - Writes `data/mapping/mapping_spec.json`.

**Stage 3 — Reconstruct (no Templafy)**
7. Open **Reconstruct** → pick a payload (e.g. `01_transition_lab`) → **Reconstruct**.
   Produces `data/output/<name>_rebuilt.pptx` by deleting unselected slides from
   the master. Branding is preserved automatically.

**Stage 4 — Diff (prove it matches)**
8. Open **Diff** → pick the original (`decks/01_transition_lab.pptx`) vs the rebuild
   (`output/01_transition_lab_rebuilt.pptx`) → **Compare**. Read the verdict:
   - **MATCH** — same slides, same text. ✅
   - **SELECTION OK, TEXT DIFFERS** — right slides, some text differs (add those
     tokens to `data/token_map.json`, re-Reconstruct).
   - **SELECTION DIFFERS** — a slide kept/dropped wrongly (refine the rule: add an
     OFAT payload and re-Map).

Iterate 6→8 until you get **MATCH** across your payloads. That's a validated POC.

## Stage cheat-sheet

| Stage | Uses Templafy? | Output |
|---|---|---|
| Construct | **yes** (builds training data) | `data/decks/*.pptx` |
| Map | no | `data/mapping/mapping_spec.json` + report |
| Reconstruct | **no** (the replacement) | `data/output/*_rebuilt.pptx` |
| Diff | no | on-screen validation verdict |

## Engine modules (pure standard library, also run standalone)

| Module | Purpose |
|---|---|
| `inspect_pptx.py` | forensic OOXML inspector (provenance discovery) |
| `pptx_forensics.py` | shared read-only parsing library |
| `map_decks.py` | learn selection + token rules from (payload → deck) pairs |
| `reconstruct.py` | headless assembler: master − unselected slides + tokens |
| `diff_decks.py` | compare an original deck against its rebuild |

Standalone example:
```bash
python map_decks.py --master data/master/master.pptx --decks data/decks --payloads data/payloads --out data/mapping
python reconstruct.py --master data/master/master.pptx --spec data/mapping/mapping_spec.json --payload data/payloads/01_transition_lab.json --out data/output/01_rebuilt.pptx
python diff_decks.py --original data/decks/01_transition_lab.pptx --rebuilt data/output/01_rebuilt.pptx
```

## Notes / current limits

- **Enum values** — synced from your script's `--list-constraints` via the button
  on Construct (cached to `data/constraints.json`). Until you sync, the built-in
  lists in `app/schema.py` are used; the pill shows which is active.
- **Token injection** (v1) uses an optional `data/token_map.json`
  (`{ "text-in-master": "PayloadFieldName" }`) for dynamic text like client name
  and date. Slide *selection* is branding-perfect on its own; fill in the token
  map once the Diff stage shows you which text still differs.
- **Batch generate** runs sequentially and the page waits until done (fine for
  ~10–20 payloads). For larger runs we'd move it to a background task.
- If your generation script's CLI differs (flags / where it writes the `.pptx`),
  adjust `generate_command()` in `app/config.py` — one place.
- The 265→delete engine keeps all masters/layouts/theme/media; it does **not**
  yet re-stamp the Microsoft sensitivity label (`MSIP_Label`) — add that before
  decks leave the building.
