# Replacing Templafy for Proposal Deck Automation — Findings & Phase‑0 Plan

**Purpose:** Share our research on how Templafy assembles branded proposal decks, and kick off a Phase‑0 investigation (with a ready‑to‑run Python inspector) to answer the one thing we don't yet know: **where does the content in the generated decks actually come from?**

**Status:** Discovery / POC scoping.
**Stack decision (locked):** Python, headless (no PowerPoint installed on the server), free/open tooling.
**Date:** 2026‑08‑31

---

## 1. TL;DR

- A Templafy‑generated `.pptx` is **not AI magic** — it is deterministic manipulation of the raw PowerPoint file format (OOXML) plus a rules/binding layer. **~90% of what we pay Templafy for is automation we can replicate with free Python libraries.**
- Templafy assembles a deck from **five parts**: a *template* with placeholder slides, a *Slides Library* of pre‑branded content (each with an **asset ID**), a *response form* (the gating questions), a *data source* (answer → asset ID / text lookup), and an *assembly engine* that stitches it together.
- **Our real unknown:** we only have **one raw template with title + body placeholders**. We do **not** yet know where the 50–60 slides of text, images and elements come from (Templafy Slides Library? data sources? an external system like SharePoint/CRM? AI‑generated?). Almost certainly a **mix**.
- **The answer is already inside any deck Templafy has produced for us.** A generated `.pptx` carries Templafy's fingerprints in custom XML parts, content‑control tags, shape alt‑text, document properties and relationships. Reading those tells us exactly which sources fed the build.
- **Phase 0 = run the included inspector (`inspect_pptx.py`) on our own decks, locally.** No confidential template ever has to leave our machines. The output tells us what to build next.

---

---

## 1b. UPDATE — Investigation results (from a real generated deck)

Running the inspector on a real 61-slide generated deck (`01_audit_only_new_tech_software.pptx`) confirmed the mechanism and revealed the content sources. **It is a hybrid**, and — crucially — the assembly is **"superset master + delete", not "empty template + insert".**

**Evidence:**
- **Superset + delete:** `docProps/app.xml` reports **Slides: 265** while the package contains **61**. Templafy starts from the ~265-slide master and **deletes the slides the payload didn't select**. *(We now have the 265-slide master in hand — this is the source of truth.)*
- **Dynamics bindings are white-box:** `datasource` appears in `customXml/item118/124/126`, `binding` in `item27/30/31/33/35/38`, `assetid` in `item126` + `slide12.xml.rels`. **Those XML parts literally contain the answer→content lookup tables and rules** — we can read them directly rather than only inferring from examples.
- **External systems in the loop:** `connector` (47 parts), `sharepoint` (13), `dam` (6), plus `ContentTypeId` in custom.xml. Several media files are **0 KB = externally linked images** (pulled from SharePoint/DAM, not embedded).
- **Profile injection:** `profile` in custom.xml + slide33/slide36 (author/contact from the Templafy user profile).
- **Post-processing:** deck carries a **Microsoft sensitivity label** (`MSIP_Label … = Internal`) and was run through **NXPowerLite** compression. Our headless builder must re-apply the sensitivity label.

**Implication:** the replacement engine is mostly **(a) a selection rule over the 265-slide master + (b) token injection (client/date/profile) + (c) linked-asset resolution** — all replicable in free Python. The only genuine external dependency is the DAM/SharePoint-linked assets.

---

## 1c. Current approach — Extract + Diff + Harvest (refined from "run all variations")

The team's instinct (generate many payload→deck pairs and learn the mapping) is sound. Two upgrades make it far cheaper and more reliable:

1. **White-box first:** the rules are already in the master's `customXml` (`item118/124/126` = data sources; `item27/30…` = bindings). Extract them; use input/output inference only as a cross-check.
2. **Spanning set, not exhaustive:** vary **one payload field at a time** (OFAT) instead of enumerating every combination. ~15–25 targeted payloads teach more than hundreds of random ones.

