"""Quick-add routes — barcode scan + explicit food pick (DESIGN.md §3.4, §5; M4).

Two of the four + actions never pass through the agent or the cascade:

- GET /api/foods/barcode?code= — digits-only normalization, then the foods
  cache (a code seen before answers with no network call), then the §3.4
  barcode ladder against FDC Branded (sources/fdc.lookup_barcode, one query),
  then Open Food Facts on a miss (sources/off.lookup_barcode — the §8.2
  quality-gated fallback). A hit is persisted (foods_repo.persist_food) so
  the returned id is stable; a miss is {found: false, food: null}, never a 404.
- POST /api/logs/from-food — a log with ONE resolved staged row built from a
  cached foods row: an explicit pick (input_mode 'barcode' | 'search'), so
  tier is NULL (no cascade tier) and alias_id NULL. The user never spoke a
  phrase, so commit learns no alias and no portion default for these rows —
  resolver/commit.py reads logs.input_mode (contract C4b). The log-row and
  item writes are routes/logs.py's own (_persist, _duplicate_post_response):
  same columns, same idempotency by client id.

Exposes an APIRouter; the integrator wires it into main.py.
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime
from typing import Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

import db
from auth import require_user
from models import FoodRef, LogResponse, Macros, Meal, ServingOption, ServingRef, StagedItem
from resolver import cascade, foods_repo
from resolver.commit import with_loggable_servings
from routes import logs as logs_routes
from routes.diary import web_source_of
from routes.photo import ESTIMATE_PICK_REFUSAL
from services import quota
from sources import fdc, off

router = APIRouter(dependencies=[Depends(require_user)])


# ---------------------------------------------------------------------------
# Wire shapes (route-local; StagedItem / LogResponse stay the shared contract)
# ---------------------------------------------------------------------------


class BarcodeFood(BaseModel):
    """The cached food a barcode resolved to — enough to render the pick and
    to call /api/logs/from-food with its id."""

    id: str
    display_name: str
    brand: str | None = None
    source: str
    servings: list[ServingOption]
    macros_per_100g: dict[str, float] | None = None


class BarcodeResponse(BaseModel):
    found: bool
    food: BarcodeFood | None = None


class FromFoodRequest(BaseModel):
    id: UUID  # client-generated — the idempotency key (§8.6)
    food_id: UUID
    # defaults: his spoken portion of this food, else the provider default × 1
    # (§7.3 steps 3–4) — history fills whatever the request leaves null
    serving_id: str | None = None
    qty: float | None = Field(default=None, gt=0, le=1000, allow_inf_nan=False)
    meal_hint: Meal | None = None  # default: the §7.2 clock window
    input_mode: Literal["barcode", "search"]
    captured_at: datetime

    @field_validator("qty", mode="before")
    @classmethod
    def _finite_qty(cls, v):
        """JSON 1e309 / NaN arrive as non-finite floats. Pydantic's own refusal
        echoes the input into the 422 body, which starlette renders with
        allow_nan=False — a 500. Hand the validator the repr ("inf"/"nan")
        instead: allow_inf_nan=False refuses it with a JSON-safe input."""
        if isinstance(v, float) and not math.isfinite(v):
            return repr(v)
        return v


_FOOD_COLS = (
    "id, source, source_url, provenance, display_name, brand, servings, "
    "default_serving_id, per_100g"
)


async def _load_food(food_id) -> dict | None:
    row = await db.fetchone(f"SELECT {_FOOD_COLS} FROM foods WHERE id = %s", (food_id,))
    # BACKLOG 184: a row cached before that rule can still carry a phantom
    # `label` serving as its default, which would stage with no numbers.
    return None if row is None else with_loggable_servings(row)


# ---------------------------------------------------------------------------
# GET /api/foods/barcode
# ---------------------------------------------------------------------------


@router.get("/api/foods/barcode", response_model=BarcodeResponse)
async def lookup_barcode(code: str) -> BarcodeResponse:
    digits = re.sub(r"\D", "", code)
    if not digits:
        raise HTTPException(status_code=422, detail="barcode has no digits")
    # A code seen before answers from the cache, no network: an FDC row
    # carries its stored gtinUpc in the payload, an OFF row is keyed by the
    # barcode — each under whichever padding its source uses, so every rung
    # of the ladder is tried. FDC first when both have it (§8.2).
    variants = fdc.gtin_variants(digits)
    row = await db.fetchone(
        f"""
        SELECT {_FOOD_COLS} FROM foods
        WHERE (source = 'fdc' AND payload->>'gtinUpc' = ANY(%s))
           OR (source = 'off' AND source_id = ANY(%s))
        ORDER BY (source = 'fdc') DESC, created_at
        LIMIT 1
        """,
        (variants, variants),
    )
    # BACKLOG 184: the same filter _load_food applies. Without it this path —
    # the CACHE HIT, the common one — advertises a pre-rule phantom `label` as
    # the default, and the from-food call that follows 422s on the very
    # serving id this response just handed the client, because that path does
    # filter. Both ends must see the same servings.
    row = None if row is None else with_loggable_servings(row)
    if row is None:
        try:
            # FDC Branded first; Open Food Facts only on its miss — the §8.2
            # fallback-on-evidence rule, and OFF reads are rate-limited per IP
            food = await fdc.lookup_barcode(digits)
            if food is None:
                food = await off.lookup_barcode(digits)
        except httpx.HTTPError as e:
            # class name only — never the message, it may embed a URL (§10)
            raise HTTPException(
                status_code=503, detail=f"barcode lookup unavailable ({type(e).__name__})"
            )
        if food is None:
            return BarcodeResponse(found=False, food=None)
        # persist first so the id is stable, then answer from the cached row —
        # a cache-forever conflict keeps the existing record (foods_repo)
        row = await _load_food(await foods_repo.persist_food(food))
    return BarcodeResponse(
        found=True,
        food=BarcodeFood(
            id=str(row["id"]),
            display_name=row["display_name"],
            brand=row["brand"],
            source=row["source"],
            servings=[
                ServingOption(**s) for s in cascade._serving_options(row["servings"] or [])
            ],
            macros_per_100g=row["per_100g"],
        ),
    )


# ---------------------------------------------------------------------------
# POST /api/logs/from-food
# ---------------------------------------------------------------------------


@router.post("/api/logs/from-food", response_model=LogResponse)
async def create_log_from_food(body: FromFoodRequest) -> LogResponse:
    t0 = time.monotonic()
    food = await _load_food(body.food_id)
    if food is None:
        raise HTTPException(status_code=404, detail="food not found")
    if food["source"] == "estimate":  # R1: the photo lane's numberless placeholder
        raise HTTPException(status_code=422, detail=ESTIMATE_PICK_REFUSAL)
    servings: list[dict] = food["servings"] or []
    default_qty = 1.0
    if body.serving_id is not None:
        serving = cascade._serving_by_id(servings, body.serving_id)
        if serving is None:
            raise HTTPException(
                status_code=422, detail=f"food has no serving '{body.serving_id}'"
            )
    else:
        # §7.3 steps 3–4, the text lane's own order for a portion nobody
        # spoke: the mode of his last-10 spoken, committed portions of THIS
        # food (LEARNABLE rows only — a barcode or photo portion never
        # counts), else the provider default × 1; the first serving only if
        # the default dangles.
        serving_id, default_qty, _ = await cascade.resolve_portion(
            {
                "servings": servings,
                "food_default_serving_id": food["default_serving_id"],
                "food_id": food["id"],
                "alias_id": None,
                "default_serving_id": None,
                "default_qty": None,
            },
            None,
            None,
        )
        serving = cascade._serving_by_id(servings, serving_id) or (
            servings[0] if servings else None
        )
    if serving is None:
        raise HTTPException(status_code=422, detail="food has no servings")
    qty = float(body.qty) if body.qty is not None else default_qty

    # Macros from the cache exactly as every resolved row: per-serving × qty,
    # verify-me (never a partial snapshot) when core macros are missing (§4).
    # Computed before any write: a non-finite scale is refused, never a
    # half-written log (Postgres rejects Infinity in jsonb).
    scaled = cascade._scale_macros(serving.get("macros") or {}, qty)
    if not all(math.isfinite(v) for v in scaled.values()):
        raise HTTPException(status_code=422, detail="qty overflows the food's macros")
    complete = all(k in scaled for k in cascade._CORE_MACROS)

    # Idempotent by client id, exactly like POST /api/logs (§8.6), the demo's
    # attempt recorded with the claim (routes/logs.claim_log) — after the free
    # refusals above, so an unknown food, a placeholder, a bad serving or qty
    # cost nothing: only the request that inserted the row writes the item; a
    # duplicate replays the stored staging state (or 409s on a committed/
    # discarded log, 404s on another tenant's id).
    if not await logs_routes.claim_log(
        """
        INSERT INTO logs (id, input_mode, raw_input, captured_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (body.id, body.input_mode, food["display_name"], body.captured_at),
    ):
        replay = await logs_routes._duplicate_post_response(body.id)
        if replay is not None:
            return replay
        await quota.check_log()  # this tenant's orphan, claimed for a re-run: the next attempt

    meal = body.meal_hint or await logs_routes.infer_meal(body.captured_at, None)

    item = StagedItem(
        item_index=0,
        phrase=food["display_name"],  # nothing was spoken — the pick's name (§5)
        status="resolved",
        tier=None,  # an explicit pick has no cascade tier
        food=FoodRef(id=str(food["id"]), name=food["display_name"], brand=food["brand"]),
        serving=ServingRef(
            id=serving["serving_id"],
            description=serving.get("description") or serving["serving_id"],
        ),
        qty=qty,
        macros=Macros(**scaled) if complete else None,
        meal=meal,
        confidence="high" if complete else "low",
        servings=cascade._serving_options(servings) or None,
        web_source=web_source_of(
            food["source"], food["source_url"], food["provenance"], food["display_name"]
        ),
    )
    resolution = cascade.Resolution(
        staged=item, alias_id=None, resolve_ms=int((time.monotonic() - t0) * 1000)
    )
    await logs_routes._persist(body.id, [resolution], meal, short_circuit=False)
    return LogResponse(log_id=body.id, meal=meal, items=[item])
