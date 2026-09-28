The Q&A module — RFP questions and their answer slides
A second pass over a deck that has already been built. The proposal is assembled from rules and answers as it always was; then the client's own RFP is read by Springboard, each question it finds is matched to an answer deck in the GPS blob container, and those slides are appended with an index in front of them.

Nothing here changes which slides the rules choose or what they say. It only adds a section on the end.

1. The flow, end to end
the user builds a deck            POST /api/libraries/{lib}/build
 └─ data/builds/<token>.pptx      (unchanged, and never modified after this)

 they tick "Add a Q&A section", choose a function line and a
 sub-serviceline, attach the RFP, and submit
                                  POST /api/qna/jobs   -> a job id, at once

 on a background thread, two to four minutes:
  ① POST /process_rfp             the RFP, Function_line, Sub_serviceline
  ② GET  /status/<job>            polled every 15s until COMPLETED
  ③ GET  /odata/Questions('<key>')  a flat entity: Q1/QN1/QA1, Q2/QN2/QA2 …
  ④ list the blob container       ~1560 .pptx answer decks, named by question
  ⑤ fuzzy-match each QN to a blob name, download the winners
  ⑥ append the answer slides      each carrying its question as a note
  ⑦ draw the index slides         ten questions to a slide, two columns, each
                                  question linked to its answer, then moved
                                  in front of the answers (§4)
  └─ data/builds/<new token>.pptx a *new* build; the original is untouched

 the screen polls GET /api/qna/jobs/{id} and, when it is done, points the
 preview and Download .pptx at the new deck

Q is the client's question, extracted from their RFP. QN is the label of the matched entry in the curated GPS library, and it is QN — never Q — that is matched against blob names, because the blobs are named after the library's own questions.

A question Springboard could not match says so itself, with a QN reading No high-confidence GPS match found…. Those questions still appear on the index slide. They are the ones somebody has to go and write, and hiding them would hide the remaining work.

2. What was added
New files
file	what it does
engine/springboard.py	the Springboard client and the blob matcher: submit, poll, read the entity, match, download
engine/qna_merge.py	appends slides from a foreign .pptx to a built deck, with all the parts they depend on
engine/qna_index.py	draws the two-column index slide
engine/qna_jobs.py	the job store: a thread per job, a JSON file per job, and the guarantees around them
engine/qna_options.py	reads and validates the function line list
qna_options.json	that list — the one file to edit to add a service line
QA.md	this
Changed
app.py — five endpoints (§5), and the module's config loaded at import.
web/index.html — the Q&A panel on the Build screen.
web/app.js — the toggle, the dropdowns, the upload, the polling.
web/app.css — the panel's layout, using only existing colour variables.
tools/check_ui_render.js — a canned /api/qna for the DOM stub.
requirements.txt — azure-storage-blob, optional, alongside msal.
.gitignore — data/qna_cache/, data/qna_jobs/, springboard/, .env.*.
.env.example — the four new variables and the two TLS escape hatches.
Removed
springboard/call_springboard_apis.py and springboard/springboard_qa_match_blobs.py were ported into engine/springboard.py and deleted, along with their readme.md and __pycache__. springboard/.env was folded into the project's own .env, so there is one config file rather than two; the loader still falls back to the old location for a checkout that has not moved.

3. Calling Springboard
The client lives behind a transport seam — springboard.TRANSPORT for HTTP and springboard.BLOBS for the container — exactly as engine/graph.py does, so the whole pipeline can be exercised with no network and no credentials.

Three things worth knowing.

The OData key is guessed. The Questions entity is filed under a document "Name", and which identifier that is differs by environment. questions() tries the docID, then the job_id, then the filename, and a 404 on the first is normal rather than a failure. In the live runs the docID 404s and the job_id succeeds.

COMPLETED is reported before the entity is readable. There is a deliberate ten-second wait between the last poll and the OData call. Without it the read races the write and 404s.

Certificate verification is on. Both original scripts passed verify=False. The real reason is narrower than that: the firm's TLS inspection presents a CA certificate whose Basic Constraints extension is not marked critical, and Python 3.13 began enforcing that by default. Clearing that one strict-X.509 flag keeps the hostname check and the signature chain — the parts that stop anyone reading the authorization key off the wire — and the live submits succeed with it. SPRING_BOARD_CA_BUNDLE and SPRING_BOARD_INSECURE_TLS exist as escape hatches and neither is needed.

Matching questions to blobs
The container is flat: about 1560 .pptx files, each named after the question it answers. The match is QN against the file name, fuzzy, minimum similarity 0.8 — Explain your company's structure(s) and physical locations scores 0.982 against Explain your companys structures and physical locations.

A blob belongs to one question, the one it scores highest against. Without that rule a popular answer deck attaches itself to half the RFP and the deck repeats it.

The algorithm was carried across unchanged rather than improved. That was checked, not assumed: both implementations were run over all 1560 real blobs for the same labels and produced identical blobs and identical scores. Its outputs are the ones the team has already reviewed, and a "better" matcher would quietly change which slides a deck gets.

