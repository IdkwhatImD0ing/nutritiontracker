"""GET/PATCH/DELETE /api/aliases — the alias edit sheet + Library list (DESIGN.md §5, §7.1, §7.4).

- GET /api/aliases?phrase=   his alias rows for norm(phrase) (BACKLOG 33), each
  with its food, use count, pin, default portion, and the §3.2 ``usual`` flag
  (cascade.pick — the same selection the resolver makes). Without ``phrase``:
  every alias, most recently used first (the Library's "his foods").
- PATCH /api/aliases/:id     pin/unpin ("make this my usual": pinning is
  exclusive per alias_norm and counts as an explicit choice, §3.2 rules 1–2),
  set the default serving (validated against foods.servings, §4) and/or qty.
- DELETE /api/aliases/:id    delete one mapping. History is immutable
  (entries.macros untouched); the provenance pointers entries.alias_id /
  item_resolutions.alias_id are nulled so the FK allows the delete.

Exposes an APIRouter; the integrator wires it into main.py.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import db
from auth import require_user
from resolver import cascade
from resolver.norm import norm

router = APIRouter(dependencies=[Depends(require_user)])

_LIST_LIMIT = 500


class AliasRow(BaseModel):
    """One aliases row ⋈ its food (route-local response shape)."""

    id: str
    alias_norm: str
    food_id: str
    food_name: str
    brand: str | None = None
    source: str
    use_count: int
    pinned: bool
    usual: bool | None = None  # §3.2 pick() — only meaningful per phrase
    default_serving_id: str | None = None
    default_serving: str | None = None  # description of default_serving_id
    default_qty: float | None = None
    state_answer: str | None = None
    last_used_at: datetime
    last_explicit_at: datetime | None = None
    rejected_count: int
    origin: str


class AliasListResponse(BaseModel):
    phrase: str | None = None
    phrase_norm: str | None = None
    aliases: list[AliasRow]


class AliasPatch(BaseModel):
    pinned: bool | None = None
    default_serving_id: str | None = None
    default_qty: float | None = None


class AliasDeleteResponse(BaseModel):
    id: str
    deleted: bool = True


# Columns cascade.pick() reads (pinned, last_used_at, last_explicit_at,
# alias_norm, food_id, use_count) plus everything the sheet renders.
_ROWS_SQL = """
SELECT a.id, a.alias_norm, a.food_id, a.default_serving_id, a.default_qty,
       a.state_answer, a.pinned, a.use_count, a.last_used_at, a.last_explicit_at,
       a.rejected_count, a.origin,
       f.display_name, f.brand, f.source, f.servings
FROM aliases a JOIN foods f ON f.id = a.food_id
"""


def _serving_description(servings: list[dict], serving_id: str | None) -> str | None:
    for s in servings or []:
        if s.get("serving_id") == serving_id:
            return s.get("description") or serving_id
    return None


def alias_row(r: dict, usual: bool | None = None) -> AliasRow:
    return AliasRow(
        id=str(r["id"]),
        alias_norm=r["alias_norm"],
        food_id=str(r["food_id"]),
        food_name=r["display_name"],
        brand=r["brand"],
        source=r["source"],
        use_count=r["use_count"],
        pinned=bool(r["pinned"]),
        usual=usual,
        default_serving_id=r["default_serving_id"],
        default_serving=_serving_description(r["servings"], r["default_serving_id"]),
        default_qty=float(r["default_qty"]) if r["default_qty"] is not None else None,
        state_answer=r["state_answer"],
        last_used_at=r["last_used_at"],
        last_explicit_at=r["last_explicit_at"],
        rejected_count=r["rejected_count"],
        origin=r["origin"],
    )


async def aliases_for_food(food_id: str) -> list[AliasRow]:
    """The aliases pointing at one food (food detail, §7.4)."""
    rows = await db.fetchall(
        _ROWS_SQL + " WHERE a.food_id = %s ORDER BY a.use_count DESC, a.last_used_at DESC",
        (food_id,),
    )
    return [alias_row(r) for r in rows]


@router.get("/api/aliases", response_model=AliasListResponse)
async def list_aliases(phrase: str | None = None) -> AliasListResponse:
    if phrase is None:
        rows = await db.fetchall(
            _ROWS_SQL + " ORDER BY a.last_used_at DESC LIMIT %s", (_LIST_LIMIT,)
        )
        return AliasListResponse(aliases=[alias_row(r) for r in rows])

    key = norm(phrase)
    if not key:
        raise HTTPException(status_code=422, detail="empty phrase")
    rows = await db.fetchall(
        _ROWS_SQL + " WHERE a.alias_norm = %s ORDER BY a.use_count DESC, a.last_used_at DESC",
        (key,),
    )
    usual_id = (await cascade.pick(rows))["id"] if rows else None
    return AliasListResponse(
        phrase=phrase,
        phrase_norm=key,
        aliases=[alias_row(r, usual=r["id"] == usual_id) for r in rows],
    )


@router.patch("/api/aliases/{alias_id}", response_model=AliasRow)
async def patch_alias(alias_id: UUID, body: AliasPatch) -> AliasRow:
    if body.pinned is None and body.default_serving_id is None and body.default_qty is None:
        raise HTTPException(status_code=422, detail="nothing to change")
    if body.default_qty is not None and body.default_qty <= 0:
        raise HTTPException(status_code=422, detail="default_qty must be positive")

    async with db.tx() as conn:
        row = await (
            await conn.execute(_ROWS_SQL + " WHERE a.id = %s", (alias_id,))
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="alias not found")

        if body.default_serving_id is not None:
            if _serving_description(row["servings"], body.default_serving_id) is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"unknown serving {body.default_serving_id!r} for this food",
                )
            await conn.execute(
                "UPDATE aliases SET default_serving_id = %s WHERE id = %s",
                (body.default_serving_id, alias_id),
            )
        if body.default_qty is not None:
            await conn.execute(
                "UPDATE aliases SET default_qty = %s WHERE id = %s",
                (body.default_qty, alias_id),
            )
        if body.pinned is True:
            # "make this my usual" — exclusive per phrase, and an explicit
            # choice (§3.2 rules 1–2), so unpinning later still leaves this
            # mapping as the most recent explicit pick.
            await conn.execute(
                "UPDATE aliases SET pinned = false WHERE alias_norm = %s AND id <> %s",
                (row["alias_norm"], alias_id),
            )
            await conn.execute(
                "UPDATE aliases SET pinned = true, last_explicit_at = now() WHERE id = %s",
                (alias_id,),
            )
        elif body.pinned is False:
            await conn.execute(
                "UPDATE aliases SET pinned = false WHERE id = %s", (alias_id,)
            )

    row = await db.fetchone(_ROWS_SQL + " WHERE a.id = %s", (alias_id,))
    return alias_row(row)


@router.delete("/api/aliases/{alias_id}", response_model=AliasDeleteResponse)
async def delete_alias(alias_id: UUID) -> AliasDeleteResponse:
    async with db.tx() as conn:
        row = await (
            await conn.execute("SELECT id FROM aliases WHERE id = %s", (alias_id,))
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="alias not found")
        # provenance pointers only — snapshots and telemetry rows stay intact
        await conn.execute(
            "UPDATE entries SET alias_id = NULL WHERE alias_id = %s", (alias_id,)
        )
        await conn.execute(
            "UPDATE item_resolutions SET alias_id = NULL WHERE alias_id = %s",
            (alias_id,),
        )
        await conn.execute("DELETE FROM aliases WHERE id = %s", (alias_id,))
    return AliasDeleteResponse(id=str(alias_id))
