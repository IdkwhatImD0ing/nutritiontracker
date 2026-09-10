# What 200 real phrases found

## The short version

I wrote 200 utterances in Bill's voice ([PHRASE_CORPUS.md](PHRASE_CORPUS.md)) and traced them
through the code. They exposed **25 distinct defects**. Every one was read in the source, and
every one was then handed to two verifiers whose job was to kill it. Fifteen more claims died
that way and are not listed here.

The defects cluster into four things that can happen to you:

| What happens to you | How many | Worst example |
|---|---|---|
| A wrong number reaches the staged card, mostly unbadged | 5 | a bare count of a food whose serving reads "1 cup, whole" multiplies the cup |
| Food or a day quietly disappears | 8 | a retried commit reports no errors, so a refused row is never retried |
| The app learns something wrong and keeps it | 5 | tapping a flavour chip renames the shared food row permanently |
| You cannot see or fix it | 7 | the diary date a log will land on appears in no API response and on no screen |

One pattern runs through almost all of them. The code meets an amount, a date, or a status it
has no way to represent, turns it into a plausible-looking wrong one, and then carries no field
anywhere that would let you notice. That is the same shape as the four defects in
[issue #21](https://github.com/IdkwhatImD0ing/nutritiontracker/issues/21).

**Status, 2026-09-09: most of these are now fixed.** See [the fix log](#what-was-fixed) at the
bottom for what landed, what was deliberately left, and what still needs your decision.

### How much to trust each finding

Read this before acting on any of it.

- Every defect below was verified by reading the source at the cited line. At the time this
  section was first written no test had been run; the suite was set up and run afterwards, during
  the fix pass, and its results are in the fix log at the bottom.
- Two of them I **executed** before any fix: the portion helpers were sliced out of `cascade.py`
  and run directly. Those are marked *executed*.
- Nothing was checked against live FDC or Open Food Facts. Where a defect's size depends on what
  a provider actually returns, it says so.
- No model was run either. Where a defect depends on what the model emits for a phrase, it says so.
- The frontend has **no test runner at all** — no jest, vitest, or playwright, and no `test`
  script in `frontend/package.json`. So "untested" for a UI bug means the whole surface, not a
  gap in one file.

---

## 1. A wrong number lands in the diary and looks fine

**1. A bare count multiplies a cup serving, at full confidence.** *executed*
[`backend/resolver/cascade.py:199`](../backend/resolver/cascade.py#L199)
Say "eight almonds" and the resolver looks for a serving that counts pieces. `_is_count_serving`
decides by asking whether any word in the description is not a measure word. For `"1 cup, whole"`
the word "whole" is not a measure word, so a cup is classified as a countable piece. I ran the
real function: it returns `True`, and `_pieces_per` returns 1.0, so eight of them stage as eight
cups with `verify_me` false — no badge, no question, confidence high. Every count fixture in
`test_resolver.py` is a clean piece serving ("1 large", "4 large", "1 scoop"), so nothing in the
suite has a descriptor word after a measure word.
*Assumption not checked:* that a real FDC record puts such a portion ahead of any true piece
portion. [`fdc.py:357`](../backend/sources/fdc.py#L357) builds descriptions as
`"{amount} {measureUnit} {modifier}"`, which is exactly how that string arises, but one live FDC
call for almonds would settle the magnitude. Nobody made it.

**2. Ounces of a drink become that many bottles.**
[`backend/sources/fdc.py:447`](../backend/sources/fdc.py#L447) →
[`backend/resolver/cascade.py:328`](../backend/resolver/cascade.py#L328)
For a drink measured in millilitres, every serving carries `grams: None` — the label serving and
the per-100 serving both. So when you say "ate ounces of water" or "eight ounces of fairlife",
the code converts to 226.8 g, finds no serving with a gram weight to map it onto, and falls
through to applying your number as a count of servings. Eight bottles, not eight ounces. The
adapter test proves the `grams: None` shape and then never feeds that food to the portion
resolver; every portion fixture carries a numeric gram weight.
*Correction to an earlier draft of this report:* that path already returns `verify_me = True`,
so the row is badged for confirmation rather than staged silently. The pre-filled number is
still wrong and still one tap from the diary, but it is not invisible. Finding 1 is the silent
one.

**3. A "1/2 cup" serving is read as a whole cup.** *executed*
[`backend/resolver/cascade.py:166`](../backend/resolver/cascade.py#L166)
The regex that reads how much a serving holds needs digits then a space. `"1/2 cup (55 g)"`
matches the "1" and then hits a slash, so the whole match fails and the divisor falls back to
1.0. I ran it: a quarter cup of that food stages as 13.75 g where 27.50 g is correct — exactly
half. A mixed number like `"1 1/2 cups"` fails the same way and runs the other direction.
`"0.5 cup"` works fine, so this only bites the fraction form — which is the form nutrition labels
use. FDC passes `householdServingFullText` through verbatim
([`fdc.py:417`](../backend/sources/fdc.py#L417)), so the string reaches the parser as written.
No serving fixture anywhere in the backend tests contains a fraction.

**4. A saved meal's portion is swapped for a different size at the same quantity.**
[`backend/resolver/cascade.py:706`](../backend/resolver/cascade.py#L706)
Since the 2026-09-09 change drops every serving missing a core macro, a saved meal can find its
stored serving gone. The code repoints to the food's new default serving but never rescales the
quantity, which was read three lines earlier. A meal saved as one 414 ml bottle logs as one 100 g
serving — about a quarter of it — while the Library still shows the bottle. It commits on one
tap, because the commit gate only blocks rows flagged as estimates. All four tier-0 fixtures in
`test_meals.py` carry all four macros, so this branch is never entered from a saved meal.

**5. Tier 0 is the only staging path with no committability gate.**
[`backend/resolver/cascade.py:654`](../backend/resolver/cascade.py#L654)
The tier-1, tier-2a and tier-2b queries all append the committable-food filter. The saved-meal
query does not. So a meal item pointing at a food with no loggable serving expands into a row
that looks resolved, shows no calories, and refuses at commit every time — and the broken-meal
guard never fires, because it only counts foods whose row is missing entirely.
*Narrower than it sounds:* every current path that writes meal items is gated elsewhere, so this
needs a meal saved before 2026-09-09 or a raw API call. The suite's only broken-meal fixture
deletes the food row, so "broken" in the tests never means present-but-unloggable.

---

## 2. Food or a day quietly disappears

**6. A retried commit reports no errors, so a refused row is never retried.**
[`backend/resolver/commit.py:315`](../backend/resolver/commit.py#L315)
If a commit response is lost — a timeout, a backgrounded phone — you tap Log again. The replay
branch returns a hardcoded empty errors list without reading a single row. The client builds its
retry set only from that list, so a row the server actually refused never re-enters the pending
queue and never reaches the diary. The normal path builds real errors; only the replay path
lies. Three tests replay and assert only that a replay happened.
*Repro is stale, the bug is not:* the phrase that would trigger it (a thin Oikos record) no
longer refuses, because BACKLOG 184/185 landed 2026-09-09. BACKLOG 187 documents the remaining
gap that still can.

**7. Only one log can hold pending stragglers.**
[`frontend/components/Today.tsx:1094`](../frontend/components/Today.tsx#L1094)
Pending items live in a single state slot and a single global localStorage key. Commit two
cold-start logs inside the roughly 90-second tier-3 window and the second overwrites the first,
remounts the component under a new key, and kills the first log's poll on unmount. That component
is the only caller of commit-item anywhere in the app and there is no server-side sweeper, so the
first log's late row is gone. Needs two overlapping commits, not one utterance.

**8. The 90-second poll stop throws away a good answer, and fires after your phone sleeps.**
[`frontend/components/StagingSheet.tsx:253`](../frontend/components/StagingSheet.tsx#L253)
At the hard stop the sheet flips every still-searching row to "not found" and returns before the
only server read in that effect — so it never asks the server what actually happened. It is also
the one branch in that effect that does not skip rows an inline resolve owns, so the real answer
lands seconds later and is discarded by the merge guard. The clock is wall-clock, captured once,
never restarted on foreground, so a phone asleep two minutes hits the stop on resume. The row
does visibly read "not found" before you commit, so it is not invisible — just wrong.

**9. Commit forwards only searching and refused rows, so unanswered ones vanish.**
[`frontend/components/StagingSheet.tsx:576`](../frontend/components/StagingSheet.tsx#L576)
"i had guest bar the chocolate chip won and a la kwa" leaves one row asking which flavour and one
not found. Commit is enabled, the payload carries all three rows, the server skips the two
non-resolved ones with no error entry, and the client's straggler filter forwards neither. The
sheet closes with two foods gone and nothing mounted to chase them. You did see the question chip
before tapping, so there was a signal — there is just no signal afterwards.

**10. One `when_day_offset` per log back-dates everything in it.**
[`backend/models.py:148`](../backend/models.py#L148)
The day offset lives on the log, not on the item. Say "i just had 8 protein shake and whatever was
left of the rice from last night" and the shake you drank ten minutes ago files to yesterday
alongside the rice. The same field means "saturday and sunday i had the same thing both days" can
only pick one day, and the other stays empty. The route tests use a helper that builds exactly one
item, so all they can assert is that the shift applies uniformly — which is the behaviour that is
wrong.
*Model behaviour unverified:* whether the model actually answers -1 for "rice from last night" was
not tested. The prompt gives it no carve-out for a provenance phrase.

**11. Adding an item after removing one refuses the whole commit.**
[`frontend/components/StagingSheet.tsx:411`](../frontend/components/StagingSheet.tsx#L411)
Remove the last row, reopen the sheet, then use "+ add item". The client picks the next index from
the rows it can see, and the server has already filtered the removed row out of that list — so it
reuses the freed index. The resolve call 409s, is swallowed as "not found", and commit then refuses
every row of the log. The backend test asserts the 409 is correct; nothing tests who supplies the
index.

**12. `log_estimate` drops components past eight and says nothing.**
[`backend/services/agent.py:548`](../backend/services/agent.py#L548)
"there was some kind of braised pork with like ate different sides and i just ate whatever" can
exceed the cap. The list is truncated silently, sitting directly below three validators that all
return proper errors. Its sibling `save_meal` builds a `dropped` list with a comment saying the
extras are "reported, never silently". The tool result is itemized, so the model is not fully
blind, and every estimate row needs explicit confirmation before it commits. `MAX_ESTIMATE_ITEMS`
has zero hits in the test suite.

**13. Re-resolving typed text keeps the first item and drops the rest.**
[`backend/routes/resolve.py:198`](../backend/routes/resolve.py#L198)
Type "chicken and rice" into the add-item or edit field and the re-decompose returns two items;
the code returns `items[0]` and discards the rest with no signal. Only the typed field reaches
this — spoken utterances go through the full staging decompose. Every fixture for it builds a
one-item log.

---

## 3. The app learns something wrong and keeps it

**14. The cooked/raw toggle swaps in an unrelated food, then commit learns it.**
[`backend/routes/resolve.py:397`](../backend/routes/resolve.py#L397)
Tapping "cooked" runs one search and takes the first hit whose description parses as cooked, with
no check that it is the same food. The design doc itself notes FDC surfaces breaded tenders above
roasted breast. The food name does visibly change in the sheet, so it is catchable — but because
the swap is recorded as a deliberate disambiguation, commit stamps the alias as an explicit
choice and that food becomes your default for 90 days. No test commits after a toggle to see
which alias won.

**15. Packaged foods get a cooked/raw question they should never get.**
[`backend/sources/fdc.py:607`](../backend/sources/fdc.py#L607)
The FDC adapter runs the state check on every record type, including Branded. A frozen orange
chicken entree gets a cooked/raw toggle. The Open Food Facts adapter deliberately does the
opposite two files over. Tapping that stray toggle feeds finding 14 and turns the boxed meal into
plain roasted chicken breast. The one adapter assertion on this flag is against a generic record
where it is correct, and no test compares the two adapters.

**16. A question chip's label becomes the food's permanent name.**
[`backend/services/tier3.py:1345`](../backend/services/tier3.py#L1345)
Tap "Mixed berry" on a flavour question and that two-word label is written into the shared foods
row as its display name. That is the text the duplicate-detection index scores against and the
name the diary renders, and the insert's conflict clause never rewrites it — so the yogurt is
called "Mixed berry" forever and the next mention of it fails to match. The one test that touches
this write uses a fresh id every time, so the conflict branch never runs.

**17. An un-evidenced web food stays in the shared table forever.**
[`backend/services/tier3.py:1512`](../backend/services/tier3.py#L1512)
The web lane writes the food row the moment the model calls `add_food`, and the evidence gate
that would delete an unverified one sits inside the "resolved" branch. There are four ways out
that skip it: turn-cap exhaustion, a null food, a needs-input answer, a not-found answer. The
McDonald's case in BACKLOG 42 is exactly that shape — three of four live runs did not resolve.
The row then survives with model-supplied macros, and is searchable, loggable and re-offered to
later runs. The gate's test fixture hardcodes a resolved result in all four of its scenarios.
BACKLOG 37 is struck as fixed, but its own text scopes the fix to the resolved path.

**18. The answer to "cooked or raw?" is dropped, so it is asked again.**
[`backend/services/tier3.py:1360`](../backend/services/tier3.py#L1360)
`resolve_item_from_source` (line 1360) and `resolve_item_from_food` (line 1406) both call
`_write_item_row` without passing the stored state answer, and the commit key-preservation list
does not carry it either. So the question comes back. The toggle's own counterpart-swap branch
repairs itself with a follow-up update, so this is not literally every path — but the id-bearing
answer and manual-search paths both lose it.

---

## 4. You cannot see or fix it

**19. Nothing tells you which day a log will land on.**
[`backend/routes/logs.py:1686`](../backend/routes/logs.py#L1686)
The resolved date is written into the staged JSON and nowhere else. No response model carries a
date — not `StagedItem`, not `LogResponse`, not `LogCreateResponse`. So a wrong day from finding
10 cannot be caught before commit. Every when-shift assertion in the suite reaches around the API
and reads the database directly, which is why the gap is invisible to the tests.

**20. `search_branded` ranks by how many query words appear in the brand.**
[`backend/sources/fdc.py:520`](../backend/sources/fdc.py#L520)
There is no stopword filter, so refining a query with a flavour word ranks the real brand down
rather than up. BACKLOG 9 is open, not struck. The one test queries "quest bar", where both words
are Quest's own brand tokens — the single case the heuristic structurally cannot fail — and its
second assertion is true by construction of the sort.
*Not checked against live data:* the specific ranking numbers in the original write-up.

**21. Saving a meal from a partly-resolved log replaces the stored item list.**
[`backend/resolver/commit.py:374`](../backend/resolver/commit.py#L374)
"save this as my usual lunch" while one item is still searching builds the meal from only the
items that committed, and the update replaces the stored list outright. The late item reaches the
diary but never joins the meal, and re-saving over an existing meal truncates it. The existing
test submits one of two rows deliberately and asserts the shrink as correct.

**22. One `logged_at` is stamped across a log that can hold two.**
[`backend/resolver/commit.py:452`](../backend/resolver/commit.py#L452)
The commit reads one `logged_at` with `LIMIT 1` and no ordering, then stamps it on every entry.
Reachable through the agent lane, where `log_food` and `log_estimate` can both fire in one turn
and each computes its own shift. Not reachable from a single decompose, where every row shares
one value — so this is narrower than it first looked.

**23. A when-shifted commit paints another day's totals onto Today.**
[`frontend/components/Today.tsx:1088`](../frontend/components/Today.tsx#L1088)
Commit returns totals for the shifted day and the handler sets them directly. The diary reload in
the same handler corrects it a round trip later, so this is a flash, not a persistent lie.

**24. The tier-3 candidate list offers sources the model cannot pick.**
[`backend/services/tier3.py:680`](../backend/services/tier3.py#L680)
Local candidates are read with no source filter, so rows written by the barcode lane (`off`) and
the photo lane (`menu`) are shown to the agent — but its output schema only accepts `fdc` and
`web`. The dedup instruction it is given cannot be followed for those rows. The test helper
inserts every fixture food as `fdc`, so the case never arises.

**25. A turn-cap failure loses the transcript and the billed call count.**
[`backend/services/tier3.py:1466`](../backend/services/tier3.py#L1466)
The transcript variable is only assigned on success, and the turn-cap handler never touches it.
So the runs that cost the most — ten turns of hosted web search — are the only ones with no
record of what happened. The one network-count assertion in the suite is on a successful run.

---

## The invariants worth testing

Grouped by the four failure patterns named in issue #21, plus a fifth these findings needed.

**Fixtures were all well-formed.**
- A serving list built by a real adapter, not by hand, must round-trip through `resolve_portion`
  without ever returning `verify_me = False` on a quantity it could not derive from that serving's
  own parsed numbers.
- Any food whose servings all carry `grams = None` must never accept a spoken mass amount as a
  serving count.
- A saved meal whose stored serving is dropped must either rescale the quantity or refuse.

**Table tests enumerated the implementation, not the domain.**
- `_is_count_serving` must be driven by descriptions the adapters actually emit
  (`"{amount} {measureUnit} {modifier}"`), not by hand-written clean ones.
- `_units_per` must be driven by fraction and mixed-number serving text, because that is what
  labels say.
- An utterance naming two different days must produce two days of entries or be refused — never
  one day silently.

**No test asserted what a model can see.**
- The `get_food` tool payload must contain per-serving macros, as the prompt promises.
- The candidate list handed to the tier-3 agent must contain only sources its output schema can
  express.
- `log_estimate` over its cap must report the dropped phrases in its result, the way `save_meal`
  does.

**Nothing tested the second run.**
- A commit replayed after a lost response must return the same errors list as the original.
- A row that was refused must still be retryable after a second, unrelated log is committed.
- An answered cooked/raw question must not be asked again on the same row.
- A web food written by a run that did not end "resolved" must not exist afterwards.

**New: nothing asserted what the user can see.** (Every one of these is currently untestable —
there is no frontend test runner.)
- The diary date a log will land on must appear in the API response before commit.
- A row that did not resolve must either block commit or be handed to the pending queue.
- The staging sheet's hard stop must read the server once before declaring anything not found.

## Where to start

One file: `backend/tests/test_portion_shapes.py`. Build serving lists with the real adapter
functions (`fdc._branded_servings`, `fdc._generic_servings`, `off.normalize`) instead of by hand,
then drive `resolve_portion` over them.

One assertion: **`verify_me` may be false only when the returned quantity was derived from the
chosen serving's own parsed numbers — its gram weight for a mass unit, its parsed leading amount
for a unit word, its piece count for a bare count — never from a fallback or from a divisor of 1.0
that a failed parse produced.**

That covers findings 1, 2, 3 and 4, runs with no database and no network, and would have caught
defects 1 and 2 from issue #21 before they shipped.

---

## What was fixed

Applied 2026-09-09. Verified by running the offline suite: **10 failed, 360 passed**, and those
ten are the documented pre-existing failures, unchanged. The five new tests in
`backend/tests/test_portion_shapes.py` pass, and four of them fail on the code as it was — a test
that cannot fail proves nothing, so that was checked by reverting and re-running.

**Fixed**

| # | What changed |
|---|---|
| 1 | A bare count only stages unverified when the serving really counts pieces (`_counts_pieces`). A cup with a preparation modifier now asks. |
| 3 | The leading-amount parser reads `1/2` and `1 1/2`. A quarter cup of a "1/2 cup (55 g)" serving stages 27.50 g, not 13.75 g. |
| 4 | A saved meal's quantity is rescaled by gram weight when its stored serving was dropped. |
| 5 | Tier 0 got the committability filter its three sibling queries carry. |
| 6 | A retried commit reconstructs the real refusals from `staged->>'commit_error'`. |
| 12 | `log_estimate` reports the components it dropped, the way `save_meal` already did. |
| 13 | Multi-food typed text no longer loses its tail silently. |
| 14 | The cooked/raw toggle refuses a counterpart that is not plausibly the same food. |
| 15 | Packaged FDC records no longer get a cooked/raw question. |
| 16 | An answer chip's label is no longer written as the shared food's permanent name. |
| 17 | The web-evidence gate covers every exit, not just the resolved one — and the chips it invalidates are filtered out with it. |
| 18 | An answered cooked/raw question survives the rewrite and is not asked again. |
| 19 | The diary date rides the API response (`LogResponse.logged_at`). |
| 20 | Brand ranking is yes/no, so refining a query no longer ranks the real brand down. |
| 21 | A late item joins the saved meal it belonged to. |
| 22 | Entries carry their own row's `logged_at`; the log-level fallback is ordered. |
| 24 | The tier-3 candidate list and the agent's output schema are derived from one another. |
| 25 | A turn-cap run keeps its transcript and its billed call count. |
| 7, 8, 9, 11, 23 | The client: a queue of pending logs instead of one slot; the poll stop reads the server before declaring anything lost and respects an in-flight resolve; every unlanded row is handed to the pending queue; "+ add item" cannot reuse a removed index; the tracker is repainted from the diary, not from a possibly-back-dated commit response. |

Finding 10's provenance half is fixed in the decompose prompt: "the rice from last night" says
when the rice was cooked, not when it was eaten, so it no longer back-dates the whole log.

**Deliberately not fixed — these need a decision**

1. **A day offset per item.** An utterance naming two days ("saturday and sunday, same thing
   both days") can still only land on one, because the offset lives on the log. Fixing it means
   the model resolves a date per food, which is a contract change and more model burden.
2. **`CommitResponse` carries no date.** Two consequences follow from it: a log whose rows
   legitimately span two days reports totals for one, and the Today tracker had to stop painting
   commit totals altogether rather than check the day. Giving the response the same `logged_at`
   the log response now has fixes both.
3. **Barcode foods are no longer offered to the tier-3 agent.** They were being shown to a model
   whose schema could not name them back. Hiding them is safe but loses dedup, so tier 3 can now
   create a second identity for a food you scanned. The better fix is to let the agent name them,
   which means teaching `_get_food_cached` to hydrate `off` rows.
4. **A saved meal with one unloggable food now stages nothing at all.** That is §3.1's stated
   intent and finding 5 asked for it, but there is no way to find an affected meal — a Library
   affordance would help.
5. **The counterpart relevance check may be too strict.** It requires every identity word to
   match, and FDC words the same food inconsistently. Failing closed is the safer direction, but
   confirming it needs live FDC calls this pass did not make.

**Still untested.** `_extend_saved_meal` and `_row_logged_at` have no tests. Neither does any
frontend change — `frontend/package.json` has no test runner at all, so all five client fixes are
reviewed-by-reading only.
