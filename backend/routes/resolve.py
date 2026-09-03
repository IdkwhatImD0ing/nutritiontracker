"""/api/resolve routes — tier-3 kick, edited-phrase re-resolution, question answers.

DESIGN.md §5, §3.4. Exposes an APIRouter; the integrator wires it into main.py.
POST /api/resolve runs the SAME cascade as staging (resolver.cascade: tier-1
pick(), tier-2 trigram, §7.3 portion resolution incl. spoken qty+unit), now
preceded by the single-item decompose micro-call (BACKLOG 24) so tier 2b works
for edited text too. The micro-call is skipped when the phrase already
exact-matches an alias (tier 1 resolves it locally; the model would be pure
latency), and any decompose failure degrades to tiers 1/2a, else tier 3.
"""

from __future__ import annotations

import json
import time
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
from resolver import cascade
from resolver.commit import KEEP_LOG_KEYS_SQL
from resolver.norm import norm
from routes import photo
from services import tier3
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
    row = await _item_row(body.log_id, body.item_index)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown log item")
    _require_live(row)
    claimed = await db.execute(
        f"""
        UPDATE item_resolutions
        SET tier3_attempts = tier3_attempts + 1, tier3_started_at = now()
        WHERE log_id = %s AND item_index = %s AND {_LIVE_SQL}
        """,
        (str(body.log_id), body.item_index),
    )
    if not claimed:  # removed/committed between the read and the write
        raise HTTPException(status_code=409, detail="item no longer live")
    return await _run_tier3(str(body.log_id), body.item_index)


# ---------------------------------------------------------------------------
# POST /api/resolve — re-resolve an edited/added phrase through the SAME
# cascade as staging (micro-decompose for tier 2b, then tiers 1–2,
# §3.2/§3.3/§7.3), else tier 3; writing the item row per §5 so edited rows are
# never telemetry ghosts.
# ---------------------------------------------------------------------------

_EXACT_ALIAS_SQL = "SELECT 1 AS hit FROM aliases WHERE alias_norm = %s LIMIT 1"


async def _micro_decompose(phrase: str) -> tuple[ParsedItem, set[str]] | None:
    """§5 / BACKLOG 24: the single-item decompose micro-call, so tier 2b works
    for edited/added text exactly as it does at staging. Same context assembly,
    prompt, and cache key as logs.py's call — hence the same cached prefix
    (§6.1) — with the edited phrase as the whole utterance. Any failure (LLM
    outage, empty parse) degrades to tiers 1/2a: /api/resolve keeps working
    without the model. Returns (parsed item, inlined index ids) or None."""
    try:
        import tracing
        from services.context import build_agent_context
        from services.decompose import decompose

        tracing.init_tracing()
        ctx = await build_agent_context()
        async with tracing.generation("decompose") as gen:
            parsed = await decompose(phrase, ctx)
            gen.update(input=phrase, output=parsed.model_dump(mode="json"))
        try:
            tracing.flush()  # serverless flush rule (§10)
        except Exception:
            pass
        if not parsed.items:
            return None
        return parsed.items[0], {index_id for index_id, _, _ in ctx.food_index}
    except Exception:
        return None


@router.post("/api/resolve", response_model=StagedItem)
async def resolve_item(body: ResolveRequest) -> StagedItem:
    t0 = time.perf_counter()
    prev = await _item_row(body.log_id, body.item_index)
    if prev is not None:
        _require_live(prev)  # a removed/committed row is never re-resolved
    prev_staged = _staged_dict(prev) if prev is not None else {}
    try:
        meal = Meal(prev_staged.get("meal") or "snack")
    except ValueError:
        meal = Meal.snack
    # spoken qty/unit: the request's, else whatever the original utterance carried
    spoken_qty = body.qty if body.qty is not None else prev_staged.get("spoken_qty")
    spoken_unit = body.unit if body.unit is not None else prev_staged.get("spoken_unit")

    # §5 (BACKLOG 24): the single-item decompose micro-call gives edited text
    # the same tier-2b library matching as staging. Skipped when the phrase
    # already exact-matches an alias — tier 1 resolves that locally.
    micro = None
    key = norm(body.phrase)
    if not key or await db.fetchone(_EXACT_ALIAS_SQL, (key,)) is None:
        micro = await _micro_decompose(body.phrase)

    index_ids: set[str] | None = None
    phrase = body.phrase
    if micro is not None:
        item0, index_ids = micro
        phrase = (item0.phrase or "").strip() or body.phrase
        # The edited text's own quantity words beat stale spoken values from
        # the pre-edit utterance; an explicit request qty/unit beats both.
        if body.qty is None and item0.qty is not None:
            spoken_qty = item0.qty
        if body.unit is None and item0.unit is not None:
            spoken_unit = item0.unit
        parsed = item0.model_copy(
            update={
                "phrase": phrase,
                "qty": float(spoken_qty) if spoken_qty is not None else None,
                "unit": spoken_unit,
            }
        )
    else:
        parsed = ParsedItem(
            phrase=phrase,
            phrase_kind="food",
            qty=float(spoken_qty) if spoken_qty is not None else None,
            unit=spoken_unit,
            state=None,
            brand=None,
            library_match=None,
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
    # (spoken qty AND unit, incl. the count-without-unit rule) as staging;
    # BACKLOG 24: `parsed` now carries the micro-call's library_match/state/
    # brand, validated against the inlined index ids (§3.3 2b).
    res = await cascade.resolve_item(parsed, body.item_index, meal, index_ids)
    if res.staged.status == "searching":
        return await _run_tier3(str(body.log_id), body.item_index)
    if res.staged.status != "resolved":
        # §3.1 guard surfaced by the micro-call (meal_ref → needs_input): a
        # meal-shaped phrase never enters food search — persist and return.
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
    if res.search_instead:
        # §3.3 rule 3 affordance (StagedItem wire field is an integrator seam)
        resolved_payload["search_instead"] = True
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


async def _find_counterpart(phrase: str, state: str) -> str | None:
    """§3.4a: ONE targeted search_generic for the counterpart entry. Returns
    the first hit whose adapter-parsed state matches the answer, else None.
    Uses fdc._parse_state — the same parser that stamps foods.state at cache
    time, so the match criterion cannot drift from what gets cached."""
    try:
        hits = await fdc.search_generic(f"{phrase} {state}")
    except Exception:
        return None  # search outage degrades to keeping the current food
    for h in hits:
        if fdc._parse_state(h.get("description", "")) == state:
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
        "SELECT state FROM foods WHERE id = %s", (staged["food"]["id"],)
    )
    food_state = _col(food_row, 0, "state") if food_row is not None else None

    if food_state != state:
        counterpart = await _find_counterpart(staged.get("phrase") or "", state)
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
        return await tier3.resolve_item_from_source(
            str(body.log_id),
            body.item_index,
            body.answer.source,
            body.answer.source_id,
            display_name=body.answer.label,
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
