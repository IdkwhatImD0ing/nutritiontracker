"""/api/resolve routes — tier-3 kick, edited-phrase re-resolution, question answers.

DESIGN.md §5, §3.4. Exposes an APIRouter; the integrator wires it into main.py.
POST /api/resolve runs the SAME cascade as staging (resolver.cascade: tier-1
pick(), §7.3 portion resolution incl. spoken qty+unit), else tier 3 in the
same request. (A decompose micro-call used to run first for a library match,
BACKLOG 24; decompose was removed 2026-09-11, and tier 3 sees his diary foods.)
"""

from __future__ import annotations

import json
import re
import time
from fractions import Fraction
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import db
from auth import require_user
from models import (
    Meal,
    ParsedItem,
    ResolveRequest,
    StagedItem,
    Tier3KickRequest,
)
from resolver import cascade, foods_repo
from resolver.commit import KEEP_LOG_KEYS_SQL
from resolver.norm import norm
from routes import photo
from routes.logs import cascade_summary
from services import quota, tier3
from sources import fdc

router = APIRouter(dependencies=[Depends(require_user)])


async def _run_tier3(
    log_id: str, item_index: int, refinement: str | None = None
) -> StagedItem:
    """tier3.run_tier3, plus the §3.4b seam: a photo row (staged carries
    `vision`) that tier 3 could not resolve becomes the numberless
    source='estimate' row that routes the user to search; any other
    not_found stays the manual search box."""
    item = (
        await tier3.run_tier3(log_id, item_index)
        if refinement is None
        else await tier3.run_tier3(log_id, item_index, refinement=refinement)
    )
    if item.status == "not_found":
        estimate = await photo.stage_estimate_food(log_id, item_index)
        if estimate is not None:
            return estimate
    return item


# ---------------------------------------------------------------------------
# Request bodies not in models.py (route-local; §5 /api/resolve/answer)
# ---------------------------------------------------------------------------


class Answer(BaseModel):
    """A tapped id-bearing option (source + source_id, or a cached food_id,
    plus label) or free text."""

    label: str | None = None
    source: str | None = None
    source_id: str | None = None
    food_id: str | None = None  # alternates chip for an already-cached food (§7.1)
    text: str | None = None


class AnswerRequest(BaseModel):
    log_id: UUID
    item_index: int
    answer: Answer


def _col(row, idx: int, key: str):
    if isinstance(row, dict):
        return row.get(key)
    return row[idx]


async def _item_row(log_id: UUID, item_index: int):
    return await db.fetchone(
        "SELECT phrase, staged, status, committed_at FROM item_resolutions "
        "WHERE log_id = %s AND item_index = %s",
        (str(log_id), item_index),
    )


# State guard — the same one as tier3._write_item_row (BACKLOG 27): every
# write in this module is conditional on the row still being live, so a late
# /api/resolve, kick, or answer can never resurrect a removed row or clobber
# a committed one. Used inline in SQL and up front via _require_live (a 409
# before any model/network spend). Item rows never carry a 'committed'
# status — commit marks them with committed_at — hence the second clause.
_LIVE_STATUSES = ("searching", "needs_input", "not_found", "resolved")
_LIVE_SQL = (
    "status IN ('searching', 'needs_input', 'not_found', 'resolved') "
    "AND committed_at IS NULL"
)


def _require_live(row) -> None:
    if _col(row, 3, "committed_at") is not None:
        raise HTTPException(status_code=409, detail="item already committed")
    if _col(row, 2, "status") not in _LIVE_STATUSES:
        raise HTTPException(status_code=409, detail="item already removed")


def _staged_dict(row) -> dict:
    raw = _col(row, 1, "staged")
    if isinstance(raw, dict):
        return raw
    return json.loads(raw) if raw else {}


def _as_written(row, item: StagedItem) -> StagedItem:
    """§3.4b: the response is the row as stored — KEEP_LOG_KEYS_SQL carries a
    photo row's `estimated`/`estimate_note` over the fresh payload, and the
    sheet renders what comes back. `row` is a one-column `RETURNING staged`
    row (column 0, unlike _item_row's shape); a refused write returns the
    payload."""
    raw = _col(row, 0, "staged") if row is not None else None
    if isinstance(raw, (str, bytes)):
        raw = json.loads(raw)
    return StagedItem.model_validate(raw) if raw else item


