# Spike: can Gemini Enterprise chat show a built deck?

**Read all of this before doing anything.** It is written for a session that
knows nothing about the project it came from. Everything you need is here or at
a path named here.

---

## 1. What we are trying to find out

We have a deck builder (`ppt_gen_v3`, a separate project) that takes a JSON
payload of answers and produces a PowerPoint deck. Its preview is a series of
**slide images** — PNGs rasterised from a PDF of the slide library.

The idea being tested: put this inside **Gemini Enterprise (GE) chat** as an
agent. An A2UI form collects the payload, the agent builds the deck, shows the
slides as images in the chat, and gives a link to download the `.pptx`.

Most of that is already proven in an earlier project. **One thing is not, and
it decides the whole design: will GE display an image from a private URL?**
Slides are confidential client material, so they can never be at a public URL.

This spike answers that and three smaller questions. **It builds no deck.** It
uses static images and a static `.pptx` so that the only thing under test is
what GE will render.

### The four questions

| # | question | why it matters |
|---|---|---|
| **T1** | Does GE render an `Image` from a **Google Cloud Storage signed URL**? From an **Azure blob SAS URL**? | decides where the agent and the images live |
| **T2** | Can one card show **10**, then **60** slide images? | real decks run to 60 slides |
| **T3** | Does a **markdown link** to a signed `.pptx` URL download the file from inside GE? | the download path; there is no file component |
| **T4** | Does a realistic **form step** deliver its values intact? | the payload has to arrive exactly as typed |

**T1 is the one that matters.** If it fails for every host, the idea as stated
is not viable and T2 is moot.

---

## 2. Rules for this spike

- **Synthetic data only.** Use the demo assets named in §4. Never upload a real
  client template, deck or payload anywhere during this spike.
- **Deterministic, no LLM in the path.** The agent emits cards from a callback,
  chosen by exact trigger words. The model's text reply is irrelevant.
- **One question per card.** Each image source gets its own surface, so a
  failure in one (a bad `data:` URI can blank a whole card) cannot hide a pass in
  another.
- **Record everything in §9 as you go**, including failures and exact error
  text. A failure with its error message is a result; a failure without one is
  a rerun.
- **Stop after setup and after each test** and tell the user exactly what to
  look at in GE chat. Only GE chat renders A2UI — you cannot see it yourself.

---

## 3. What is already known — do not re-learn it

All of this was verified in GE in the earlier project. Source of truth:
`C:\GenAi_Prjcts\claude-cowork-demo\a2ui_gallary\v0p9\guidelines.md` §7.

**Use A2UI v0.9.** The `gemini-a2ui-agent` skill says "GE supports v0.8 only".
That is **out of date** — GE has rendered v0.9 since May 2026. The skill is still
right about transport, deployment and version pins; follow `guidelines.md` for
everything v0.9.

Confirmed working:

- **`"version": "v0.9"` on every message.** Without it GE silently falls back
  to v0.8 and renders nothing.
- **`catalogId`** must be the full basic-catalog URL:
  `https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json`.
  The short `"material"` id fails with "Catalog not found".
- **Transport**: the `<a2a_datapart_json>` wrapper with mime type
  `application/json+a2ui`, one message per DataPart. Raw `inline_data` gives
  "Unsupported attachment".
- **Fresh `surfaceId` per card** (`f"name-{uuid4().hex[:12]}"`). Reusing one
  updates the first card instead of drawing a new one.
- **Images render in v0.9** — but only a **public Google-hosted URL**
  (`https://www.gstatic.com/webp/gallery/1.jpg`) has been tried.
- **Markdown renders only in `Text` with `variant: "body"`** — not `caption`.
  Links are clickable.
- **Form write-back works only on flat, single-level paths**: `/cuisine`, not
  `/form/cuisine`. Button `action.event.context` arrives **already resolved** to
  the chosen values.
- **"User action triggered."** appears on every click and cannot be changed.
- **The Agent Engine Playground and `adk web` show raw JSON only.** Judge
  rendering in GE chat, nowhere else.

Known to fail, in the older **v0.8** — none of these have been retried on v0.9:

- an external non-Google image host (picsum) → *"This content could not be
  displayed"*, a CSP block
- a base64 `data:` URI in an `Image` → not rendered
- a `data:` URI inside markdown → **blanks the entire card**

### Exact v0.9 shapes (copied from the working concierge)

