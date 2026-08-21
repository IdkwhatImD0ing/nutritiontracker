"""PATCH/DELETE /api/entries/:id — post-commit corrections (DESIGN.md §3.5, §5).

Qty-only edits SCALE the stored snapshot proportionally (macros × new/old) —
deterministic and cache-independent, so provider refreshes can never rewrite
history. Only a serving change re-derives from the foods cache. Food
corrections feed alias learning with the same explicit-choice semantics as
staging edits (reusing resolver/commit.py's helpers). Auth-guarded; the
integrator wires this router into main.py.
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


class EntryPatch(BaseModel):
    """Any subset: qty scales; serving_id re-derives; food_id corrects + learns."""

    qty: float | None = None
    serving_id: str | None = None
    food_id: str | None = None


class EntryPatchResponse(BaseModel):
    entry: DiaryEntry
    totals: DayTotals


@router.patch("/api/entries/{entry_id}", response_model=EntryPatchResponse)
async def patch_entry(entry_id: UUID, body: EntryPatch) -> EntryPatchResponse:
    if body.qty is None and body.serving_id is None and body.food_id is None:
        raise HTTPException(422, "nothing to change")
    if body.qty is not None and body.qty <= 0:
        raise HTTPException(422, "qty must be positive")

    async with db.tx() as conn:
        entry = await (
            await conn.execute("SELECT * FROM entries WHERE id = %s", (entry_id,))
        ).fetchone()
        if entry is None:
            raise HTTPException(404, "entry not found")

        old_qty = float(entry["qty"])
        qty = float(body.qty) if body.qty is not None else old_qty
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
        else:
            # qty-only: scale the stored snapshot proportionally — never the cache
            if old_qty == 0:
                raise HTTPException(422, "cannot scale an entry with qty 0")
            food_id, serving_id, food = old_food_id, entry["serving_id"], None
            ratio = qty / old_qty
            macros = {
                k: round(float(v) * ratio, 2)
                for k, v in entry["macros"].items()
                if isinstance(v, (int, float))
            }
            grams = (
                round(float(entry["grams"]) * ratio, 2)
                if entry["grams"] is not None
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
        learned = False
        if food_changed and res is not None and res["tier"] != 0:
            learned = True
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
            "macros=%s, alias_id=%s WHERE id = %s",
            (food_id, serving_id, qty, grams, Json(macros), alias_id, entry_id),
        )
        if res is not None:
            # keep finals honest (§9 edit-rate, §7.3 mode-of-last-10 inputs);
            # was_edited only ever ratchets true — was_disambiguated untouched
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
            meal=Meal(entry["meal"]),
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