# ---------------------------------------------------------------------------
# POST /api/resolve/tier3 — the client-side tier-3 kick (§3.4)
# ---------------------------------------------------------------------------


@router.post("/api/resolve/tier3", response_model=StagedItem)
async def kick_tier3(body: Tier3KickRequest) -> StagedItem:
    import tracing  # lazy: stubbed in route tests

    # §10: one trace per run, the log id as its session; the agent's turns
    # and tool calls nest under tier3's span
    async with tracing.run("resolve.tier3", session_id=str(body.log_id), input=body) as trace:
        item = await _kick_tier3(body)
        trace.set(output=item)
        return item


async def _kick_tier3(body: Tier3KickRequest) -> StagedItem:
    return await kick_row(body.log_id, body.item_index)


async def kick_row(log_id: UUID, item_index: int, *, claim: bool = True) -> StagedItem:
    """One tier-3 run for a staged row: the live check, the demo's capped
    spend, the claim, then _run_tier3 (with the §3.4b photo seam). The phone's
    kick and the chat's inline lookups (routes/logs._resolve_inline,
    2026-09-11) are the same run behind the same gates.

    `claim=False` is the inline path: it leaves tier3_attempts and
    tier3_started_at alone, so a row the request ran out of time for reaches
    the phone with both of the fallback's tries (and the 60 s stale clock)
    intact."""
    row = await _item_row(log_id, item_index)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown log item")
    _require_live(row)
    await quota.check_resolve()  # demo: capped model spend — after the free refusals
    if claim:
        claimed = await db.execute(
            f"""
            UPDATE item_resolutions
            SET tier3_attempts = tier3_attempts + 1, tier3_started_at = now()
            WHERE log_id = %s AND item_index = %s AND {_LIVE_SQL}
            """,
            (str(log_id), item_index),
        )
        if not claimed:  # removed/committed between the read and the write
            raise HTTPException(status_code=409, detail="item no longer live")
    return await _run_tier3(str(log_id), item_index)


# ---------------------------------------------------------------------------
# POST /api/resolve — re-resolve an edited/added phrase through the SAME
# cascade as staging (tier 1, §3.2/§7.3), else tier 3 — which reads his diary
# foods and never asks (§3.4); writing the item row per §5 so edited rows are
# never telemetry ghosts. Until 2026-09-11 a decompose micro-call ran first
# for a library match; decompose is gone, and tier 3 now sees which foods are
# his, so a typed edit of one of them is still found.
# ---------------------------------------------------------------------------

_EXACT_ALIAS_SQL = "SELECT 1 AS hit FROM aliases WHERE alias_norm = %s LIMIT 1"

# A typed amount at the front of the text — what the decompose micro-call used
# to split off (BACKLOG 24): "2 eggs", "30g whey", "1/2 cup rice", "two scoops
# of whey". A number glued to a word only when the word is a mass/volume unit,
# so "7up" and "2% milk" stay whole.
_NUMBER_WORDS = {
    "one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0, "five": 5.0,
    "six": 6.0, "seven": 7.0, "eight": 8.0, "nine": 9.0, "ten": 10.0, "half": 0.5,
}
_TYPED_AMOUNT_RE = re.compile(
    r"^\s*(\d+/\d+|\d+(?:\.\d+)?)(?:\s+|(?=(?:g|kg|oz|lb|lbs|ml)\b))(.+)$", re.IGNORECASE
)
_TYPED_UNITS = frozenset({
    "g", "gram", "grams", "kg", "oz", "ounce", "ounces", "lb", "lbs", "pound", "pounds",
    "ml", "cup", "cups", "tbsp", "tablespoon", "tablespoons", "tsp", "teaspoon", "teaspoons",
    "scoop", "scoops", "bowl", "bowls", "slice", "slices", "piece", "pieces", "bottle",
    "bottles", "can", "cans", "bar", "bars", "glass", "glasses", "serving", "servings",
})


def _typed_amount(text: str) -> tuple[float, str | None, str] | None:
    """(qty, unit, the rest) for text that starts with an amount, else None."""
    m = _TYPED_AMOUNT_RE.match(text)
    if m is not None:
        try:
            qty = float(Fraction(m.group(1)))
        except (ValueError, ZeroDivisionError):
            return None
        rest = m.group(2)
    else:
        first, _, rest = text.strip().partition(" ")
        qty = _NUMBER_WORDS.get(first.lower())
        if qty is None:
            return None
    words = rest.split()
    unit = None
    if len(words) > 1 and words[0].lower() in _TYPED_UNITS:
        unit, words = words[0].lower(), words[1:]
        if len(words) > 1 and words[0].lower() == "of":
            words = words[1:]
    if qty <= 0 or not words:
        return None
    return qty, unit, " ".join(words)