```json
{"version":"v0.9","createSurface":{"surfaceId":"s-1a2b3c","catalogId":"https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json","sendDataModel":false}}
{"version":"v0.9","updateComponents":{"surfaceId":"s-1a2b3c","components":[ ... ]}}
{"version":"v0.9","updateDataModel":{"surfaceId":"s-1a2b3c","path":"/","value":{ ... }}}
```

| component | shape |
|---|---|
| Column / Row | `{"id","component":"Column","children":["a","b"],"align":"stretch"}` |
| Card | `{"id","component":"Card","child":"one_id"}` — a single child |
| Text | `{"id","component":"Text","text":"…","variant":"h4"\|"body"\|"caption"}` |
| Image | `{"id","component":"Image","url":"…","fit":"contain","variant":"largeFeature"}` |
| Button | `{"id","component":"Button","child":"label_text_id","variant":"primary","action":{"event":{"name":"…","context":{…}}}}` |
| Modal | `{"id","component":"Modal","trigger":"button_id","content":"column_id"}` |
| Tabs | `{"id","component":"Tabs","tabs":[{"title":"…","child":"id"}]}` |
| TextField | `{"id","component":"TextField","label":"…","variant":"shortText","value":{"path":"/key"}}` |
| DateTimeInput | `{"id","component":"DateTimeInput","label":"…","value":{"path":"/key"},"enableDate":true,"enableTime":false}` |
| CheckBox | `{"id","component":"CheckBox","label":"…","value":{"path":"/key"}}` |
| ChoicePicker | `{"id","component":"ChoicePicker","label":"…","variant":"mutuallyExclusive"\|"multipleSelection","displayStyle":"chips"\|"checkbox","value":{"path":"/key"},"options":[{"label":"…","value":"…"}]}` |

**Use `"fit": "contain"` for slides.** `cover` crops, and a cropped slide
misrepresents the deck.

For anything not in this table, read
`C:\GenAi_Prjcts\claude-cowork-demo\a2ui_gallary\v0p9\agent\concierge.py`
rather than guessing.

---

## 4. Setup

### 4.1 Copy the proven scaffolding