**Must-do test:** generate the **same payload twice and diff the two decks.** Identical → content is deterministic/curated (harvestable). Different body text → there is an LLM step to reproduce separately. *This single test decides whether the builder needs an AI component.*

**The pipeline (current step = 2–4):**
1. **Extract** — parse each deck's customXml → data sources, bindings, asset IDs (the actual rules).
2. **Diff** — match every generated slide back to its master origin; record which master slides each payload kept vs. dropped. → `map_decks.py`
3. **Correlate** — for each master slide, is it *always kept* or *conditional on field=value*? Detect injected tokens. → `map_decks.py`
4. **Map** — one machine-readable `mapping_spec.json`, refined incrementally as more pairs are added.
5. **Harvest** — the generated decks + the 265 master ARE the content library; no separate Slides-Library access needed.
6. **Assemble (headless Python)** — new payload → mapping spec → select master slides → inject tokens → resolve linked images → re-apply theme + sensitivity label → output `.pptx`.

**Risks to plan around:** external DAM/SharePoint-linked assets (may need those integrations or embed-on-harvest); determinism (pending the twice-run test); sensitivity-label re-stamping; contractual/ToS check on reverse-engineering a licensed tool's output (worth confirming with the contract owner — not a blocker for an internal POC).

---

## 2. How Templafy builds a branded deck (the mechanism to replicate)

Five moving parts:

| Part | What it is | Our equivalent |
|---|---|---|
| **Slides Library** | Pre‑approved branded slides/sections, each stored as a mini‑presentation with a unique **asset ID** | Our SME‑provided templates & elements |
| **Smart template** | A master deck with **placeholder slides** marking where content is injected | Our raw proposal template |
| **Response form** | The gating questions (service line, client, date…) as **dropdown / checkbox / text** fields | Our input UI |
| **Data source** | A table/JSON mapping *answer option → asset ID* (and text values) | A config/lookup file or DB |
| **Assembly engine** | Replaces placeholders with library slides by asset ID, fills text/images from profile + data source + form, sets doc properties, applies color theme | **The part we need to build** |