@router.post("/api/resolve", response_model=StagedItem)
async def resolve_item(body: ResolveRequest) -> StagedItem:
    import tracing  # lazy: stubbed in route tests

    # §10: one trace per run (nothing is sent when no model ran — an alias hit)
    async with tracing.run("resolve", session_id=str(body.log_id), input=body) as trace:
        item = await _resolve(body)
        trace.set(output=item)
        return item


async def _resolve(body: ResolveRequest) -> StagedItem:
    import tracing

    t0 = time.perf_counter()
    prev = await _item_row(body.log_id, body.item_index)
    if prev is not None:
        _require_live(prev)  # a removed/committed row is never re-resolved
    elif await db.fetchone("SELECT 1 AS hit FROM logs WHERE id = %s", (str(body.log_id),)) is None:
        # an added phrase hangs on a log of this tenant's: an unknown or
        # foreign log (invisible under RLS) is 404 here, before any model
        # call — not a tenant-FK failure after tier 3's
        raise HTTPException(status_code=404, detail="log not found")
    await quota.check_resolve()  # demo: capped model spend — after the free refusals
    prev_staged = _staged_dict(prev) if prev is not None else {}
    try:
        meal = Meal(prev_staged.get("meal") or "snack")
    except ValueError:
        meal = Meal.snack
    # spoken qty/unit: the request's, else whatever the original utterance carried
    spoken_qty = body.qty if body.qty is not None else prev_staged.get("spoken_qty")
    spoken_unit = body.unit if body.unit is not None else prev_staged.get("spoken_unit")

    phrase = body.phrase
    # the sheet sends the text as typed: an amount at its front ("2 eggs")
    # comes off the phrase and beats the pre-edit utterance's; an explicit
    # request qty/unit beats both. Not for a phrase he has said as a whole.
    typed = _typed_amount(phrase)
    if typed is not None and await db.fetchone(_EXACT_ALIAS_SQL, (norm(phrase),)) is None:
        typed_qty, typed_unit, phrase = typed
        if body.qty is None:
            spoken_qty = typed_qty
            spoken_unit = body.unit if body.unit is not None else typed_unit
    # A food of his typed by its name — the edit's analogue of the chat
    # agent's food_id (§3.3): matched exactly (norm), never fuzzily, against
    # the foods his diary holds, so the row is that food with no search.
    key = norm(phrase)
    mine = next(
        (
            r for r in await foods_repo.recent_foods()
            if key and key in {
                norm(r["display_name"] or ""),
                norm(f"{r['brand'] or ''} {r['display_name'] or ''}"),
            }
        ),
        None,
    )
    parsed = ParsedItem(
        phrase=phrase,
        phrase_kind="food",
        qty=float(spoken_qty) if spoken_qty is not None else None,
        unit=spoken_unit,
        state=None,
        brand=None,
        library_match=mine["id"] if mine else None,
        meal_ref=None,
    )

    searching = StagedItem(
        item_index=body.item_index,
        phrase=phrase,
        status="searching",
        qty=spoken_qty,
        meal=meal,
    )
    staged_payload = searching.model_dump(mode="json", exclude_none=True)
    staged_payload["spoken_qty"] = spoken_qty
    staged_payload["spoken_unit"] = spoken_unit
    # §3.5/§3.2: on an existing row, the ORIGINAL phrase/phrase_norm and the
    # original proposed_* are preserved — commit learns the alias for what was
    # actually spoken, and `explicit` compares the final food against what the
    # system first proposed. The new text lives in the staged payload.
    # The conflict branch carries the live guard (_LIVE_SQL, table-qualified
    # because EXCLUDED shares the column names): a row removed or committed
    # since the pre-check above refuses the update (rowcount 0) — never a
    # resurrection via 'searching'.
    # §3.1 invariant: a row that was a tier-0 expansion stays tier 0 in the
    # column through every edit — its phrase column is a food display name,
    # not something spoken, so commit must never learn an alias for it.
    written = await db.execute(
        f"""
        INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm, status, staged)
        VALUES (%s, %s, %s, %s, 'searching', %s::jsonb)
        ON CONFLICT (log_id, item_index) DO UPDATE
        SET status = 'searching',
            staged = EXCLUDED.staged{KEEP_LOG_KEYS_SQL},
            tier = CASE WHEN item_resolutions.tier = 0 THEN 0 ELSE NULL END,
            was_edited = TRUE
        WHERE item_resolutions.status IN ('searching', 'needs_input', 'not_found', 'resolved')
          AND item_resolutions.committed_at IS NULL
        """,
        (
            str(body.log_id),
            body.item_index,
            phrase,
            norm(phrase),
            json.dumps(staged_payload),
        ),
    )
    if not written:
        raise HTTPException(status_code=409, detail="item no longer live")

    # BACKLOG 6: the one cascade — same pick(), same §7.3 portion resolution
    # (spoken qty AND unit, incl. the count-without-unit rule) as staging
    async with tracing.run("cascade", kind="retriever", input=[parsed.phrase]) as lookup:
        res = await cascade.resolve_item(
            parsed, body.item_index, meal, {mine["id"]} if mine else None
        )
        lookup.set(output=cascade_summary([res]))
    if res.staged.status == "searching":
        return await _run_tier3(str(body.log_id), body.item_index)
    if res.staged.status != "resolved":
        # a §3.1 tier-0 guard (a saved meal that cannot fit one row, or is
        # missing foods): a meal-shaped phrase never enters food search —
        # persist and return.
        stored = await db.fetchone(
            f"""
            UPDATE item_resolutions SET status = %s, staged = %s::jsonb{KEEP_LOG_KEYS_SQL}
            WHERE log_id = %s AND item_index = %s AND {_LIVE_SQL}
            RETURNING staged
            """,
            (
                res.staged.status,
                json.dumps(res.staged.model_dump(mode="json", exclude_none=True)),
                str(body.log_id),
                body.item_index,
            ),
        )
        return _as_written(stored, res.staged)

    item = res.staged
    resolved_payload = item.model_dump(mode="json", exclude_none=True)
    if spoken_qty is not None or spoken_unit is not None:
        # keep the spoken qty/unit alive for later edits (§7.3 rule 1)
        resolved_payload["spoken_qty"] = spoken_qty
        resolved_payload["spoken_unit"] = spoken_unit
    if res.state_answer:
        # §3.4a seam: commit persists this to aliases.state_answer (W2)
        resolved_payload["state_answer"] = res.state_answer
    # proposed_* keep the FIRST proposal (COALESCE): a re-resolution after an
    # edit must not erase the baseline that commit's explicit-choice and
    # rejected_count semantics compare against (§3.5).
    stored = await db.fetchone(
        f"""
        UPDATE item_resolutions
        SET status = 'resolved', staged = %s::jsonb{KEEP_LOG_KEYS_SQL},
            tier = CASE WHEN item_resolutions.tier = 0 THEN 0 ELSE %s END,
            resolve_ms = %s,
            proposed_food_id = coalesce(proposed_food_id, %s),
            proposed_serving_id = coalesce(proposed_serving_id, %s),
            proposed_qty = coalesce(proposed_qty, %s),
            alias_id = %s
        WHERE log_id = %s AND item_index = %s AND {_LIVE_SQL}
        RETURNING staged
        """,
        (
            json.dumps(resolved_payload),
            item.tier,
            int((time.perf_counter() - t0) * 1000),
            item.food.id if item.food else None,
            item.serving.id if item.serving else None,
            item.qty,
            res.alias_id,
            str(body.log_id),
            body.item_index,
        ),
    )
    return _as_written(stored, item)


