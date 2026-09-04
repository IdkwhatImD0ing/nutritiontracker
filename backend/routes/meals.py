"""/api/meals — saved-meal (meal macro) CRUD (DESIGN.md §5, §8.4, §3.1).

A saved meal is a name + trigger phrases + a fixed list of (food, serving,
qty). norm() is applied to trigger phrases on every write (§3.1 — post-norm
inputs could never equal "my usual breakfast"); items are validated against
the foods cache; delete = deactivate (old diary rows keep their context,
§8.4). POST /api/meals/from-log is the "Save as meal" tap: the macro is built
from the log's committed entries. The write helpers live in resolver/commit.py
(§3.5 step 4 shares them). Auth-guarded; the integrator wires this router
into main.py.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Json
from pydantic import BaseModel, Field

import db
from auth import require_user
from resolver import commit as commit_tx
from routes.photo import ESTIMATE_PICK_REFUSAL

router = APIRouter(dependencies=[Depends(require_user)])


# ---------------------------------------------------------------------------
# Route-local wire types (§5 /api/meals; models.py untouched)
# ---------------------------------------------------------------------------


class MealItemIn(BaseModel):
    food_id: str
    serving_id: str
    qty: float = 1.0


class MealItemOut(MealItemIn):
    """Stored item + display fields from the foods cache (Library rendering)."""

    food_name: str | None = None
    brand: str | None = None
    serving_description: str | None = None


class MealCreate(BaseModel):
    name: str = Field(min_length=1)
    trigger_phrases: list[str] = []  # empty → norm(name)
    items: list[MealItemIn]


class MealPatch(BaseModel):
    """Any subset. trigger_phrases/items REPLACE the stored lists."""

    name: str | None = None
    trigger_phrases: list[str] | None = None
    items: list[MealItemIn] | None = None
    is_active: bool | None = None


class MealFromLog(BaseModel):
    log_id: UUID
    name: str = Field(min_length=1)
    trigger_phrases: list[str] = []


class MealOut(BaseModel):
    id: UUID
    name: str
    trigger_phrases: list[str]  # as stored: post-norm()
    items: list[MealItemOut]
    is_active: bool
    created_at: datetime
    updated_at: datetime


class MealDeleteResponse(BaseModel):
    id: UUID
    is_active: bool = False


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_FOODS_SQL = "SELECT id, display_name, brand, servings FROM foods WHERE id = ANY(%s::uuid[])"


async def _hydrate(rows: list[dict]) -> list[MealOut]:
    ids = sorted({str(i["food_id"]) for r in rows for i in (r["items"] or [])})
    foods = {str(f["id"]): f for f in await db.fetchall(_FOODS_SQL, (ids,))} if ids else {}
    out: list[MealOut] = []
    for r in rows:
        items: list[MealItemOut] = []
        for i in r["items"] or []:
            food = foods.get(str(i["food_id"]))
            serving = (
                commit_tx._find_serving(food["servings"], i.get("serving_id"))
                if food is not None
                else None
            )
            items.append(
                MealItemOut(
                    food_id=str(i["food_id"]),
                    serving_id=i.get("serving_id"),
                    qty=float(i.get("qty") or 1),
                    food_name=food["display_name"] if food else None,
                    brand=food["brand"] if food else None,
                    serving_description=serving["description"] if serving else None,
                )
            )
        out.append(
            MealOut(
                id=r["id"],
                name=r["name"],
                trigger_phrases=list(r["trigger_phrases"]),
                items=items,
                is_active=r["is_active"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
        )
    return out


async def _one(meal_id: UUID) -> MealOut:
    row = await db.fetchone("SELECT * FROM meal_macros WHERE id = %s", (meal_id,))
    if row is None:
        raise HTTPException(404, "meal not found")
    return (await _hydrate([row]))[0]


def _items_dicts(items: list[MealItemIn]) -> list[dict]:
    return [i.model_dump() for i in items]


_PLACEHOLDER_SQL = (
    "SELECT 1 AS hit FROM foods WHERE id = ANY(%s::uuid[]) AND source = 'estimate' LIMIT 1"
)


async def _refuse_placeholders(conn, items: list[MealItemIn]) -> None:
    """R1/§3.4b: the photo lane's numberless placeholder (source='estimate') is
    not a food — a saved meal holding one would expand to a row that can
    never commit (item 58's broken-meal guard). Refused like every other pick
    path (photo.ESTIMATE_PICK_REFUSAL), before validate_meal_items; ids that
    are not uuids are left to its own 422."""
    ids: list[str] = []
    for i in items:
        try:
            ids.append(str(UUID(i.food_id)))
        except ValueError:
            continue
    if ids and await (await conn.execute(_PLACEHOLDER_SQL, (ids,))).fetchone() is not None:
        raise HTTPException(422, ESTIMATE_PICK_REFUSAL)


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


@router.get("/api/meals", response_model=list[MealOut])
async def list_meals(include_inactive: bool = False) -> list[MealOut]:
    """Active macros; include_inactive=1 adds the deactivated ones (the
    Library lists them under "inactive" with a reactivate action, §8.4)."""
    rows = await db.fetchall(
        "SELECT * FROM meal_macros WHERE (is_active OR %s) ORDER BY is_active DESC, name",
        (include_inactive,),
    )
    return await _hydrate(rows)


@router.post("/api/meals", response_model=MealOut, status_code=201)
async def create_meal(body: MealCreate) -> MealOut:
    try:
        async with db.tx() as conn:
            dup = await (
                await conn.execute(
                    "SELECT 1 AS hit FROM meal_macros WHERE is_active AND lower(name) = lower(%s)",
                    (body.name.strip(),),
                )
            ).fetchone()
            if dup is not None:
                raise HTTPException(409, f"a saved meal named '{body.name.strip()}' exists")
            await _refuse_placeholders(conn, body.items)
            row = await commit_tx.save_meal_macro(
                conn,
                name=body.name,
                trigger_phrases=body.trigger_phrases,
                items=_items_dicts(body.items),
            )
    except commit_tx.MealInvalid as e:
        raise HTTPException(422, str(e))
    return (await _hydrate([row]))[0]


@router.patch("/api/meals/{meal_id}", response_model=MealOut)
async def patch_meal(meal_id: UUID, body: MealPatch) -> MealOut:
    if body.name is None and body.trigger_phrases is None and body.items is None and body.is_active is None:
        raise HTTPException(422, "nothing to change")
    try:
        async with db.tx() as conn:
            row = await (
                await conn.execute("SELECT * FROM meal_macros WHERE id = %s", (meal_id,))
            ).fetchone()
            if row is None:
                raise HTTPException(404, "meal not found")
            name = body.name.strip() if body.name is not None else row["name"]
            if not name:
                raise HTTPException(422, "a saved meal needs a name")
            triggers = (
                commit_tx.norm_triggers(body.trigger_phrases, name)
                if body.trigger_phrases is not None
                else list(row["trigger_phrases"])
            )
            if body.items is not None:
                await _refuse_placeholders(conn, body.items)
            items = (
                await commit_tx.validate_meal_items(conn, _items_dicts(body.items))
                if body.items is not None
                else row["items"]
            )
            is_active = body.is_active if body.is_active is not None else row["is_active"]
            if is_active:
                # same active-name uniqueness POST enforces: two active macros
                # under one name resolve arbitrarily (find_macro and
                # save_meal_macro both take updated_at DESC).
                dup = await (
                    await conn.execute(
                        "SELECT 1 AS hit FROM meal_macros WHERE is_active "
                        "AND lower(name) = lower(%s) AND id <> %s",
                        (name, meal_id),
                    )
                ).fetchone()
                if dup is not None:
                    raise HTTPException(409, f"a saved meal named '{name}' exists")
            row = await (
                await conn.execute(
                    "UPDATE meal_macros SET name = %s, trigger_phrases = %s, items = %s, "
                    "is_active = %s, updated_at = now() WHERE id = %s RETURNING *",
                    (name, triggers, Json(items), is_active, meal_id),
                )
            ).fetchone()
    except commit_tx.MealInvalid as e:
        raise HTTPException(422, str(e))
    return (await _hydrate([row]))[0]


@router.delete("/api/meals/{meal_id}", response_model=MealDeleteResponse)
async def delete_meal(meal_id: UUID) -> MealDeleteResponse:
    """§8.4: delete = deactivate. Idempotent; the row stays."""
    updated = await db.execute(
        "UPDATE meal_macros SET is_active = false, updated_at = now() "
        "WHERE id = %s AND is_active",
        (meal_id,),
    )
    if not updated:
        row = await db.fetchone("SELECT 1 AS hit FROM meal_macros WHERE id = %s", (meal_id,))
        if row is None:
            raise HTTPException(404, "meal not found")
    return MealDeleteResponse(id=meal_id)


@router.post("/api/meals/from-log", response_model=MealOut)
async def meal_from_log(body: MealFromLog) -> MealOut:
    """"Save as meal": the macro's items are the log's committed entries
    (food, serving, qty as committed). Create-or-update by name, so saying
    "save this as my usual breakfast" twice updates the one macro."""
    log = await db.fetchone("SELECT status FROM logs WHERE id = %s", (body.log_id,))
    if log is None:
        raise HTTPException(404, "log not found")
    entries = await db.fetchall(
        "SELECT food_id, serving_id, qty FROM entries WHERE log_id = %s ORDER BY item_index",
        (body.log_id,),
    )
    if not entries:
        raise HTTPException(409, f"log is {log['status']} with no committed items")
    try:
        async with db.tx() as conn:
            row = await commit_tx.save_meal_macro(
                conn,
                name=body.name,
                trigger_phrases=body.trigger_phrases,
                items=[
                    {"food_id": str(e["food_id"]), "serving_id": e["serving_id"], "qty": float(e["qty"])}
                    for e in entries
                ],
            )
    except commit_tx.MealInvalid as e:
        raise HTTPException(422, str(e))
    return (await _hydrate([row]))[0]