From `C:\GenAi_Prjcts\claude-cowork-demo\a2ui_gallary\v0p9\` into this folder:

| copy | why |
|---|---|
| `agent/a2ui.py` | the transport wrapper — use it unchanged |
| `deploy_to_agent_engine.py` | create-or-update deployment |
| `requirements.txt` | **the version pins are load-bearing** — `google-cloud-aiplatform[agent_engines,adk]==1.148.1`, `google-adk[a2a]==1.31.1`. Other combinations crash in Agent Engine with `'coroutine' object has no attribute 'id'` |
| `env.dev.example` | then fill in `.env.dev` |

Also read `agent/agent.py` there for two things to carry over:
`_strip_history` (a `before_model_callback` that stops the model echoing A2UI
JSON into the chat) and `_extract_action` (parses an incoming click).

**Give the deployment its own display name**, e.g. `deck-preview-spike`, so it
creates a new Agent Engine resource and does not overwrite the concierge.

Use a fresh virtual environment with exactly those pins.

### 4.2 Make the test assets — synthetic only

Sources, both synthetic and safe:

- `C:\GenAi_Prjcts\claude-cowork-demo\Templfy\proposal_builder\ppt_gen_v3\templates\demo\library.pdf` — a 9-page slide library
- `C:\GenAi_Prjcts\claude-cowork-demo\Templfy\proposal_builder\ppt_gen_v3\fixtures\targets\p1_new_client.pptx` — a deck built from a synthetic payload

Make, in `assets/`:

1. `slide-01.png` … `slide-09.png` — the 9 PDF pages rasterised at **1200 px
   wide** with `pypdfium2` (`page.render(scale=1200/page.get_width()).to_pil().save(...)`).
2. `slide-10.png` … `slide-60.png` — copies of those 9, cycling, so there are 60
   **distinct file names**. Distinct names mean distinct URLs, so a browser cache
   cannot make a failure look like a pass.
3. `tiny.png` — one slide shrunk to about 160 px wide, a few KB, for the `data:`
   URI check.
4. `proposal.pptx` — a copy of `p1_new_client.pptx`.

### 4.3 Host them

**Google Cloud Storage** — a **new, private bucket**: uniform access, public
access prevention on. Same project as the agent is fine.

> The `gcloud` commands below were written without a `gcloud` install to hand,
> so their flags are unverified. **Run `gcloud <command> --help` and confirm
> each flag before using it** — and adjust rather than improvise if one differs.
> If `gcloud` is missing, install the Google Cloud CLI first.

```
gcloud storage buckets create gs://<bucket> --location=<region> --uniform-bucket-level-access --public-access-prevention
gcloud storage cp assets/slide-*.png gs://<bucket>/spike/ --content-type=image/png
gcloud storage cp assets/proposal.pptx gs://<bucket>/spike/proposal.pptx --content-type=application/vnd.openxmlformats-officedocument.presentationml.presentation --content-disposition="attachment; filename=\"proposal.pptx\""
```

**Signed URLs — make them locally for the spike, not inside the agent.** V4
signed URLs last up to 7 days, which covers the spike, and it keeps IAM out of
the question being tested. Signing needs a service account; your user login
cannot sign V4 URLs.

```
gcloud storage sign-url gs://<bucket>/spike/slide-01.png --duration=7d --impersonate-service-account=<sa>@<project>.iam.gserviceaccount.com
```

You need `roles/iam.serviceAccountTokenCreator` on that service account to
impersonate it. Sign every object the tests use and write the URLs into a
`urls.json` the agent loads at start-up.

> **For the real build, not now:** signing inside Agent Engine has no key file.
> It needs `blob.generate_signed_url(version="v4", service_account_email=…,
> access_token=…)` with credentials from `google.auth.default()`, and the
> runtime service account granted Token Creator on itself. Note it in §9 as a
> follow-up; do not solve it in this spike.

**Azure** — a storage account with a **private** container. Upload the same
`slide-01.png`, then generate a read-only SAS URL for that one blob (portal:
blob → *Generate SAS* → Read, 7 days, HTTPS only). Skip Azure if there is no
account to hand, and say so in §9 — do not let it hold up T1.

---

## 5. The agent

One `LlmAgent`. Its instruction: *"Reply with exactly one line naming the test
that was run. Never output JSON."*

Everything real happens in the `after_model_callback`: read the user's text,
match it **exactly** against the trigger words below, and append that test's
cards. Unknown text gets a card listing the trigger words. No model judgement
anywhere in the path.

| user types | emits |
|---|---|
| `help` | a card listing every trigger word |
| `t1` | the T1 cards (§6) |
| `t2-10` / `t2-60` / `t2-modal` | the T2 cards (§7) |
| `t3` | the T3 card (§8) |
| `t4` | the T4 form (§8); its submit button comes back as a click, answered with an echo card |

Deploy, register in GE (Admin console → Agents → Add agent → Vertex AI Agent
Engine → paste the `projects/.../reasoningEngines/...` resource name), and
**stop: tell the user to type `help` in GE chat and confirm the card renders.**
If `help` doesn't render, nothing else will — fix that first.

### How to read a failure

When an image fails, have the user open the browser's developer tools
(**F12 → Console**) before retrying. A CSP block prints a line like:

> Refused to load the image '…' because it violates the following Content
> Security Policy directive: "img-src …"

**Copy the whole `img-src …` list into §9.** It is GE's image allowlist, and it
answers T1 for every host at once — including hosts not tested here.

---

## 6. T1 — which image hosts render

One response, **six separate surfaces**, each titled with what it is testing:

| surface | image URL | expected |
|---|---|---|
| **T1-control** | `https://www.gstatic.com/webp/gallery/1.jpg` | renders — already proven; if it doesn't, the card is broken, not the host |
| **T1-gcs-signed** | GCS signed URL for `slide-01.png` | **the question** |
| **T1-gcs-public** | the same object via `https://storage.googleapis.com/<bucket>/spike/public-01.png` — upload a copy to a separate *public* bucket, or skip | separates "Google Storage is blocked" from "the signature query string is the problem" |
| **T1-azure-sas** | Azure SAS URL for `slide-01.png` | the non-Google host |
| **T1-other** | any public non-Google image, e.g. `https://picsum.photos/800/450` | did v0.9 lift the v0.8 block on arbitrary hosts? |
| **T1-datauri** | `data:image/png;base64,…` of `tiny.png` | **send this last, in its own surface** — in v0.8 a data URI blanked its whole card |

Each surface: `Card > Column > [Text h4 "T1-gcs-signed", Image]`.

**Record for each:** renders / *"This content could not be displayed"* / empty
card / blank bubble / error, plus any Console line.

---

## 7. T2 — how many slides fit on one card

Run only if at least one host passed T1. Use the best host.