# ---------------------------------------------------------------------------
# §3.4a cooked/raw toggle — resolved locally, NEVER through a tier-3 re-run
# ---------------------------------------------------------------------------

_STATE_WORDS = ("cooked", "raw", "dry")


# How much of the row's food must survive in a hit for it to be the same food
# wearing a different state. Below 1.0 on purpose: see _plausible_counterpart.
_COUNTERPART_OVERLAP = 0.6


def _identity_tokens_ordered(text: str) -> list[str]:
    """The words that say WHICH food this is, in the order they were written:
    norm()'s tokens (§3.0), minus the cooked/raw/dry ones — the words a
    counterpart is *supposed* to differ in. fdc._parse_state does the dropping,
    so the list cannot drift from the one that stamps foods.state. norm()
    deliberately leaves plural/singular variance alone (§3.0), so cascade's
    stemmer stands in for it on both sides. Order is kept because the first of them is
    the head noun, which carries more weight than the rest."""
    return [
        cascade._unit_stem(t)
        for t in norm(text).split()
        if fdc._parse_state(t) is None
    ]


def _identity_tokens(text: str) -> set[str]:
    return set(_identity_tokens_ordered(text))


def _plausible_counterpart(food_name: str, description: str) -> bool:
    """Is this hit the same food as the one already on the row, differing only
    in its state?

    The toggle fires one search for "{phrase} {state}" and FDC's ranking is
    quirky — §3.4 rule 4 records that "chicken breast cooked" surfaces breaded
    tenders above plain roasted breast. Taking the first state-matching hit
    therefore swapped the row onto a *different food*, and because the swap is
    recorded as a deliberate disambiguation, commit stamped that food as an
    explicit choice: the phrase's default for the next 90 days.

    So a hit has to look like the same food — but "every identity word" is too
    strict to be that test. FDC words one food several ways across its own
    records ("broilers or fryers" beside "broiler or fryers", "skinless,
    boneless" present on one and absent on the other), so demanding a full
    subset fails on phrasing rather than on identity, and the toggle then does
    nothing at all: the user taps "raw" and the row does not move. That is a
    quieter failure than the wrong swap, not a better one.

    Two things instead, both about identity rather than wording: the HEAD noun
    — the first identity word, the thing the food actually is — must appear,
    and most of the rest must too. "chicken, breaded tenders" keeps the head
    but loses "breast", so it still fails; a differently-worded roasted breast
    keeps both and passes.
    """
    wanted = list(_identity_tokens_ordered(food_name))
    if not wanted:
        return False
    got = _identity_tokens(description)
    if wanted[0] not in got:
        return False
    overlap = len(set(wanted) & got) / len(set(wanted))
    return overlap >= _COUNTERPART_OVERLAP


