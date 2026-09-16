# Rendering the built deck through Microsoft Graph — findings

**It works.** A deck built from a JSON payload was uploaded to SharePoint,
converted by Office Online, rasterised, and the uploaded copy removed — and the
cover reads `Globex International Holdings`, not `{{FullClientName}}`. That is
the one thing the library-PDF preview structurally cannot do.

**And it very nearly shipped with a confidentiality hole.** A Graph `DELETE`
does not delete: it moves the file to the site recycle bin, where it stays for
the retention period, readable. Twenty-two "deleted" decks were found sitting
there. That is fixed — §5 — but it is the most important thing in this
document, because every automated check passed while it was true.

Whether the whole thing is worth what it costs is §8, and the answer is
qualified.

Everything below was run against a **real Microsoft 365 tenant** unless marked
*fake*. The tenant is a personal Business Standard trial, not the firm's, which
matters for several results and is called out where it does.

Read `CLAUDE.md` in this folder first. This is the results file it asks for.

---

## 1. What was built

A fourth export backend, `graph`, alongside `library`, `libreoffice`,
`powerpoint` and `svg`.

- `engine/graph.py` — the Graph client: token, upload, convert, delete, purge.
- `engine/render.py` — `_probe_graph` / `_export_graph`, one more pair in
  `BACKENDS`. Nothing else in that file changed shape.
- `app.py` — `?engine=graph` is accepted and refuses with 503 rather than
  falling back, exactly as the other named backends do.
- `tools/fake_graph.py` — a recorded Graph, used by the checks.
- `tools/graph_setup.py` — `grant`, `discover`, `check`, `sweep [--delete]`.
- 18 new checks in `tools/check_api.py`. Full set: **83 passed, 0 failed**, and
  they pass with the network blocked, so none of them can reach a tenant.

`graph` is **last** in `ORDER`, so `auto` never reaches it while any other
renderer works. It is chosen only by name or by `PPTGEN_RENDERER=graph`.

One wrinkle when comparing backends by hand: they all write into the same
`data/renders/<token>/`, and the marker there records one engine per folder, so
`/png/{n}` serves whichever ran last. Pre-existing, and `CLAUDE.md` §4 says not
to restructure, so it was left alone.

## 2. The shape of a render, as observed

Nine HTTP requests on the first render of a process, six after that:

| # | request | note |
|---|---|---|
| 1 | `GET …/.well-known/openid-configuration` | msal will not ask for a token without it |
| 2 | `POST …/v2.0/token` | client credentials, scope `…/.default` |
| 3 | `PUT /drives/{drive}/items/root:/pptgen-render/<name>:/content` | the deck goes to SharePoint |
| 4 | `GET /drives/{drive}/items/{id}/content?format=pdf` | answers **302**, not the PDF |
| 5 | `GET <pre-authed URL>` | the PDF itself, **unauthenticated** |
| 6 | `DELETE /drives/{drive}/items/{id}` | removes it from the folder — **not** a deletion |
| 7 | `GET /drives/{drive}/root?$select=sharepointIds` | which site the drive is in, cached after the first |
| 8 | `GET /beta/sites/{site}/recycleBin/items?$filter=name eq …` | is it in the bin? |
| 9 | `POST /beta/sites/{site}/recycleBin/items/delete` | purge it for good |

Requests 1, 2 and 7 happen once per process; the rest happen every render.

**The token is two round trips, not one.** `engine/graph.py` keeps the msal
application between renders, which removes 1 and 2 for every render after the
first — a check counts them (`2 cold, 0 warm`).

**Step 4 hands back a credential.** `format=pdf` redirects to a pre-authed
SharePoint URL. Our bearer token is never sent on to it, and that URL never
appears in an error message. Both are enforced by checks.

**Steps 8 and 9 are on the `beta` endpoint.** There is no v1.0 way to empty a
recycle bin through Graph. That is a real dependency, and it is in §7.

## 3. Timings, against real Graph

Demo library, 1600 px, domestic connection, after the purge fix. The
library-PDF column is the same machine and the same deck.

| deck | library PDF | graph, first render | graph, steady state |
|---|---|---|---|
| 8 slides | **0.20 s** | 10.2 s | **6.1 – 7.3 s**, median 7.2 s |
| 61 slides | **1.50 s** | — | **13.8 s** *(measured before the purge fix; add ~2 s)* |
| 61 slides, 6.6 MB | — | — | **13.6 s** *(same caveat)* |

- **The purge costs about two seconds** — two extra round trips per render.
  Correctness is worth it, but it is not free.
