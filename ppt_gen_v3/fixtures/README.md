# The fixture exercise

A 15-slide master library, three payloads, and the deck each payload should
produce. Enough to walk the whole tool without touching a confidential file.

```
master.pptx             the library to upload
payloads/*.json         the answers
targets/*.pptx          the deck each payload should build
reference_rules.json    the answer key — try without it first
```

## What to do

1. **Upload `master.pptx`** as a library. Give it a name; that name becomes the
   folder under `templates/` and the id every rule refers to.
2. **Inspect it.** 15 slides, every id from a `{{block:…}}` marker, 10
   placeholders. Nothing should be named by its title.
3. **Author the rules.** Keep the library order — it is already right — and give
   the seven conditional slides a condition. Each one says on the slide what it
   is for, and the payload field names match.
4. **Build** with a payload, then **compare** against the target of the same
   name. The goal is `MATCH`.

Upload all three payloads on the **Questions** screen first, so the build form
and condition dropdowns are populated: one payload cannot tell you which fields
vary, and a field that never varies is not a usable condition.

## Why three payloads

Every conditional slide is in at least one and absent from at least one. With a
single payload you cannot tell a conditional slide from a permanent one — the
rule is not discoverable, only guessable.

| | p1_new_client | p2_expansion | p3_minimal |
|---|---|---|---|
| slides | 12 | 13 | 9 |
| `AuditType` | New Audit Client | Expansion of Services | New Audit Client |
| `Transition_lab` | false | true | false |
| `Quality` | true | false | false |
| `Industry` | Technology | Healthcare | *(blank)* |
| `City` | Chicago | Boston | *(blank)* |

`p3_minimal` leaves `Industry` and `City` blank on purpose: it is the case that
distinguishes a condition on `exists` from one on a value.

## What this does and does not prove

It exercises rule authoring, ordering, placeholder binding, the build, and the
compare screen — end to end, through the same API the browser uses.

It does **not** prove fidelity to Templafy. The targets are built by this
engine, so a bug shared by both sides would cancel out. Only the 70 real decks
can answer that, and `engine/verify.py` is still how you ask.

## Regenerating

```
python tools/make_test_fixtures.py     # rebuild master, payloads and targets
python tools/check_fixtures.py         # walk the whole exercise via the API
```

Shape `creationId`s are derived from the block and shape ids rather than
generated fresh, so regenerating does not invent new identities and quietly
invalidate a target somebody already built against.

`check_fixtures.py` exists because a fixture set that cannot reach `MATCH`
wastes an afternoon and looks like a bug in the tool. It also asserts that
removing a rule is *reported and named correctly* — which is how the alignment
bug in `verify.align()` was found: a weak structural match was claiming a slide
that a later one matched exactly, so the right verdict named the wrong slide.