async def _find_counterpart(phrase: str, state: str, food_name: str) -> str | None:
    """§3.4a: ONE targeted search_generic for the counterpart entry. Returns
    the first hit whose adapter-parsed state matches the answer AND which is
    plausibly the same food as the row's current one, else None.
    Uses fdc._parse_state — the same parser that stamps foods.state at cache
    time, so the match criterion cannot drift from what gets cached."""
    try:
        hits = await fdc.search_generic(f"{phrase} {state}")
    except Exception:
        return None  # search outage degrades to keeping the current food
    for h in hits:
        description = h.get("description", "")
        if fdc._parse_state(description) == state and _plausible_counterpart(
            food_name, description
        ):
            return str(h["fdc_id"])
    return None


async def _answer_state_toggle(
    log_id: UUID, item_index: int, staged: dict, state: str
) -> StagedItem | None:
    """Handle a cooked/raw/dry toggle answer on an already-RESOLVED row (§3.4a,
    §7.1): if the chosen food's state already matches (or no counterpart can be
    found), persist the answer into the staged jsonb — zero network; otherwise
    fire one targeted search_generic for the counterpart entry and resolve it
    with a single get_food. Never a run_tier3 re-entry, never a status flip to
    'searching', never a tier3_attempts bump. The swap is recorded as a
    NON-switch (proposed_food_id follows the counterpart) so commit's
    switched-detection doesn't land rejected_count on the user's own alias for
    a one-tap clarification (§3.2/§3.5).

    Returns None when the row isn't a resolved food — a free-text answer to a
    tier-3 question falls through to the normal re-kick path.
    """
    if staged.get("status") != "resolved" or not (staged.get("food") or {}).get("id"):
        return None
    food_row = await db.fetchone(
        "SELECT state, display_name FROM foods WHERE id = %s", (staged["food"]["id"],)
    )
    food_state = _col(food_row, 0, "state") if food_row is not None else None
    # the row's own food is what a counterpart has to be a counterpart OF: the
    # stored display_name ("chicken breast, roasted") is the name the adapters
    # condense from the same catalog the search reads, so it is the comparable
    # side of _plausible_counterpart. A food row deleted under us leaves no
    # identity words, and no swap can be justified.
    food_name = _col(food_row, 1, "display_name") if food_row is not None else None

    if food_state != state:
        counterpart = await _find_counterpart(
            staged.get("phrase") or "", state, food_name or ""
        )
        if counterpart is not None:
            item = await tier3.resolve_item_from_source(
                str(log_id), item_index, "fdc", counterpart
            )
            if item.status == "resolved" and item.food is not None:
                item = item.model_copy(update={"state_question": None})
                await db.execute(
                    f"""
                    UPDATE item_resolutions
                    SET staged = (staged - 'state_question')
                                 || jsonb_build_object('state_answer', %s::text),
                        proposed_food_id = %s
                    WHERE log_id = %s AND item_index = %s AND {_LIVE_SQL}
                    """,
                    (state, item.food.id, str(log_id), item_index),
                )
                return item
            # counterpart id 404ed mid-flight (stale index, §3.4) — fall
            # through and restore the original resolved row with the answer

    # matching state, no counterpart found, or counterpart fetch failed:
    # keep the current food, persist the answer, clear the question.
    staged = dict(staged)
    staged["state_answer"] = state
    staged.pop("state_question", None)
    await db.execute(
        f"""
        UPDATE item_resolutions
        SET status = 'resolved', staged = %s::jsonb{KEEP_LOG_KEYS_SQL}
        WHERE log_id = %s AND item_index = %s AND {_LIVE_SQL}
        """,
        (json.dumps(staged), str(log_id), item_index),
    )
    return StagedItem.model_validate(staged)