**Assembly logic (from Templafy's own docs):**

1. User answers the response form (e.g. *service line = "Audit"*).
2. The engine looks the answer up in the **data source** → gets an **asset ID**.
3. A **placeholder slide** in the template is *replaced* by the referenced library presentation — **all** of that presentation's slides are inserted at that position. *Static* insertion = fixed asset ID; *dynamic* insertion = answer‑driven asset ID.
4. **Smart fields** (text/image) inside slides are bound to a source — **user profile, data source, response form, or an external system** — and filled in.
5. Post‑processing: set document name, document properties, custom XML values, and **apply the brand color theme**.

**Key technical takeaways:**

- A `.pptx` is a **ZIP of XML** (OOXML). Everything Templafy does is programmatic OOXML editing + a binding/rules layer.
- "Insert slide" = **copy whole slides between decks while preserving master/layout/theme/media**. This is the single hardest technical problem in the whole effort — text and image injection is comparatively easy.
- **Branding consistency comes from the library slides already being branded** + applying the master's color theme — not from the tool "designing" anything.

**Sources:**
- Templafy — [Dynamics capabilities for PowerPoint](https://support.templafy.com/hc/en-us/articles/5138869988765-Dynamics-capabilities-for-PowerPoint)
- Templafy — [How to dynamically insert slides from the Library in a presentation](https://support.templafy.com/hc/en-us/articles/5120634464925-How-to-dynamically-insert-slides-from-the-Library-in-a-presentation)
- Templafy — [Document automation in PowerPoint (rules + AI hybrid)](https://www.templafy.com/introducing-document-automation-in-powerpoint/)

---

## 3. The open question: where does the content come from?

We have **one raw template with only title + body placeholders**. That template cannot, by itself, produce a 50–60 slide branded proposal. So the content is coming from somewhere else. The realistic possibilities:

1. **Templafy Slides Library** — content pre‑authored as branded library slides (asset IDs), inserted based on answers.
2. **Data sources** — text/values pulled from a table or JSON keyed on the form answers.
3. **External systems** — CRM / SharePoint / DAM (client name, logos, boilerplate, disclaimers).
4. **AI‑generated** — narrative body text generated per section.

It is almost certainly a **combination**. We cannot design the replacement engine until we know the split. **Phase 0 determines it.**

---

## 4. Reverse‑engineering approach (respects confidentiality)

We do **not** need to share any confidential template with anyone (including external tools) to learn how it's wired. A Templafy‑produced deck stores its own provenance:

- **Custom XML parts** (`customXml/item*.xml`) — Templafy's binding/config metadata.
- **Custom document properties** (`docProps/custom.xml`) — often the response‑form answers and profile values (client, date, service line).
- **Shape names & alt‑text** (`descr`) — smart‑field binding JSON frequently lives here.
- **Content controls** (`sdt`) — another binding mechanism.
- **Relationships** (`*.rels`) — reveal external links (SharePoint/Salesforce/DAM) and image sources.
- **Theme** (`theme1.xml`) — the brand color scheme.

The included `inspect_pptx.py` reads all of these and prints a structured report. It runs **100% locally, makes no network calls, and never modifies the file.**

---

## 5. Phase 0 — Discovery (what to do now)

**Goal:** Produce a concrete map of where each piece of content in a generated deck originates, so we can scope the build.

**Steps:**

1. Gather two files (kept local/confidential):
   - **(a)** the raw proposal template (title/body placeholders), and
   - **(b)** at least **one real generated output deck** from Templafy (ideally for a dummy/sanitised client if possible).
2. Run the inspector on each (see §6).
3. Read the report against the interpretation guide (§7) and record, per slide/section, the **content source**.
4. Fill in the findings table (§8) and bring it back — that drives Phases 1–4.

**Definition of done for Phase 0:** every slide/section in the sample deck is tagged with one of: *library slide*, *data‑source text*, *form/profile value*, *external system*, or *literal/AI text* — plus a list of any external systems referenced.

---

## 6. How to run the inspector

Requires only **Python 3.8+** (standard library — nothing to install).

```bash
# Basic structural report to the console
python inspect_pptx.py "path/to/generated_deck.pptx"

# Save the report as Markdown to share internally
python inspect_pptx.py "path/to/generated_deck.pptx" --report deck_report.md

# Also dump the custom XML + docProps to a folder for manual inspection,
# and include short text previews (treat output as confidential)
python inspect_pptx.py "path/to/generated_deck.pptx" --dump-xml out_xml --show-text
```

Run it on **both** the raw template and a generated deck, and compare — the *difference* between them is exactly what Templafy injected.

> **Note:** `--show-text` and `--dump-xml` write actual deck content to disk. Only use them where that's acceptable, and treat the output with the same confidentiality as the deck.

---

## 7. How to read the output (interpretation guide)

| What you see in the report | What it means | Implication for our build |
|---|---|---|
| **Provenance keyword** hits for `templafy` / `assetId` / `dataSource` / `binding` | Content is wired through Templafy Dynamics; surrounding XML names the data source / asset IDs | Replicate with our own data‑source lookup + slide library |
| **Custom document properties** like `client`, `date`, `serviceLine`, `author` | Those values came from the **response form** or **user profile** | Straightforward: capture in our input form, inject as text |
| **Shape name / alt‑text** contains `{ … }`, `binding`, or `templafy` | That shape is a **smart field**; the JSON is its binding config | Tells us the exact field‑level mapping to reproduce |
| **Many slide layouts / masters**, or slide text is fully **literal** | Whole branded slides were **inserted from the Slides Library** | We need a slide‑copy engine + a library of branded section decks |
| **External** relationships to SharePoint / Salesforce / a DAM | Content pulled from an **external system integration** | Scope an integration; this is the main non‑trivial dependency |
| **Body placeholders** with rich literal text and **no** binding markers | Static library content **or AI‑generated** narrative | Decide: curated boilerplate vs. an LLM generation step |
| **Custom XML parts = 0**, no keyword hits | Likely a hand‑made/static deck, not Dynamics‑built | Simpler than feared — mostly text/image injection |

---

## 8. Findings table (fill in during Phase 0)

| Slide / section | Content type (title/body/image/table) | Apparent source | Evidence (part / property / marker) | Notes |
|---|---|---|---|---|
| | | | | |
| | | | | |

---

## 9. Full POC roadmap (after Phase 0)

- **Phase 1 — Content model & library format.** Based on Phase‑0 findings, define how SMEs supply content going forward (data‑source tables for text, an image/asset folder, and — if needed — a small library of branded section decks).
- **Phase 2 — Assembly engine (core POC).** Python (`python-pptx` + OOXML): load the branded template, fill title/body placeholders, inject images/elements, apply the brand color theme, set doc properties, output the draft `.pptx`. **Prove out headless whole‑slide copy early — it's the main technical risk.**
- **Phase 3 — Input → binding layer.** A form/API (e.g. FastAPI) that takes service line, client, date… → data‑source lookup → engine. Mirrors Templafy's *response form + data source* model.
- **Phase 4 — Fidelity & scale.** Diff generated decks against Templafy output; harden; batch/API.

**Known risk areas:** (a) high‑fidelity whole‑slide copy while headless, and (b) any content that turns out to come from an external system we'd still need to integrate.

---

## 10. Confidentiality & safety notes

- `inspect_pptx.py` is **read‑only** and makes **no network calls** — safe to run on confidential decks on a locked‑down machine.
- No template or generated deck needs to be shared externally to complete Phase 0.
- Keep any `--dump-xml` / `--report --show-text` output under the same access controls as the source decks.

---

## 11. How to run the mapper (`map_decks.py`) — current step

Requires only **Python 3.8+** (standard library). Keep `pptx_forensics.py` in the same folder.

Layout your files like this:
```
master_265.pptx
decks/     01_audit.pptx, 02_advisory.pptx, ...   (the generated decks)
payloads/  01_audit.json, 02_advisory.json, ...   (the REST payloads used)
```
Name each payload with the **same stem** as its deck so they auto-pair (or pass `--manifest pairs.csv` with a `pptx,payload` header).

```bash
python map_decks.py --master master_265.pptx --decks ./decks --payloads ./payloads --out ./mapping
```

Outputs to `./mapping/`:
- **`mapping_report.md`** — human summary: match quality, which master slides are *always kept* / *conditional on a field* / *unclear*, and detected injected tokens.
- **`mapping_spec.json`** — machine-readable rules to feed the future assembler.

**How to read it:**
- **Check "Match quality" first.** Slides matched *via creationId* are reliable. Lots of low-confidence/text-only matches ⇒ those master shapes lack creationIds; inspect them by hand.
- **"Always kept"** = your static skeleton. **"Conditional"** with **high** confidence = a real selection rule (e.g. `serviceLine=audit` keeps slides X–Y). **"Unclear"** = add more OFAT payloads to resolve.
- Cross-check the inferred conditional rules against the master's `customXml` data sources (`item118/124/126`) — those are authoritative.

**Iterate:** add more (payload, deck) pairs and re-run — the spec sharpens each time. This is the "keep running, logic updates" loop.

---

### Appendix — files in this drop
- `templafy_findings_and_phase0.md` — this document.
- `inspect_pptx.py` — zero‑dependency, read‑only OOXML inspector (usage in §6).
- `pptx_forensics.py` — shared, read‑only parsing library (imported by the mapper).
- `map_decks.py` — the payload→deck mapper that learns selection + token rules (usage in §11).
