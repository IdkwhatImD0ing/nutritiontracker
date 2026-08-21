"""/api/resolve routes — tier-3 kick, edited-phrase re-resolution, question answers.

DESIGN.md §5, §3.4. Exposes an APIRouter; the integrator wires it into main.py.
M0 scope for POST /api/resolve: re-run tier 1 (exact alias match) or tier 3 for an
edited phrase — the single-item decompose micro-call (tier 2b parity) is a later pass.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import db
from auth import require_user
from models import (
    Alternate,
    FoodRef,
    Macros,
    ResolveRequest,
    ServingRef,
    StagedItem,
    Tier3KickRequest,
)
from services import tier3

router = APIRouter(dependencies=[Depends(require_user)])


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


# ---------------------------------------------------------------------------
# norm() — §3.0 (local copy; the resolver workstream owns the canonical one)
# ---------------------------------------------------------------------------

_ARTICLES = {"a", "an", "the", "my", "some"}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    s = re.sub(r"[^\w\s]", " ", s)
    words = s.split()
    while words and words[0] in _ARTICLES:
        words.pop(0)
    return " ".join(words)


def _col(row, idx: int, key: str):
    if isinstance(row, dict):
        return row.get(key)
    return row[idx]


async def _item_row(log_id: UUID, item_index: int):
    return await db.fetchone(
        "SELECT phrase, staged FROM item_resolutions WHERE log_id = %s AND item_index = %s",
        (str(log_id), item_index),
    )


def _staged_dict(row) -> dict:
    raw = _col(row, 1, "staged")
    if isinstance(raw, dict):
        return raw
    return json.loads(raw) if raw else {}


# ---------------------------------------------------------------------------
# POST /api/resolve/tier3 — the client-side tier-3 kick (§3.4)
# ---------------------------------------------------------------------------


@router.post("/api/resolve/tier3", response_model=StagedItem)
async def kick_tier3(body: Tier3KickRequest) -> StagedItem:
    row = await _item_row(body.log_id, body.item_index)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown log item")
    await db.execute(
        """
        UPDATE item_resolutions
        SET tier3_attempts = tier3_attempts + 1, tier3_started_at = now()
        WHERE log_id = %s AND item_index = %s
        """,
        (str(body.log_id), body.item_index),
    )
    return await tier3.run_tier3(str(body.log_id), body.item_index)


# ---------------------------------------------------------------------------
# POST /api/resolve — re-resolve an edited phrase (M0: tier 1 or tier 3),
# writing the item row per §5 so edited rows are never telemetry ghosts.
# ---------------------------------------------------------------------------


@router.post("/api/resolve", response_model=StagedItem)
async def resolve_item(body: ResolveRequest) -> StagedItem:
    t0 = time.perf_counter()
    key = _norm(body.phrase)
    prev = await _item_row(body.log_id, body.item_index)
    prev_staged = _staged_dict(prev) if prev is not None else {}
    meal = prev_staged.get("meal") or "snack"
    # spoken qty/unit: the request's, else whatever the original utterance carried
    spoken_qty = body.qty if body.qty is not None else prev_staged.get("spoken_qty")
    spoken_unit = body.unit if body.unit is not None else prev_staged.get("spoken_unit")

    searching = StagedItem(
        item_index=body.item_index,
        phrase=body.phrase,
        status="searching",
        qty=spoken_qty,
        meal=meal,  # type: ignore[arg-type]
    )
    staged_payload = searching.model_dump(mode="json", exclude_none=True)
    staged_payload["spoken_qty"] = spoken_qty
    staged_payload["spoken_unit"] = spoken_unit
    # §3.5/§3.2: on an existing row, the ORIGINAL phrase/phrase_norm and the
    # original proposed_* are preserved — commit learns the alias for what was
    # actually spoken, and `explicit` compares the final food against what the
    # system first proposed. The new text lives in the staged payload.
    await db.execute(
        """
        INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm, status, staged)
        VALUES (%s, %s, %s, %s, 'searching', %s::jsonb)
        ON CONFLICT (log_id, item_index) DO UPDATE
        SET status = 'searching',
            staged = EXCLUDED.staged,
            tier = NULL,
            was_edited = TRUE
        """,
        (
            str(body.log_id),
            body.item_index,
            body.phrase,
            key,
            json.dumps(staged_payload),
        ),
    )

    item = await _try_tier1(body, key, meal, spoken_qty, t0)
    if item is not None:
        return item
    return await tier3.run_tier3(str(body.log_id), body.item_index)


async def _try_tier1(
    body: ResolveRequest, key: str, meal: str, spoken_qty: float | None, t0: float
) -> StagedItem | None:
    """Exact alias match (§3.2). M0 pick(): pinned → recent explicit choice →
    use_count/recency (the mode-of-last-10 refinement is the resolver workstream's)."""
    rows = await db.fetchall(
        """
        SELECT a.id, a.food_id, a.default_serving_id, a.default_qty,
               f.display_name, f.brand, f.servings, f.default_serving_id
        FROM aliases a JOIN foods f ON f.id = a.food_id
        WHERE a.alias_norm = %s
        ORDER BY a.pinned DESC,
                 (a.last_explicit_at IS NOT NULL
                  AND a.last_explicit_at > now() - interval '90 days') DESC,
                 a.last_explicit_at DESC NULLS LAST,
                 a.use_count DESC, a.last_used_at DESC
        """,
        (key,),
    )
    if not rows:
        return None

    top = rows[0]
    alias_id = str(_col(top, 0, "id"))
    food_id = str(_col(top, 1, "food_id"))
    servings_raw = _col(top, 6, "servings")
    servings = servings_raw if isinstance(servings_raw, list) else json.loads(servings_raw)
    serving_id = _col(top, 2, "default_serving_id") or _col(top, 7, "default_serving_id")
    serving = next((s for s in servings if s.get("serving_id") == serving_id), None)
    if serving is None:  # dangling id never renders (§4 invariant)
        serving = next(
            (s for s in servings if s.get("serving_id") == _col(top, 7, "default_serving_id")),
            servings[0] if servings else None,
        )
    if serving is None:
        return None
    # §7.3: spoken quantity wins, else the alias default, else 1
    if spoken_qty is not None:
        final_qty = float(spoken_qty)
    else:
        final_qty = float(_col(top, 3, "default_qty") or 1)
    raw_macros = serving.get("macros") or {}
    macros = (
        Macros(**{k: round(float(v) * final_qty, 2) for k, v in raw_macros.items()})
        if {"kcal", "protein_g", "carb_g", "fat_g"} <= set(raw_macros)
        else None
    )
    alternates = [
        Alternate(
            food_id=str(_col(r, 1, "food_id")),
            label=(_col(r, 4, "display_name") or "")
            + (f" ({_col(r, 5, 'brand')})" if _col(r, 5, "brand") else ""),
        )
        for r in rows[1:4]
    ] or None

    item = StagedItem(
        item_index=body.item_index,
        phrase=body.phrase,
        status="resolved",
        tier=1,
        food=FoodRef(
            id=food_id,
            name=_col(top, 4, "display_name") or "",
            brand=_col(top, 5, "brand"),
        ),
        serving=ServingRef(
            id=serving.get("serving_id", ""), description=serving.get("description", "")
        ),
        qty=final_qty,
        macros=macros,
        meal=meal,  # type: ignore[arg-type]
        alternates=alternates,
    )
    # proposed_* keep the FIRST proposal (COALESCE): a re-resolution after an
    # edit must not erase the baseline that commit's explicit-choice and
    # rejected_count semantics compare against (§3.5).
    await db.execute(
        """
        UPDATE item_resolutions
        SET status = 'resolved', staged = %s::jsonb, tier = 1,
            resolve_ms = %s,
            proposed_food_id = coalesce(proposed_food_id, %s),
            proposed_serving_id = coalesce(proposed_serving_id, %s),
            proposed_qty = coalesce(proposed_qty, %s),
            alias_id = %s
        WHERE log_id = %s AND item_index = %s
        """,
        (
            json.dumps(item.model_dump(mode="json", exclude_none=True)),
            int((time.perf_counter() - t0) * 1000),
            food_id,
            serving.get("serving_id"),
            final_qty,
            alias_id,
            str(body.log_id),
            body.item_index,
        ),
    )
    return item


# ---------------------------------------------------------------------------
# POST /api/resolve/answer — tier-3 question response (§5)
# ---------------------------------------------------------------------------


@router.post("/api/resolve/answer", response_model=StagedItem)
async def answer_question(body: AnswerRequest) -> StagedItem:
    row = await _item_row(body.log_id, body.item_index)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown log item")

    if body.answer.food_id:
        # Already-cached food (alternates chip): one local read, no network.
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
    # Free text: one re-kick — a second agent run carrying the refinement.
    await db.execute(
        """
        UPDATE item_resolutions
        SET tier3_attempts = tier3_attempts + 1, tier3_started_at = now(),
            status = 'searching'
        WHERE log_id = %s AND item_index = %s
        """,
        (str(body.log_id), body.item_index),
    )
    return await tier3.run_tier3(str(body.log_id), body.item_index, refinement=text)