- **The first render of a process is slower**, and not only because of the
  token: even warm, the first conversion runs long. Office Online appears to
  warm up per tenant.
- **One run in six took 28.7 s.** Everything else was 6–7 s. Tail latency on a
  hosted conversion service is real, and a preview that usually takes seven
  seconds and occasionally takes thirty needs a progress indicator, not a
  spinner that looks hung.
- **Steady state is ~35× the library path** for a short deck. The local half is
  identical — both are pypdfium2 — so all of the difference is network and
  Office Online.
- File size barely matters: 6.6 MB through an upload session cost the same as
  80 KB through a simple PUT.

### Confirmed through the app, not just a script

The `fixtures/` library (15 slides, three payloads) was imported through the
browser **with no library PDF**, rules authored on the Rules screen, and a deck
built from a payload. `auto` found no library PDF, fell through to the
converters, and with `PPTGEN_RENDERER=graph` landed on Graph. The preview showed
**filled values**. That exercises the path a user actually takes, including the
fallback — and it is the case the library-PDF preview cannot serve at all,
since a library with no PDF has no pages to copy.

## 4. The §7 questions, answered

| question | verdict |
|---|---|
| Does it render, and do the values show? | **Yes.** Verified by eye, and again in the browser. |
| How long for 8 slides? For 60? | **~7 s and ~16 s** steady state, against 0.2 s and 1.5 s. Occasional 30 s outliers. |
| Does the SharePoint item always disappear? | **Now yes** — folder *and* recycle bin. It did not before; see §5. |
| What happens over ~4 MB? | **Handled.** 6.6 MB through `createUploadSession` in 320 KiB-multiple chunks. |
| What does Graph do under repeated calls? | **No throttling at this scale.** Ten back-to-back renders drew no 429. Our handling of 429s is proven against a fake only. |
| SmartArt, a chart, the corporate font? | **Untested, and untestable here** — §7. The significant gap. |
| What fails when the network is down? | `?engine=graph` returns 503 with the reason; `auto` still previews from the library PDF. No 500. |

## 5. The deletion guarantee, and how it was wrong

This is the part worth reading twice.

The original implementation did what the design said: `DELETE` the uploaded
item, in a `finally`. Checks proved it happened after a success, after a forced
mid-render failure, and that a failed delete was raised rather than swallowed.
`sweep` listed the folder and reported it empty. All green.

**All of it was true and none of it was sufficient.** A SharePoint `DELETE`
moves the item to the site recycle bin. Listing the bin — which nothing did —
showed **22 decks**, every one from a render that had reported success,
retrievable in two clicks, including a 6.6 MB one and the decks from the
browser session.

Under `CLAUDE.md` §6 that is the exact failure the section exists to prevent,
and it was invisible because the tooling and the tests shared one blind spot:
the fake modelled a folder, and Graph has a bin.

**The fix.** `delete()` is now two operations — remove the item, then purge it
from the recycle bin via `POST /beta/sites/{site}/recycleBin/items/delete`. A
purge that fails is raised, with a message saying the deck is still in the bin
and must be removed by hand. `Prefer: permanent-delete` on the original DELETE
was tried first; SharePoint ignores it, and there is no v1.0 route.

**What is now proven, against the real tenant:**

1. **Leak on purpose** — upload, do not delete. `sweep` finds and names it.
   (Checked first: a sweep that cannot report dirty proves nothing when clean.)
2. **Fail mid-render** — real upload, conversion raises, `finally` removes it.
   Folder empty, bin empty.
3. **A full render** adds nothing to the bin, verified by counting before and
   after.
4. `sweep` now reports **both** places, and `--delete` empties both.

The 22 decks were purged. The site is empty.

**The lesson is not "we fixed a bug".** It is that a confidentiality guarantee
checked only by the code that implements it will confirm its own assumptions.
This was found by asking, while drafting the administrator request, "what
happens to the recycle bin?" — not by any test.

## 6. What it costs to set up, measured by doing it

1. An Entra ID app registration — minutes, free.
2. **A Microsoft 365 licence somewhere.** An Azure subscription is not enough:
   a bare Entra directory has no SharePoint, so there is no drive for
   `format=pdf` to operate on.
3. Admin consent for `Sites.Selected`.
4. **A per-site grant, which has no button anywhere.** The app cannot grant
   itself; an administrator must `POST /sites/{id}/permissions` while holding
   `Sites.FullControl.All`.

Least privilege is real and was demonstrated, not assumed: **before the grant,
the app with valid credentials got `403 Access denied` on every site in the
tenant, including reading its own.** After the grant it could see exactly one.