| trigger | layout |
|---|---|
| `t2-10` | `Card > Column > [Text h4, Image × 10]` |
| `t2-60` | same, 60 images |
| `t2-modal` | `Card > Column > [Text h4, Row of 3 Images, Button "All 60 slides"]`, plus `Modal(trigger=button, content=Column of 60 Images)` |

The Modal layout is the likely real design: a glance inline, the whole deck one
click away, and no Prev/Next buttons. **Paging with buttons is ruled out** —
every click is a round trip plus a "User action triggered." bubble.

**Record:** do all images render? in order? roughly how long until the last one
appears? any size error on the response? Does `t2-60` render at all, or does
the whole response fail?

---

## 8. T3 and T4

### T3 — download the deck

One card: `Card > Column > [Text h4 "T3-download", Text body "[Download proposal.pptx](<signed url>)"]`.

The Text **must** be `variant: "body"`; markdown does not render in `caption`.

**Record:** is the link clickable? Does GE show a warning page before external
links? Does it open a new tab or download directly? Does the downloaded file
open in PowerPoint? If the URLs are still valid, also try one generated with
`--duration=1m` after it has expired — it **should** fail. A download link that
never dies is its own finding.

### T4 — a realistic form step

One surface, all flat paths except the one deliberate exception:

| field | component | path |
|---|---|---|
| Full client name | TextField | `/FullClientName` |
| Due date | DateTimeInput, date only | `/DueDate` |
| Transition lab | CheckBox | `/Transition_lab` |
| Industry sub-sector | ChoicePicker, `mutuallyExclusive`, chips, **~40 made-up options** | `/Industry_sub_sector` |
| Services | ChoicePicker, `multipleSelection`, chips, 6 options | `/Services` |
| City | TextField | `/client/city` — **deliberately nested**, to confirm the flat-only rule still holds in v0.9 |

Seed defaults with `updateDataModel` at `path: "/"`. Add a primary **Build**
button whose `action.event.context` maps each key to its path, e.g.
`"DueDate": {"path": "/DueDate"}`.

When the click comes back, reply with one card listing **every received key,
its value, and its JSON type**.

**Record:**
- Did every value arrive? With the right type — boolean, list, string?
- **The exact format of the date.** An ISO string? Does it carry a time or a
  timezone? The deck builder expects `YYYYMMDD`, so this decides the conversion.
- Did `/client/city` arrive, or come back empty? That confirms or overturns the
  flat-only rule.
- Is a 40-chip picker usable, or a wall? (There is no collapsed dropdown in the
  v0.9 basic catalog, and `filterable` was ignored last time — confirm.)

---

## 9. Results log — fill in as you go

| test | result | exact error or observation |
|---|---|---|
| setup: `help` card renders | | |
| T1-control (gstatic) | | |
| T1-gcs-signed | | |
| T1-gcs-public | | |
| T1-azure-sas | | |
| T1-other (picsum) | | |
| T1-datauri | | |
| GE `img-src` allowlist, if a Console line appeared | | |
| T2-10 | | |
| T2-60 | | |
| T2-modal | | |
| T3 link clickable / downloads / opens in PowerPoint | | |
| T3 expired link fails | | |
| T4 values and types | | |
| T4 date format received | | |
| T4 nested path `/client/city` | | |
| T4 40-chip picker usable? | | |
| follow-up: signing inside Agent Engine | not tested — as planned | |

---

## 10. What the results mean

| outcome | design |
|---|---|
| **T1-gcs-signed renders** | Build the real thing on Google: the deck engine runs *inside* the agent (it is pure Python, no Office needed), libraries are published to Cloud Storage, and slides and the `.pptx` go out as short-lived signed URLs. |
| gcs-signed fails, **gcs-public renders** | The query string is the problem, not the host. Try a signed-cookie or proxy approach before giving up; do not make client slides public. |
| gcs fails, **azure-sas renders** | The agent can stay thin and call the Azure-hosted builder, which serves the images. |
| **only the gstatic control renders** | GE allowlists Google's own asset hosts only. Slides-in-chat is not viable. Fall back to: form in chat → build → **download link only**, plus a link to the web app's viewer for the preview. |
| data URI renders on v0.9 | Note it, but **do not design around it**: 60 slides of base64 in one message is megabytes. |

Whatever the outcome, report it with the §9 table filled in.

---

## 11. Clean up

- Delete the `spike/` objects, and the public bucket if one was made.
- Revoke the Azure SAS, or let it expire.
- Keep the Agent Engine resource and GE registration only if the user wants to
  go further. It has its own display name, so it does not affect the concierge.
