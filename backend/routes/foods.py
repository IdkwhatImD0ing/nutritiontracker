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
    # source='estimate' is the photo lane's numberless placeholder
    # (routes/photo.stage_estimate_food) — never a search hit (R1).
    rows = await db.fetchall(
        """
        SELECT id, source, source_id, display_name, brand,
               similarity(display_name || ' ' || coalesce(brand, ''), %s) AS sim
        FROM foods
        WHERE source <> 'estimate'
          AND ((display_name || ' ' || coalesce(brand, '')) ILIKE %s
               OR similarity(display_name || ' ' || coalesce(brand, ''), %s) > %s)
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
    foods_repo.apply_label(food, body.display_name)  # owner only: the cache is shared
    food_id = await foods_repo.persist_food(food)
    return SelectResponse(food_id=str(food_id))


# ---------------------------------------------------------------------------
# GET /api/foods/{food_id} — food detail (§7.1 "everything the database
# returned" rule, §7.4 drill-in). The full cached payload normalized for
# display: every nutrient the source returned (absent = not measured, never
# zero), the serving definitions, the source badge (date/quality flags), the
# citation + provenance for web foods, and the aliases pointing at the food.
# ---------------------------------------------------------------------------

from datetime import datetime as _datetime  # noqa: E402

from models import ServingOption, WebSource  # noqa: E402
from routes.aliases import AliasRow, aliases_for_food  # noqa: E402
from routes.diary import web_source_of  # noqa: E402

_STALE_YEARS = 3
_SOURCE_LABELS = {
    "fdc": "USDA FoodData Central",
    "off": "Open Food Facts",
    "web": "Web",
    "estimate": "Photo estimate",  # §3.4b placeholder — no numbers
}


class NutrientRow(BaseModel):
    """One nutrient as the source reported it. ``basis`` is the denominator:
    per 100 g / 100 ml for FDC foodNutrients, per serving for branded label
    panels and web-transcribed values."""

    name: str
    amount: float
    unit: str | None = None
    basis: str


class SourceBadge(BaseModel):
    source: str
    label: str
    data_type: str | None = None  # FDC: "SR Legacy" | "Foundation" | "Branded"
    data_source: str | None = None  # FDC Branded: "GDSN" | "LI"
    published_date: str | None = None
    modified_date: str | None = None
    quality_flags: list[str]


class FoodDetail(BaseModel):
    id: str
    source: str
    source_id: str
    source_url: str | None = None
    name: str
    display_name: str
    brand: str | None = None
    food_type: str
    state: str | None = None
    state_sensitive: bool
    default_serving_id: str
    servings: list[ServingOption]
    per_100g: dict[str, float] | None = None
    nutrients: list[NutrientRow]
    badge: SourceBadge
    citation: WebSource | None = None  # §3.4a: render as a visible, clickable link
    provenance: dict | None = None
    payload: dict | None = None  # the provider response verbatim (§7.1 "everything")
    aliases: list[AliasRow]
    refreshed_at: _datetime
    created_at: _datetime


def _parse_date(s: str | None) -> _datetime | None:
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return _datetime.strptime(s[:19] if "T" in s else s, fmt)
        except ValueError:
            continue
    return None


def _nutrients(row: dict) -> list[NutrientRow]:
    payload = row["payload"] or {}
    out: list[NutrientRow] = []
    if row["source"] == "fdc":
        basis = "per 100 ml" if any(
            s.get("serving_id") == "100ml" for s in row["servings"] or []
        ) else "per 100 g"
        for n in payload.get("foodNutrients") or []:
            amount = n.get("amount")
            if amount is None:  # category-header rows (§3.4 adapter rule)
                continue
            meta = n.get("nutrient") or {}
            name = meta.get("name") or n.get("name")
            if not name:
                continue
            out.append(
                NutrientRow(
                    name=name, amount=float(amount), unit=meta.get("unitName"), basis=basis
                )
            )
        for key, val in (payload.get("labelNutrients") or {}).items():
            if isinstance(val, dict) and val.get("value") is not None:
                out.append(
                    NutrientRow(
                        name=key, amount=float(val["value"]), basis="per serving (label)"
                    )
                )
    elif row["source"] == "web":
        values = ((row["provenance"] or {}).get("values_as_read")) or {}
        for key, val in values.items():
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                out.append(NutrientRow(name=key, amount=float(val), basis="per serving"))
    elif row["source"] == "off":
        for key, val in (payload.get("nutriments") or {}).items():
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                out.append(NutrientRow(name=key, amount=float(val), basis="as reported"))
    return out


def _badge(row: dict) -> SourceBadge:
    payload = row["payload"] or {}
    source = row["source"]
    flags: list[str] = []
    published = modified = data_type = data_source = None
    if source == "fdc":
        data_type = payload.get("dataType")
        data_source = payload.get("dataSource")
        published = payload.get("publishedDate")
        modified = payload.get("modifiedDate")
        when = _parse_date(modified) or _parse_date(published)
        if when is not None and (_datetime.now() - when).days > 365 * _STALE_YEARS:
            flags.append(f"old record (published {published or modified})")
    elif source == "off":
        published = str(payload.get("last_modified_t") or "") or None
        completeness = payload.get("completeness")
        if completeness is not None:
            flags.append(f"completeness {completeness}")
        flags.extend(str(t) for t in payload.get("data_quality_warnings_tags") or [])
    elif source == "web":
        published = (row["provenance"] or {}).get("transcribed_at")
        flags.append("transcribed from the official page — verify against the link")
    return SourceBadge(
        source=source,
        label=_SOURCE_LABELS.get(source, source),
        data_type=data_type,
        data_source=data_source,
        published_date=published,
        modified_date=modified,
        quality_flags=flags,
    )


# §3.4b/§8.9: a photo placeholder (source='estimate') is the photographer's
# alone. Its provenance names the log it came from, and under RLS that log is
# visible only to its tenant — everyone else gets the 404 an unknown id gets.
# What the photographer sees is trimmed of the row's internals: the log id,
# item index and capture time are pointers, not food data, and the payload is
# the same vision output again.
_PLACEHOLDER_INTERNALS = ("log_id", "item_index", "captured_at")


async def _own_placeholder(row: dict) -> bool:
    try:
        log_id = UUID(str((row["provenance"] or {}).get("log_id")))
    except ValueError:
        return False  # no pointer to a log: nobody's
    return await db.fetchone("SELECT 1 AS hit FROM logs WHERE id = %s", (log_id,)) is not None


def _public_provenance(row: dict) -> dict | None:
    prov = row["provenance"]
    if row["source"] != "estimate" or not isinstance(prov, dict):
        return prov
    return {k: v for k, v in prov.items() if k not in _PLACEHOLDER_INTERNALS} or None


@router.get("/api/foods/{food_id}", response_model=FoodDetail)
async def get_food_detail(food_id: UUID) -> FoodDetail:
    row = await db.fetchone("SELECT * FROM foods WHERE id = %s", (food_id,))
    if row is None or (row["source"] == "estimate" and not await _own_placeholder(row)):
        raise HTTPException(status_code=404, detail="food not found")
    servings = [
        ServingOption(
            serving_id=s["serving_id"],
            description=s.get("description") or s["serving_id"],
            grams=float(s["grams"]) if isinstance(s.get("grams"), (int, float)) else None,
            macros={
                k: float(v)
                for k, v in (s.get("macros") or {}).items()
                if isinstance(v, (int, float))
            }
            or None,
        )
        for s in row["servings"] or []
        if s.get("serving_id")
    ]
    return FoodDetail(
        id=str(row["id"]),
        source=row["source"],
        source_id=row["source_id"],
        source_url=row["source_url"],
        name=row["name"],
        display_name=row["display_name"],
        brand=row["brand"],
        food_type=row["food_type"],
        state=row["state"],
        state_sensitive=bool(row["state_sensitive"]),
        default_serving_id=row["default_serving_id"],
        servings=servings,
        per_100g=row["per_100g"],
        nutrients=_nutrients(row),
        badge=_badge(row),
        citation=web_source_of(
            row["source"], row["source_url"], row["provenance"], row["display_name"]
        ),
        provenance=_public_provenance(row),
        payload=(
            row["payload"]
            if row["source"] != "estimate" and isinstance(row["payload"], dict)
            else None
        ),
        aliases=await aliases_for_food(str(row["id"])),
        refreshed_at=row["refreshed_at"],
        created_at=row["created_at"],
    )
