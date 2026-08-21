"""/api/foods routes — manual search + select (DESIGN.md §3.4, §5).

The boring escape hatch behind not_found rows, and the Library search:

- GET /api/foods/search?q=&source= — local foods cache first (trigram +
  substring over display_name + brand, the §4 foods_name_trgm target), then
  FDC passthrough: generic (SR Legacy + Foundation) and branded (ranked by
  brand match, §3.4 rule 2), condensed hits carrying (source, source_id).
- POST /api/foods/select — persist the chosen food (persist_food); with log
  context (log_id + item_index) also resolve that staged item exactly like an
  id-bearing answer tap (one get_food + persist, no agent run).

Exposes an APIRouter; the integrator wires it into main.py.
"""

from __future__ import annotations

import asyncio
from typing import Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import db
from auth import require_user
from models import StagedItem
from resolver import foods_repo
from services import tier3
from sources import fdc

router = APIRouter(dependencies=[Depends(require_user)])

_LOCAL_LIMIT = 10
_REMOTE_LIMIT = 10
_SIM_FLOOR = 0.15  # typo net; substring matches pass regardless via ILIKE


# ---------------------------------------------------------------------------
# Wire shapes (route-local; StagedItem stays the one shared contract)
# ---------------------------------------------------------------------------


class FoodHit(BaseModel):
    """One condensed search hit — enough to render a row and to select it."""

    source: str
    source_id: str
    label: str  # display_name (local) / description (FDC)
    brand: str | None = None
    food_id: str | None = None  # set on local-cache hits (already persisted)
    data_type: str | None = None  # FDC: "SR Legacy" | "Foundation" | "Branded"
    protein_per_100g: float | None = None


class FoodSearchResponse(BaseModel):
    query: str
    local: list[FoodHit]  # the cache — always searched, always first
    generic: list[FoodHit]  # FDC SR Legacy + Foundation
    branded: list[FoodHit]  # FDC Branded, brand-ranked (§3.4 rule 2)
    fdc_error: str | None = None  # exception class name when the passthrough
    # failed — local hits still render (the escape hatch must not 502)


class SelectRequest(BaseModel):
    source: str
    source_id: str
    display_name: str | None = None
    log_id: UUID | None = None
    item_index: int | None = None


class SelectResponse(BaseModel):
    food_id: str | None = None  # None only when a log-context resolve degraded
    item: StagedItem | None = None  # set when log context was given


# ---------------------------------------------------------------------------
# GET /api/foods/search
# ---------------------------------------------------------------------------


def _like_pattern(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def _local_hits(q: str) -> list[FoodHit]:
    rows = await db.fetchall(
        """
        SELECT id, source, source_id, display_name, brand,
               similarity(display_name || ' ' || coalesce(brand, ''), %s) AS sim
        FROM foods
        WHERE (display_name || ' ' || coalesce(brand, '')) ILIKE %s
           OR similarity(display_name || ' ' || coalesce(brand, ''), %s) > %s
        ORDER BY sim DESC
        LIMIT %s
        """,
        (q, _like_pattern(q), q, _SIM_FLOOR, _LOCAL_LIMIT),
    )
    return [
        FoodHit(
            source=r["source"],
            source_id=r["source_id"],
            label=r["display_name"],
            brand=r["brand"],
            food_id=str(r["id"]),
        )
        for r in rows
    ]


@router.get("/api/foods/search", response_model=FoodSearchResponse)
async def search_foods(
    q: str, source: Literal["all", "local", "fdc"] = "all"
) -> FoodSearchResponse:
    q = q.strip()
    if not q:
        raise HTTPException(status_code=422, detail="empty query")

    local = await _local_hits(q)
    generic: list[FoodHit] = []
    branded: list[FoodHit] = []
    fdc_error: str | None = None
    if source != "local":
        try:
            generic_raw, branded_raw = await asyncio.gather(
                fdc.search_generic(q), fdc.search_branded(q)
            )
        except httpx.HTTPError as e:
            fdc_error = type(e).__name__  # never the message — may embed a URL
        else:
            cached = {(h.source, h.source_id) for h in local}
            generic = [
                FoodHit(
                    source="fdc",
                    source_id=str(g["fdc_id"]),
                    label=g["description"],
                    data_type=g["data_type"],
                    protein_per_100g=g["protein_per_100g"],
                )
                for g in generic_raw
                if ("fdc", str(g["fdc_id"])) not in cached
            ][:_REMOTE_LIMIT]
            branded = [
                FoodHit(
                    source="fdc",
                    source_id=str(b["fdc_id"]),
                    label=b["description"],
                    brand=b.get("brand_name") or b.get("brand_owner"),
                    data_type=b["data_type"],
                )
                for b in branded_raw
                if ("fdc", str(b["fdc_id"])) not in cached
            ][:_REMOTE_LIMIT]
    return FoodSearchResponse(
        query=q, local=local, generic=generic, branded=branded, fdc_error=fdc_error
    )


# ---------------------------------------------------------------------------
# POST /api/foods/select
# ---------------------------------------------------------------------------


@router.post("/api/foods/select", response_model=SelectResponse)
async def select_food(body: SelectRequest) -> SelectResponse:
    if body.source != "fdc":
        raise HTTPException(status_code=422, detail="unknown source")
    if (body.log_id is None) != (body.item_index is None):
        raise HTTPException(
            status_code=422, detail="log_id and item_index go together"
        )

    if body.log_id is not None and body.item_index is not None:
        # Log context: resolve the staged item like an id-bearing answer tap
        # (§5) — one get_food + persist_food + item write, no agent run. A
        # stale id degrades to a clean needs_input/not_found item (§3.4).
        try:
            item = await tier3.resolve_item_from_source(
                str(body.log_id),
                body.item_index,
                body.source,
                body.source_id,
                display_name=body.display_name,
            )
        except LookupError:
            raise HTTPException(status_code=404, detail="unknown log item")
        return SelectResponse(
            food_id=item.food.id if item.food else None, item=item
        )

    # Library path: just cache the food.
    food = await fdc.get_food(body.source, body.source_id)
    if food is None:  # stale index id (404) — §3.4 adapter rules
        raise HTTPException(status_code=404, detail="source id no longer exists")
    food = dict(food)
    if body.display_name:
        food["display_name"] = body.display_name
    food_id = await foods_repo.persist_food(food)
    return SelectResponse(food_id=str(food_id))