Downloads are cached in data/qna_cache/ by blob name, so a second run does not re-fetch. That folder is gitignored: it holds firm content.

4. Adding the slides
This is the hard part, and it is worth saying why. engine/assemble.py opens by explaining that assembly is simple because every block comes from one library — the masters, layouts, theme and media are already in the package, so there is no closure to chase and no foreign master to graft. An answer deck is exactly the case that excludes: somebody else's .pptx, with its own master, its own theme and its own images.

So qna_merge.py does the closure-chasing: a slide needs its layout, a layout needs its master, a master needs its theme, and a master must be listed in sldMasterIdLst or the file will not open. Every part is renamed to a free name, every relationship rewritten, and [Content_Types].xml kept in step.

It is pure Python — zipfile and the helpers in engine/ooxml.py. No PowerPoint, no LibreOffice, no network. Twenty slides merge in about two seconds, and it would run unchanged in a Linux container.

Notes, for navigation
Every appended slide carries a speaker note reading Q: <the matched library question> — the right-hand column of the index, Springboard's QN, not the client's own wording. The slide came out of the curated library and answers the library's question; the client's phrasing of it differs from one RFP to the next for the same slide, and the approved sample deck does the same.

One blob often contributes several slides — in one run a single answer contributed twenty — and all of them get the same note, so a reader landing anywhere in an answer can see what it is answering.

assemble.py drops notes parts on purpose, so a built deck has no notes master at all. The merger borrows one from the blob rather than writing its own; a hand-written notes master was rejected by PowerPoint outright.

Deduplication, and its limit
Every blob brings a master, a theme and its images. Copied naively, a deck with ten answers carries ten near-identical corporate masters and the same logo ten times. Designs are therefore matched by a content hash over the whole group — master, its layouts, its theme, everything they reference — and copied once. Images are matched by content too.

Only ppt/media may be shared. A chart's chartStyle, chartColorStyle and embedded workbook are byte-identical between two charts almost always, and they are owned one-to-one. See §6.

The effect: in one real run, 24 appended slides added 0.1 MB to a 42.8 MB deck.

The index slide
qna_index.py draws a two-column table — the extracted RFP question on the left, the matched library question on the right — ten questions to a slide, continuing onto more slides as needed.

The slide hangs off a layout of the proposal library, so the title style, footer, logo and page number are the client's own. The table inside keeps the colours of the approved design: the purple heading #510DBC on its light purple band #E9DFFC, sampled from the library's own "Instructions – Navigation" slide. The split is deliberate — the slide should look like the client's deck, the table should look like the Q&A module wherever it appears.

There is no slide title by default; the two column headings already say what the slide is.

Linking each question to its answer
Every question that has an answer is a slide jump — <a:hlinkClick action="ppaction://hlinksldjump"> — straight to the slide its answer starts on. Questions with no answer carry no link, which is the same distinction the right-hand column already makes.

A jump is a relationship to a slide part, not a slide number, so the target must exist before the link can be written. append_decks therefore appends the answers first, builds the index with real relationships to them, and then moves the index entries ahead of the answers in sldIdLst. The parts stay where they are; only the running order changes.

That ordering is the whole reason this is done here rather than as a pass over the finished file. The destination of every link is already known exactly — append_decks computes it. A separate pass has to rediscover it by reading the notes back out and fuzzy-matching the table against them, which is guesswork about our own data, and one run had five questions all beginning "What are your proposed performance measurement processes around…". Doing it inline also avoids a second full rewrite of a 40 MB deck and a dependency on python-pptx, which nothing else in the engine uses.

The count of index slides is computed before they are drawn, because the answers are numbered from after them, and then checked against the real count. If they ever disagree the merge refuses: a wrong slide number is worse than an error.

The link colour is not ours. PowerPoint paints hyperlinked text with the theme's hlink slot and overrides whatever fill the run carries — an explicit <a:schemeClr val="tx2"/> was tried and still came out in the theme's colour, so the fill is now left off rather than written and ignored. In public_audit_template that slot is #00B8F5, which measures 2.29:1 against white: below WCAG AA for body text, and at 8.5pt these cells are body text. Only the underline is ours. If the firm wants readable links here, the change is to the template's theme, because changing it in code would change every link in the client's deck.

5. The job, and the API
Springboard takes minutes, so nothing waits in a request.

endpoint	what it is for
GET /api/qna	can this machine run a job, what is missing, and the function lines
GET /api/qna/options	the dropdown lists on their own
POST /api/qna/jobs	multipart: the RFP, the build token, function line, sub-serviceline
GET /api/qna/jobs/{id}	where one job has got to
GET /api/qna/jobs	every job this process knows about
stage is the phase — submitting, waiting, reading, matching, downloading, assembling — and detail is the sentence to put beside a progress bar. Springboard's own status_list feeds lines like poll 4 of 60 — SEARCHING.

Three guarantees, and why each exists
A finished job writes a new build; it never modifies the one it was given. The user is usually previewing the proposal while the Q&A runs, and rewriting that file underneath them would invalidate the images already rendered from it. And if the merge went wrong, the deck they built is still there.

