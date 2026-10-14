"""PATCH/DELETE /api/entries/:id — post-commit corrections (DESIGN.md §3.5, §5).

Qty-only edits SCALE from a stored full-precision base frozen in the macros
jsonb sidecar (BACKLOG 28) — deterministic, cache-independent, and round-trip
exact under repeated edits, so provider refreshes can never rewrite history
and 2dp rounding never compounds. Only a serving change re-derives from the
foods cache (and resets the base). Food corrections feed alias learning with
the same explicit-choice semantics as staging edits (reusing
resolver/commit.py's helpers). Auth-guarded; the integrator wires this router
into main.py.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Json
from pydantic import BaseModel

import db
from auth import require_user
from models import DayTotals, FoodRef, Macros, Meal, ServingRef
from resolver import commit as commit_tx
from routes.diary import DiaryEntry, compute_day_totals, local_date

router = APIRouter(dependencies=[Depends(require_user)])

# BACKLOG 28: qty edits scale from a frozen full-precision base kept as flat
# float keys inside the macros jsonb ("base_qty", "base_grams", "base_kcal",
# ...). Flat floats, not a nested dict, because diary.py renders
# Macros(**macros) whose extra fields are float-typed — a nested value would
# 500 the diary. The base freezes the first time a scale would actually round
# (until then the stored snapshot is itself an exact multiple of the commit
# snapshot), so repeated qty edits scale from the base and are round-trip
# exact instead of compounding 2dp roundings. Rendered macros stay 2dp.
_BASE_PREFIX = "base_"
_BASE_QTY = "base_qty"
_BASE_GRAMS = "base_grams"


def _split_base(stored: dict) -> tuple[dict, dict]:
    """Split the stored macros jsonb into (display macros, base sidecar)."""
    display = {k: v for k, v in stored.items() if not k.startswith(_BASE_PREFIX)}
    base = {k: v for k, v in stored.items() if k.startswith(_BASE_PREFIX)}
    return display, base


class EntryPatch(BaseModel):
    """Any subset: qty scales; serving_id re-derives; food_id corrects + learns;
    meal refiles the entry under another meal and leaves the portion alone."""

    qty: float | None = None
    serving_id: str | None = None
    food_id: str | None = None
    meal: Meal | None = None


class EntryPatchResponse(BaseModel):
    entry: DiaryEntry
    totals: DayTotals


@router.patch("/api/entries/{entry_id}", response_model=EntryPatchResponse)
async def patch_entry(entry_id: UUID, body: EntryPatch) -> EntryPatchResponse:
    portion = not (body.qty is None and body.serving_id is None and body.food_id is None)
    if not portion and body.meal is None:
        raise HTTPException(422, "nothing to change")
    if body.qty is not None and body.qty <= 0:
        raise HTTPException(422, "qty must be positive")

    async with db.tx() as conn:
        entry = await (
            await conn.execute("SELECT * FROM entries WHERE id = %s", (entry_id,))
        ).fetchone()
        if entry is None:
            raise HTTPException(404, "entry not found")

        # The clock's guess stays in logs.meal_inferred (instrumentation);
        # only the entry moves.
        meal = body.meal or Meal(entry["meal"])
        old_qty = float(entry["qty"])
        qty = float(body.qty) if body.qty is not None else old_qty
        base: dict = {}  # BACKLOG 28 sidecar; empty on serving/food changes
        old_food_id = str(entry["food_id"])
        food_changed = body.food_id is not None and body.food_id != old_food_id
        serving_changed = (
            body.serving_id is not None and body.serving_id != entry["serving_id"]
        )

        if food_changed or serving_changed:
            # serving/food change re-derives from the foods cache (§3.5)
            food_id = body.food_id if food_changed else old_food_id
            food = await (
                await conn.execute("SELECT * FROM foods WHERE id = %s", (food_id,))
            ).fetchone()
            if food is None:
                raise HTTPException(422, f"unknown food {food_id}")
            serving_id = body.serving_id or (
                food["default_serving_id"] if food_changed else entry["serving_id"]
            )
            serving = commit_tx._find_serving(food["servings"], serving_id)
            if serving is None:
                raise HTTPException(422, f"unknown serving {serving_id}")
            missing = commit_tx._missing_core_macros(serving)
            if missing:  # NEVER write partial macros (§4 invariant)
                raise HTTPException(
                    422,
                    f"serving '{serving_id}' is missing core macros "
                    f"({', '.join(missing)}) — pick another serving or food",
                )
            serving_id = serving["serving_id"]
            macros = commit_tx._scale_macros(serving["macros"], qty)
            grams = (
                round(float(serving["grams"]) * qty, 2)
                if isinstance(serving.get("grams"), (int, float))
                else None
            )
        elif not portion:
            # meal-only: the stored snapshot is rewritten as it was, sidecar
            # and all — nothing scales, nothing rounds
            food_id, serving_id, food = old_food_id, entry["serving_id"], None
            macros, base = _split_base(entry["macros"])
            grams = float(entry["grams"]) if entry["grams"] is not None else None
        else:
            # qty-only: scale the stored snapshot proportionally — never the cache
            if old_qty == 0:
                raise HTTPException(422, "cannot scale an entry with qty 0")
            food_id, serving_id, food = old_food_id, entry["serving_id"], None
            display, base = _split_base(entry["macros"])
            if _BASE_QTY not in base:
                # No base yet: the stored snapshot is still exact (nothing has
                # rounded it). Scale it directly while nothing rounds; the
                # moment a scale would round, freeze the pre-edit snapshot as
                # the base so later edits scale from HERE, never from a
                # rounded result (BACKLOG 28).
                ratio = qty / old_qty
                exact = {
                    k: float(v) * ratio
                    for k, v in display.items()
                    if isinstance(v, (int, float))
                }
                g = float(entry["grams"]) * ratio if entry["grams"] is not None else None
                if all(round(x, 2) == x for x in exact.values()) and (
                    g is None or round(g, 2) == g
                ):
                    macros, grams = exact, g  # lossless — still exact, no base
                else:
                    base = {
                        f"{_BASE_PREFIX}{k}": float(v)
                        for k, v in display.items()
                        if isinstance(v, (int, float))
                    }
                    base[_BASE_QTY] = old_qty
                    if entry["grams"] is not None:
                        base[_BASE_GRAMS] = float(entry["grams"])
            if base:
                ratio = qty / float(base[_BASE_QTY])
                macros = {
                    k[len(_BASE_PREFIX):]: round(float(v) * ratio, 2)
                    for k, v in base.items()
                    if k not in (_BASE_QTY, _BASE_GRAMS)
                }
                grams = (
                    round(float(base[_BASE_GRAMS]) * ratio, 2)
                    if _BASE_GRAMS in base
                    else None
                )

        # telemetry + alias learning need the committed item row, if any
        res = None
        if entry["log_id"] is not None and entry["item_index"] is not None:
            res = await (
                await conn.execute(
                    "SELECT * FROM item_resolutions WHERE log_id = %s AND item_index = %s",
                    (entry["log_id"], entry["item_index"]),
                )
            ).fetchone()

        alias_id = entry["alias_id"]
        learned = food_changed and res is not None and res["tier"] != 0
        if learned:
            # §3.4b rule 5 / R3: a correction on a photo, barcode or search
            # log learns nothing — the same logs.input_mode gate as commit.
            log = await (
                await conn.execute(
                    "SELECT input_mode FROM logs WHERE id = %s", (entry["log_id"],)
                )
            ).fetchone()
            learned = log is not None and log["input_mode"] not in commit_tx._NO_LEARN_MODES
        if learned:
            # §3.5: post-commit food corrections feed alias learning — same
            # explicit-choice semantics as staging edits: the original phrase
            # learns the corrected food; the abandoned mapping is rejected.
            alias_id = await commit_tx._learn_alias(
                conn,
                phrase=res["phrase"],
                food_id=food_id,
                serving_id=serving_id,
                qty=qty,
                explicit=True,
                proposed_food_id=entry["food_id"],
                tier=res["tier"],
            )

        await conn.execute(
            "UPDATE entries SET food_id=%s, serving_id=%s, qty=%s, grams=%s, "
            "macros=%s, alias_id=%s, meal=%s WHERE id = %s",
            (
                food_id, serving_id, qty, grams, Json({**macros, **base}), alias_id,
                meal.value, entry_id,
            ),
        )
        if res is not None and portion:
            # keep finals honest (§9 edit-rate, §7.3 mode-of-last-10 inputs);
            # was_edited only ever ratchets true — was_disambiguated untouched.
            # A meal move is no food/serving/qty delta, so it is not an edit.
            await conn.execute(
                "UPDATE item_resolutions SET final_food_id=%s, final_serving_id=%s, "
                "final_qty=%s, alias_id=%s, was_edited=TRUE WHERE id = %s",
                (food_id, serving_id, qty, alias_id, res["id"]),
            )
        if learned:
            await commit_tx._recompute_alias_default(conn, alias_id)

    # response assembly OUTSIDE the tx (db helpers deadlock re-entrantly)
    if food is None:
        food = await db.fetchone(
            "SELECT display_name, brand, servings FROM foods WHERE id = %s", (food_id,)
        )
    serving_row = commit_tx._find_serving(food["servings"], serving_id)
    return EntryPatchResponse(
        entry=DiaryEntry(
            id=entry_id,
            meal=meal,
            food=FoodRef(id=food_id, name=food["display_name"], brand=food["brand"]),
            serving=ServingRef(
                id=serving_id,
                description=serving_row.get("description", serving_id)
                if serving_row
                else serving_id,
            ),
            qty=qty,
            macros=Macros(**macros),
            logged_at=entry["logged_at"],
            # the gram weight moved with the edit (a serving change re-derives
            # it, a qty change scales it) — the diary row renders it, so send
            # the new one rather than leaving the client's stale value up
            grams=grams,
        ),
        totals=await compute_day_totals(await local_date(entry["logged_at"])),
    )


@router.delete("/api/entries/{entry_id}", response_model=DayTotals)
async def delete_entry(entry_id: UUID) -> DayTotals:
    entry = await db.fetchone(
        "SELECT logged_at FROM entries WHERE id = %s", (entry_id,)
    )
    if entry is None:
        raise HTTPException(404, "entry not found")
    await db.execute("DELETE FROM entries WHERE id = %s", (entry_id,))
    return await compute_day_totals(await local_date(entry["logged_at"]))