# ---------------------------------------------------------------------------
# POST /api/resolve/answer — tier-3 question response (§5)
# ---------------------------------------------------------------------------


@router.post("/api/resolve/answer", response_model=StagedItem)
async def answer_question(body: AnswerRequest) -> StagedItem:
    import tracing  # lazy: stubbed in route tests

    # §10: one trace per run — only the free-text re-kick runs a model, so
    # an id-bearing or cooked/raw answer records nothing
    async with tracing.run("resolve.answer", session_id=str(body.log_id), input=body) as trace:
        item = await _answer(body)
        trace.set(output=item)
        return item


async def _answer(body: AnswerRequest) -> StagedItem:
    row = await _item_row(body.log_id, body.item_index)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown log item")
    _require_live(row)

    if body.answer.food_id:
        # Already-cached food (alternates chip): one local read, no network.
        # R1: never the photo lane's numberless placeholder (source='estimate').
        src = await db.fetchone(
            "SELECT source FROM foods WHERE id = %s", (body.answer.food_id,)
        )
        if src is not None and _col(src, 0, "source") == "estimate":
            raise HTTPException(status_code=422, detail=photo.ESTIMATE_PICK_REFUSAL)
        try:
            return await tier3.resolve_item_from_food(
                str(body.log_id), body.item_index, body.answer.food_id
            )
        except LookupError:
            raise HTTPException(status_code=404, detail="unknown food")

    if body.answer.source and body.answer.source_id:
        # Id-bearing option: one get_food + persist, no second agent run.
        # The chip's label is NOT passed as display_name: it is the text of one
        # answer ("Mixed berry"), not a name for a food every account reads back.
        # persist_food's ON CONFLICT never rewrites display_name, so whatever
        # lands on the insert is the row's name for life — the diary's, and the
        # text the duplicate-detection trigram scores — and the next mention of
        # that yogurt would fail to match it. The adapter's own name is right
        # here; a caller with an authoritative label (the Library pick,
        # routes/foods.select_food) still passes one.
        return await tier3.resolve_item_from_source(
            str(body.log_id),
            body.item_index,
            body.answer.source,
            body.answer.source_id,
        )

    text = (body.answer.text or body.answer.label or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="answer needs ids or text")
    # §3.4a: a cooked/raw/dry toggle on a resolved row resolves locally (at
    # most one targeted search_generic) — never a full tier-3 re-run.
    if text.lower() in _STATE_WORDS:
        toggled = await _answer_state_toggle(
            body.log_id, body.item_index, _staged_dict(row), text.lower()
        )
        if toggled is not None:
            return toggled
    # Free text: one re-kick — a second agent run carrying the refinement.
    await quota.check_resolve()  # demo: capped model spend (the local paths above are free)
    claimed = await db.execute(
        f"""
        UPDATE item_resolutions
        SET tier3_attempts = tier3_attempts + 1, tier3_started_at = now(),
            status = 'searching'
        WHERE log_id = %s AND item_index = %s AND {_LIVE_SQL}
        """,
        (str(body.log_id), body.item_index),
    )
    if not claimed:  # removed/committed between the read and the write
        raise HTTPException(status_code=409, detail="item no longer live")
    return await _run_tier3(str(body.log_id), body.item_index, refinement=text)