The uploaded RFP is deleted when the job ends, success or failure, in a finally. It is the client's own document; data/ being gitignored is not the same as gone.

A job still marked running when the server restarts is failed at startup, with "the server restarted while this was running — submit the RFP again". The thread is gone; a progress bar that never moves is worse than an error.

Jobs run one at a time. Springboard is a shared service and a queue of concurrent RFPs is a good way to find its rate limit.

The merged build records no library pages
post_build writes a sidecar mapping each slide to a page of the library PDF, which is what makes the normal preview instant. A merged deck deliberately writes an empty page list, because its answer slides are in no library. Had the list been kept, the preview would have shown the proposal and silently omitted the Q&A. Instead the viewer falls through to a renderer that can draw the real file.

Locally that is PowerPoint: several seconds rather than a fifth of a second. In a deployment there is no PowerPoint, so it would be graph if configured and svg — the skeleton approximation — if not. The downloaded file is identical either way; only the picture on screen changes.

6. What went wrong, and what it cost
Three bugs in the merge produced a file that engine.validate passed, that python-pptx opened happily, and that PowerPoint refused with a generic error. Each was found only by opening the result through COM. They are recorded because the next person to touch OOXML will meet them again.

sldMasterId and sldLayoutId share one id space across the whole presentation. The proposal deck makes it plain: its two masters are 2147483648 and 2147483676, and the 27 layouts of the first fill 2147483649–2147483675 — one unbroken run through both elements. Numbering a new master from the highest master id lands on a layout id that already exists, and PowerPoint rejects the file with 0x80070570, "the file is corrupted", saying nothing about ids.

Content types must be copied from the source deck, not inferred. ooxml.infer_ctype only knows the presentation parts, because that is all assemble.py has ever written. Charts, chart styles, chart colours and embedded workbooks inferred to nothing and fell through to the generic application/xml default.

Only ppt/media may be deduplicated by content hash. Two charts have byte-identical chartStyle and chartColorStyle parts almost always, and a chart owns its sub-parts one-to-one. Hashing pointed the second chart at the first one's parts; PowerPoint rejected the deck with every relationship resolving and no part missing.

Two smaller ones: a never-copy list written ppt/notesSlides/ but compared against a lower-cased name, so blob notes were copied and then collided with the generated ones; and a hand-written notes master that PowerPoint would not open, now borrowed from the blob instead.

7. Measured, against the real services
A live run through the HTTP API, SAP_customized.docx against Audit and Assurance / Audit-Public, public_audit_template:

step	
build the proposal	3.4 s, 59 slides
submit	9 s
Springboard processing	150 s, six phases
questions extracted	16
matched to an answer deck	10
match and download	~68 s (lists 1560 blobs, fetches the uncached)
merge	2.5 s, +2 index and +16 answer slides
total	228 s
result	77 slides, 42.5 MB, opens in PowerPoint
Linking adds nothing measurable — it is part of drawing the index. On a 15-question run, all 9 links resolved to exactly the slide the merge report names, the 6 unanswered questions correctly carried none, and PowerPoint's own Hyperlink.SubAddress agreed on every destination.

An earlier run with a different answer set: 15 questions, 9 matched, 24 appended slides, and deduplication held the growth to 0.1 MB on a 42.8 MB deck.

The appended slides were compared against the same slides rendered from the blob on its own: identical, including a chart, photographs and the corporate font — the only difference being the slide number in the footer.

8. Configuration
Four variables in .env, at the project root, beside the GRAPH_* ones:

SPRING_BOARD_RFP_BACKEND_URL=
SPRING_BOARD_AUTHORIZATION_KEY=
AZURE_STORAGE_CONNECTION_STRING=
AZURE_BLOB_CONTAINER_NAME=

GET /api/qna reports which are missing by name, and the UI hides the whole panel when any are — a button that is going to answer 503 is worse than no button. Values are never returned by any endpoint and never logged.

qna_options.json holds the function lines and their sub-servicelines. It is re-read on every request, so editing it and reloading the page is the whole mechanism; the strings go to Springboard verbatim, so they must be its identifiers rather than display labels.

9. What is not done
qna_options.json holds one pair. Audit and Assurance / Audit-Public is the only combination run against Springboard for real. The rest of the taxonomy is not guessed at: a plausible wrong value is accepted and quietly returns the wrong answers, which is worse than a short list.
No offline checks. Nothing in tools/check_api.py covers any of this, and CLAUDE.md §8 asks for it. All four engine modules are built to be testable without the network — the seams exist and were used by hand — but the merge needs a small synthetic answer deck in fixtures/, and Springboard needs a tools/fake_springboard.py in the shape of tools/fake_graph.py. This is the largest thing outstanding.
Masters accumulate. Nine answer decks brought nine masters, because each blob is genuinely a different source deck. It costs nothing in file size and PowerPoint opens it, but the layout gallery shows them all. The hand-made sample got down to two.
data/builds/ still grows unboundedly, and each Q&A job now adds a second deck to it. The README already lists cleanup as not done; this makes it twice as pressing.