Step 4 also went wrong most often. Graph Explorer's *Modify Permissions* tab
lists only the permissions the URL currently in its address bar needs, so
looking for `Sites.FullControl.All` while the bar says `/me` finds nothing. And
`/sites?search=` fails with `generalException` on a young tenant, because
SharePoint search has not crawled yet. Both are in the output of
`graph_setup.py grant` now.

In the firm's tenant, step 4 is a request to another team.

## 7. What is still not proven

- **Fidelity.** `templates/demo/library.pptx` and the fixture master contain
  **no charts, no SmartArt, no images and no embedded fonts** — text and shapes
  only. The fidelity claim is not merely untested, it is *untestable with the
  assets this spike is permitted to use*. Answering it needs a decision: build
  a synthetic template that exercises SmartArt, a chart and the corporate font,
  or authorise one controlled test with real branding. **Nobody should rely on
  the fidelity claim until that happens.**
- **The second-stage recycle bin.** SharePoint has two. Purging from the first
  normally moves an item to the site-collection bin, which the Graph beta
  endpoint does not appear to expose. The bin we can see is empty; whether
  anything sits behind it needs checking in the SharePoint UI
  (`/_layouts/15/RecycleBin.aspx`, then *Second-stage recycle bin*). **Until
  that is confirmed, treat the fix as verified for the first stage only.**
- **A beta endpoint in the critical path.** Steps 8 and 9 are `/beta`, and beta
  endpoints change without notice. If it goes, renders fail loudly rather than
  quietly leaving decks in the bin — the right failure — but they do fail.
- **Throttling** at real volume; ten renders is not load.
- **A firm tenant** with conditional access, DLP and app-consent policy.
- **Timings from the deployment** rather than a domestic connection.

## 8. Recommendation

**Keep the library-PDF preview as the default. Offer `graph` as an explicit,
on-demand "show me exactly what the client will see" — not as the everyday
preview.** That is where it sits now, and where it should stay.

- **Speed is a real difference.** Seven seconds against a fifth of a second
  changes what a preview is. The library path can re-render on every edit;
  `graph` cannot, and a sixty-slide deck is a sixteen-second wait with
  occasional thirty-second outliers.
- **Every preview puts a confidential deck in SharePoint**, and §5 is the
  reason to weigh that properly rather than wave it through. The guarantee now
  holds, but it held "obviously" before too. As a default this is thousands of
  round trips through a document library; as a pre-send check it is one per
  proposal.
- **The operational cost is per-tenant admin work**, including a grant with no
  UI and a secret to rotate.
- **But it is the only deployable way to see filled values.** LibreOffice is
  blocked by policy, PowerPoint COM cannot run on Azure, and the library PDF
  cannot show values by construction. For the deliberate check before a deck
  goes to a client, `graph` is the only candidate that exists.

So: not a migration and not a rejection. The library PDF answers *which slides,
in what order, in the real branding*, instantly. `graph` answers *is this
exactly what the client will see*, in about seven seconds, when asked.

**Two conditions before anyone is told this is available:**

1. **The fidelity test in §7.** If Office Online renders the firm's SmartArt or
   corporate font badly, a "see exactly what the client sees" check is
   confidently wrong — worse than not having it at all.
2. **Confirm the second-stage recycle bin is empty**, and agree with whoever
   administers SharePoint what retention applies to that library. The guarantee
   should not rest solely on a beta API our own code calls.

---

## Setting it up

```
cp .env.example .env            # .env is gitignored; keep it that way
python tools/graph_setup.py grant    --site <host>.sharepoint.com:/sites/<name>
python tools/graph_setup.py discover --site <host>.sharepoint.com:/sites/<name>
python tools/graph_setup.py check
```

`grant` prints the two requests an administrator runs in Graph Explorer.
`discover` then finds `GRAPH_DRIVE_ID` with the app's own credentials, which is
itself the first proof the grant landed. Then `?engine=graph` on a build, or
`PPTGEN_RENDERER=graph`.

After anything that failed deliberately — and periodically regardless:

```
python tools/graph_setup.py sweep          # folder AND recycle bin
python tools/graph_setup.py sweep --delete # empty both
```

## Running the checks without a tenant

```
python tools/check_api.py
```

No credentials and no network: the graph checks set fake environment variables,
put `tools/fake_graph.py` in `engine.graph.TRANSPORT`, and replace the real
transport with one that raises — so a check cannot reach a live tenant even on
a machine with a working `.env`. The fake models the recycle bin as well as the
folder, because the version that did not let 22 real decks through.
