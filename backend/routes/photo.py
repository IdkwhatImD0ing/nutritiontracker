"""POST /api/logs/photo (DESIGN.md §3.4a, §12 M4).

The photo lane — the one place estimation is permitted, and only for what a
photo cannot tell us: portions, and items no database resolves.

  photo → services/vision.analyze (gpt-6-astra, structured: the foods seen
  + portion estimates, NEVER nutrition numbers) → ParsedItem[] → the SAME
  cascade as text/voice (cascade.resolve_item, tiers 1–3; tier 0 is a spoken
  shortcut and a plate is not a meal reference) → routes/logs._persist with
  input_mode='photo'.

Per row: a database food whose amount came from the photo is flagged
estimated="portion" with a user-facing estimate_note. The estimate rides the
§7.3 spoken-quantity channel (a count, else est_grams as "g"), so a row tier
3 resolves later gets it too, and the flag survives every rewrite through
resolver/commit.KEEP_LOG_KEYS_SQL. A row nothing resolves — tier 3 said
not_found — becomes an estimate row via stage_estimate_food(): a foods row
with source='estimate' and provenance = the vision output, WITHOUT macros,
because the model must not invent calories; flagged estimated="unresolved",
it routes the user to search and can never commit (the core-macros guard).

An estimated row no longer waits for a per-row confirm (Bill, 2026-09-09):
the commit is the confirmation, and the flag's job is to render the row amber
with its note in the sheet and in the diary. A numberless `unresolved` row
still cannot commit — the core-macros guard refuses it, and Search is the way
out. The image bytes are never persisted; each
row's `vision` jsonb key keeps the raw structured output for the eval loop.
Photo rows learn no aliases and no portion defaults (commit.py reads
logs.input_mode).
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from psycopg.types.json import Json
from pydantic import BaseModel

import db
import deadline
from auth import is_owner, require_user
from models import (
    FoodRef, Macros, Meal, ParsedItem, ServingOption, ServingRef, StagedItem, WebSource,
)
from resolver import cascade, foods_repo
from resolver.norm import norm
from resolver.commit import KEEP_LOG_KEYS_SQL
from routes.logs import (
    _PHOTO_RESERVE_S,
    _RERUN_GRACE_SECONDS,
    AgentOutcome,
    Emit,
    LogCreateResponse,
    _drop_empty_log,
    cascade_summary,
    _duplicate_post_response,
    _inline_enabled,
    _load_log_response,
    _persist,
    _resolve_inline,
    _size_by_weight,
    _stage_estimate,
    claim_log,
    infer_meal,
)
from services import quota

if TYPE_CHECKING:
    from models import LogCreate, LogPollResponse
    from services.agent import PhotoChange
    from services.places import Fix, Place
    from services.vision import VisionItem, VisionResult
    from sources.bonappetit import CafeMeal, Dish

router = APIRouter(dependencies=[Depends(require_user)])

# Upload bounds: the client downscales before upload (a re-encoded plate
# photo is well under 1 MB); 12 MB is the contract's cap. Content type AND
# magic bytes must both pass — the sniffed type is what reaches the model.
MAX_IMAGE_BYTES = 12 * 1024 * 1024
IMAGE_MIME_ALLOWLIST = frozenset({"image/jpeg", "image/png", "image/webp"})
# ISO-BMFF brands (bytes 8–12, after "ftyp") that mean HEIC/HEIF
_HEIC_BRANDS = frozenset(
    {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs", b"mif1", b"msf1"}
)

# Same live-row guard as tier3._write_item_row (BACKLOG 27): an estimate-row
# write never resurrects a removed row or clobbers a committed one.
_LIVE_STATUSES = ("searching", "needs_input", "not_found", "resolved")
_LIVE_SQL = (
    "status IN ('searching', 'needs_input', 'not_found', 'resolved') "
    "AND committed_at IS NULL"
)

# R1: a source='estimate' foods row is this lane's numberless placeholder
# (stage_estimate_food) — never a pick target on any lane. The 422 detail
# every pick path (from-food, the answer chip) answers with.
ESTIMATE_PICK_REFUSAL = "photo-lane placeholder — pick a database food"

# §3.4b: vision's identification confidence caps the row's.
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _cap_confidence(row: str | None, vision: str) -> str:
    if row is None or _CONFIDENCE_RANK[vision] < _CONFIDENCE_RANK[row]:
        return vision
    return row


def sniff_image(head: bytes) -> str | None:
    """Image type from magic bytes: jpeg/png/webp (accepted), heic (named so
    the refusal can say why — the vision API takes PNG/JPEG/WEBP/GIF only),
    else None."""
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp" and head[8:12] in _HEIC_BRANDS:
        return "image/heic"
    return None


def _local(captured_at: datetime, tz: str | None) -> datetime:
    """The photo's local wall clock. `tz` is an unvalidated client string: an
    unknown zone, or none, keeps captured_at's own offset."""
    try:
        return captured_at.astimezone(ZoneInfo(tz)) if tz else captured_at
    except Exception:
        return captured_at


async def _photo_context(
    captured_at: datetime, tz: str | None, where: Fix | None = None
) -> tuple[str, list[Place]]:
    """What the vision call is told about the person, and when they ate.

    §6.1's profile notes are the one place they already write standing facts
    about themselves ("I eat lunch at the Pinterest SF cafeteria, menu at
    …"), and they are what turns the web lane from a guess into a lookup: no
    place named, nothing to look up. The date matters as much — a cafeteria
    menu is per-day, so a photo without one can only be matched against
    today's. Nothing here knows what a cafeteria is; the notes are free text
    and the lane works the same for a restaurant.

    `where` (2026-09-12) is the phone's fix when the shot was taken; the
    places around it (services/places.py), nearest first, name a place
    without anyone having to write it down. They are looked up beside the
    notes read — two independent round trips — and returned with the context
    so the chat can name the place. They are worded as where the PHONE was,
    never where the food came from — a plate cooked at home next door to a
    taqueria looks the same to a map."""
    from services import places  # lazy: stubbed in route tests

    async def _near() -> list[Place]:
        return await places.nearby(where) if where is not None else []

    row, near = await asyncio.gather(
        db.fetchone("SELECT value FROM settings WHERE key = 'profile_notes'"), _near()
    )
    notes = (row["value"] if row and isinstance(row.get("value"), str) else "") or ""
    local = _local(captured_at, tz)
    when = f"This photo was taken on {local:%A %Y-%m-%d} at {local:%H:%M} local time."
    around = ""
    if near:
        around = (
            "When it was taken, the phone was at or near one of these places "
            "(from a map, nearest first):\n"
            + "\n".join(f"- {p.line()}" for p in near)
            + "\nThat is where the phone was, not necessarily where the food came "
            "from: food cooked at home or brought from somewhere else looks the "
            "same to a map."
        )
    return "\n\n".join(part for part in (notes.strip(), when, around) if part), near


async def _cafe_meal(captured_at: datetime, tz: str | None) -> CafeMeal | None:
    """§3.4b café menu (2026-09-30): what the Bon Appétit café the profile
    notes name served at the meal the photo falls in, every dish with its
    published numbers — None when the notes name no such café, it served
    nothing at that hour, or its page could not be read. It runs beside
    _photo_context and reads the notes itself, so that function's
    (context, places) answer stays exactly as it is."""
    from sources import bonappetit  # lazy: stubbed in route tests

    row = await db.fetchone("SELECT value FROM settings WHERE key = 'profile_notes'")
    site = bonappetit.site_in(row["value"] if row and isinstance(row.get("value"), str) else "")
    return await bonappetit.meal_at(site, _local(captured_at, tz)) if site else None


# ---------------------------------------------------------------------------
# vision item → cascade input → flagged row
# ---------------------------------------------------------------------------


def _count(v: VisionItem) -> float | None:
    return float(v.qty) if v.qty is not None and v.qty > 0 else None


def _grams(v: VisionItem) -> float | None:
    return float(v.est_grams) if v.est_grams is not None and v.est_grams > 0 else None


def _parsed_item(v: VisionItem) -> ParsedItem:
    """The cascade's input. The estimate rides the §7.3 spoken-quantity
    channel: a count for countable items (the piece word is dropped — the
    count rule prefers the food's household serving, which the word would
    only have to fuzzy-match), else est_grams as grams. Nothing else is
    invented: no state, brand, library or meal match."""
    qty = unit = None
    if _count(v) is not None:
        qty = _count(v)
    elif _grams(v) is not None:
        qty, unit = _grams(v), "g"
    return ParsedItem(
        phrase=v.phrase.strip() or v.note.strip(),
        phrase_kind="food",
        qty=qty,
        unit=unit,
        state=None,
        brand=None,
        library_match=None,
        meal_ref=None,
    )


def _estimate_note(v: VisionItem) -> str:
    grams = _grams(v)
    return f"Photo: {v.note.strip() or v.phrase}" + (f" (~{grams:g} g)" if grams else "")


def _vision_portion(item: StagedItem, v: VisionItem) -> tuple[ServingOption, float] | None:
    """The photo's portion on the resolved food's own servings: a count on a
    household serving, else est_grams on a gram-based one (the 100 g row,
    else the least count-like — cascade.resolve_portion's choice). None when
    nothing maps."""
    servings = item.servings or []
    count = _count(v)
    if count is not None:
        counts = [s for s in servings if cascade._is_count_serving(s.description)]
        if counts:
            current = next(
                (s for s in counts if item.serving and s.serving_id == item.serving.id), None
            )
            base = current or counts[0]
            return base, count / cascade._pieces_per(base.description)  # "4 large" holds four
    grams = _grams(v)
    if grams is not None:
        by_grams = [s for s in servings if s.grams and s.grams > 0]
        if by_grams:
            base = next((s for s in by_grams if s.grams == 100), None) or max(
                by_grams, key=lambda s: not cascade._is_count_serving(s.description)
            )
            return base, round(grams / float(base.grams), 4)
    return None


def _macros_for(serving: ServingOption, qty: float) -> Macros | None:
    raw = serving.macros or {}
    if all(k in raw for k in cascade._CORE_MACROS):
        return Macros(**cascade._scale_macros(raw, qty))
    return None


def _flag(item: StagedItem, v: VisionItem) -> StagedItem:
    """Stamp the §3.4a flag on one cascade row. A resolved row gets the
    photo's portion applied over its servings, macros recomputed from the
    food's own per-serving numbers; a searching row keeps the estimate in
    spoken_qty/unit for tier 3. estimated="portion" whenever vision supplied
    a portion; a row vision could not size keeps the resolver's usual portion,
    unflagged. Vision's identification confidence caps the row's."""
    has_portion = _count(v) is not None or _grams(v) is not None
    update: dict = {}
    note = _estimate_note(v)
    if item.status == "resolved" and item.serving is not None and has_portion:
        mapped = _vision_portion(item, v)
        if mapped is not None:
            serving, qty = mapped
            macros = _macros_for(serving, qty)
            update.update(
                serving=ServingRef(id=serving.serving_id, description=serving.description),
                qty=qty,
                macros=macros,
                confidence="low" if macros is None else _cap_confidence(item.confidence, v.confidence),
            )
        else:
            # nothing maps — the cascade may have put est_grams straight into
            # qty (§7.3's no-gram-serving branch); one serving, verify me
            current = next(
                (s for s in item.servings or [] if s.serving_id == item.serving.id), None
            )
            update.update(
                qty=1.0,
                macros=_macros_for(current, 1.0) if current else None,
                confidence="low",
            )
            note += " — couldn't map that to a serving; check the amount"
    elif item.status == "resolved":
        update["confidence"] = _cap_confidence(item.confidence, v.confidence)
    if has_portion:
        update.update(estimated="portion", estimate_note=note)
    return item.model_copy(update=update) if update else item


async def _stage_menu_food(item: StagedItem, v: VisionItem) -> StagedItem:
    """A row the model matched to a published dish, carrying that page's own
    numbers (§3.4a as widened 2026-09-09, Bill's ruling: the photo lane may
    transcribe a menu's nutrition, because every photo row is confirmed by a
    human before it commits).

    The numbers still reach the diary the only way any numbers can — through a
    cached `foods` row, here `source='menu'` with the URL and the raw block in
    its provenance, so a committed entry can always be traced to the page it
    was read off. The row keeps `estimated="portion"`: the FOOD is the menu's,
    the AMOUNT is one published serving and almost certainly not what was on
    the plate, so it still cannot commit until someone says it is right.

    CODE decides whether the block is usable, not the prompt: a dish name, an
    http(s) URL and all four core macros as real numbers, or the whole block is
    dropped and the row stays exactly as the cascade left it. A page read
    halfway must yield nothing rather than a plausible-looking calorie."""
    m = v.menu
    if m is None:
        return item
    from services import vision  # lazy: stubbed in route tests

    macros = {"kcal": m.kcal, "protein_g": m.protein_g, "carb_g": m.carb_g, "fat_g": m.fat_g}
    if not (m.dish or "").strip() or not m.source_url.lower().startswith(("http://", "https://")):
        return item
    # finite and non-negative: pydantic already made these floats, but it
    # accepts inf/nan, and `Infinity` is not JSON — it would blow up in
    # persist_food after the vision spend, or store a number nothing can read
    if not all(math.isfinite(x) and x >= 0 for x in macros.values()):
        return item

    dish = m.dish.strip()
    serving = {
        "serving_id": "menu",
        "description": (m.serving or "").strip() or "1 serving",
        "grams": None,
        "macros": macros,
    }
    food_id = await foods_repo.persist_food(
        {
            "source": "menu",
            "source_id": f"{m.source_url}#{norm(dish)}",
            "source_url": m.source_url,
            "provenance": {
                "menu": m.model_dump(mode="json"),
                # which model read the page: a transcribed number that turns
                # out wrong has to be traceable to the run that made it
                "model": vision.MODEL,
                "vision": True,
            },
            "name": dish,
            "display_name": dish,
            "brand": None,
            "food_type": "generic",
            "state": None,
            "state_sensitive": False,
            "servings": [serving],
            "default_serving_id": "menu",
            "per_100g": None,
            "payload": {"menu": m.model_dump(mode="json")},
        }
    )
    return item.model_copy(
        update={
            "status": "resolved",
            "tier": None,
            "food": FoodRef(id=food_id, name=dish),
            "serving": ServingRef(id="menu", description=serving["description"]),
            "qty": 1.0,
            "macros": Macros(**macros),
            "servings": [ServingOption(**s) for s in cascade._serving_options([serving])],
            "search_instead": False,
            "estimated": "portion",
            "estimate_note": (
                f"Menu: {dish} — numbers from {m.source_url}, "
                f"per {serving['description']}. Check the amount."
            ),
            # §3.4a: transcribed numbers owe the reader the page they came
            # from, as a visible link — the same debt tier 3's web rows pay
            "web_source": WebSource(url=m.source_url, title=dish),
        }
    )


# ---------------------------------------------------------------------------
# The café menu (§3.4b, 2026-09-30): a matched dish's published numbers
# ---------------------------------------------------------------------------

# The café publishes some protein figures that are plainly wrong (7 g in 7.7 oz
# of honey mustard chicken, 3 g in 2.8 oz of roast chicken, checked
# 2026-09-30), and a model asked to spot them caught half. So code does: when
# the row's database food is a protein food (≥ 10 g per 100 g) and the dish
# claims under 40% of its protein per gram, the database row stays.
_GUARD_MIN_PROTEIN_PER_G = 0.10
_GUARD_RATIO = 0.4


def _protein_per_g(item: StagedItem) -> float | None:
    """Protein per gram of the row's database food — None for a row with no
    database food (still searching, or the numberless §3.4b placeholder) or
    no gram serving to measure by."""
    if item.status != "resolved" or item.food is None or item.estimated == "unresolved":
        return None
    for s in item.servings or []:
        protein = (s.macros or {}).get("protein_g")
        if s.grams and s.grams > 0 and protein is not None:
            return protein / s.grams
    return None


def _numbers_look_wrong(item: StagedItem, dish: Dish) -> bool:
    ref = _protein_per_g(item)
    return (
        ref is not None
        and ref >= _GUARD_MIN_PROTEIN_PER_G
        and dish.grams is not None
        and dish.nutrients["protein_g"] / dish.grams < _GUARD_RATIO * ref
    )


async def _keep_database_row(log_id: UUID, item: StagedItem, dish: Dish) -> StagedItem | None:
    """The guard tripped: the row keeps its database food, and its note says
    why the café's dish was not used — in words, never the numbers."""
    doubt = (
        f"the café lists {dish.name}, but its published numbers look wrong, "
        "so this row keeps the database's"
    )
    note = f"{item.estimate_note} — {doubt}" if item.estimate_note else doubt[0].upper() + doubt[1:]
    row = await db.fetchone(
        f"UPDATE item_resolutions SET staged = staged{KEEP_LOG_KEYS_SQL} "
        "|| jsonb_build_object('estimate_note', %s::text) "
        f"WHERE log_id = %s AND item_index = %s AND {_LIVE_SQL} RETURNING staged",
        (note, str(log_id), item.item_index),
    )
    return StagedItem.model_validate(row["staged"]) if row else None


async def _stage_cafe_food(
    log_id: UUID, item: StagedItem, v: VisionItem, dish: Dish, cafe: CafeMeal
) -> StagedItem | None:
    """The row becomes the café's dish: a `source='menu'` foods row carrying
    every nutrient the page published for its serving, sized by the photo's
    grams, flagged "portion" (the food is the café's, the amount a guess) and
    cited to the day page. Written the way stage_estimate_food writes: KEEP
    carries the old flag and note, so both are re-applied after it, and the
    photo's spoken-quantity channel is carried over for a later search pick.
    None when the row is no longer live."""
    from services import menu_match  # lazy: stubbed in route tests

    serving = {
        "serving_id": "menu",
        "description": dish.serving or "1 serving",
        "grams": dish.grams,
        "macros": dict(dish.nutrients),
    }
    # persist_food caches forever, so the numbers are part of the key: a dish
    # the café re-publishes with new numbers is a new row, never a rewrite
    # under a diary that already snapshotted the old ones
    digest = hashlib.sha256(
        json.dumps([serving["description"], serving["macros"]], sort_keys=True).encode()
    ).hexdigest()[:12]
    food_id = await foods_repo.persist_food(
        {
            "source": "menu",
            "source_id": f"{cafe.site}.cafebonappetit.com#{dish.id}:{digest}",
            "source_url": cafe.url,
            "provenance": {
                "cafe": {
                    "site": cafe.site, "dish_id": dish.id, "station": dish.station,
                    "date": cafe.day.isoformat(), "meal": cafe.label, "url": cafe.url,
                },
                # which model chose the dish: a wrong match has to be
                # traceable to the run that made it
                "model": menu_match.MODEL,
            },
            "name": dish.name,
            "display_name": dish.name,
            "brand": None,
            "food_type": "generic",
            "state": None,
            "state_sensitive": False,
            "servings": [serving],
            "default_serving_id": "menu",
            "per_100g": None,
            "payload": {"dish": dataclasses.asdict(dish)},
        }
    )
    grams = _grams(v)
    qty = round(grams / dish.grams, 4) if grams and dish.grams else 1.0
    note = (
        f"Café menu ({cafe.label}): {dish.name} — the café's published numbers, "
        f"per {serving['description']} ({cafe.url}); the amount is the photo's"
        + (f" (~{grams:g} g)" if grams else ", one serving — check it")
    )
    staged = StagedItem(
        item_index=item.item_index,
        phrase=item.phrase,
        status="resolved",
        tier=None,
        food=FoodRef(id=food_id, name=dish.name),
        serving=ServingRef(id="menu", description=serving["description"]),
        qty=qty,
        macros=Macros(**cascade._scale_macros(dish.nutrients, qty)),
        meal=item.meal,
        confidence=v.confidence,
        servings=[ServingOption(**s) for s in cascade._serving_options([serving])],
        search_instead=False,
        estimated="portion",
        estimate_note=note,
        web_source=WebSource(url=cafe.url, title=f"{dish.name} — {cafe.label} menu"),
    )
    row = await db.fetchone(
        f"""
        UPDATE item_resolutions
        SET status = 'resolved',
            staged = %s::jsonb
                     || jsonb_build_object('spoken_qty', item_resolutions.staged->'spoken_qty',
                                           'spoken_unit', item_resolutions.staged->'spoken_unit')
                     {KEEP_LOG_KEYS_SQL}
                     || jsonb_build_object('estimated', 'portion'::text, 'estimate_note', %s::text)
        WHERE log_id = %s AND item_index = %s AND {_LIVE_SQL}
        RETURNING staged
        """,
        (Json(staged.model_dump(mode="json", exclude_none=True)), note, str(log_id), item.item_index),
    )
    return StagedItem.model_validate(row["staged"]) if row else None


async def _stage_cafe_rows(
    log_id: UUID,
    rows: list[StagedItem],
    seen: VisionResult,
    cafe: CafeMeal,
    picks: dict[int, Dish],
) -> tuple[list[StagedItem], list[str], list[str]]:
    """Each matched item's row becomes the café's dish, unless the dish
    published no numbers or the protein guard says they look wrong — then the
    row stays as the cascade and tier 3 left it. Runs after the inline
    lookups, because the guard's reference is the database food they found.
    Returns (the rows, the dishes used, the dishes set aside)."""
    by_index = {r.item_index: r for r in rows}  # one row per vision item, same index
    used: list[str] = []
    aside: list[str] = []
    for i, dish in sorted(picks.items()):
        item = by_index[i]
        if not dish.nutrients:
            aside.append(dish.name)
            continue
        if _numbers_look_wrong(item, dish):
            written = await _keep_database_row(log_id, item, dish)
            aside.append(dish.name)
        else:
            written = await _stage_cafe_food(log_id, item, seen.items[i], dish, cafe)
            if written is not None:
                used.append(dish.name)
        if written is not None:
            by_index[i] = written
    return [by_index[r.item_index] for r in rows], used, aside


def _summary(seen: VisionResult) -> str:
    """logs.raw_input for a photo log — the History line, never model input."""
    parts = []
    for v in seen.items:
        if _count(v) is not None:
            parts.append(f"{_count(v):g} {v.phrase}")
        elif _grams(v) is not None:
            parts.append(f"{v.phrase} ~{_grams(v):g} g")
        else:
            parts.append(v.phrase)
    return "photo: " + (", ".join(parts) if parts else "nothing recognized")


def _and_list(parts: list[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def photo_reply(seen: VisionResult, items: list[StagedItem]) -> str:
    """The photo turn's reply in the chat (§6.4) — built in code, with no model
    call of its own.

    The chat agent never sees the photo (§8.8: the bytes reach the vision call
    and nothing else), so a model turn here could only reword a tool result it
    has no independent knowledge of — a second round trip on the slowest lane
    in the app, for nothing. Vision's `note` is already a phrase written for
    the user and carries no numbers, by contract (services/vision.INSTRUCTIONS),
    which is exactly what this needs. Same shape as `_fallback_reply`
    (services/agent.py): when the answer is what the tools did, code says it."""
    notes = [v.note.strip() or v.phrase.strip() for v in seen.items if (v.note or v.phrase).strip()]
    if not notes:
        return "I couldn't find any food in that photo — try another shot, or just tell me what you ate."
    shown = _and_list(notes[:5])
    more = f", and {len(notes) - 5} more" if len(notes) > 5 else ""
    out = [f"I see {shown}{more}."]
    searching = sum(1 for it in items if it.status == "searching")
    if searching:
        out.append(f"Still looking {'one' if searching == 1 else str(searching)} up.")
    unresolved = [it.phrase for it in items if it.estimated == "unresolved"]
    if unresolved:
        out.append(
            f"I have no database match for {_and_list(unresolved[:3])} — search for it in the list."
        )
    out.append("The amounts are my estimate, so tell me what's off.")
    return " ".join(out)


# ---------------------------------------------------------------------------
# POST /api/logs/photo
# ---------------------------------------------------------------------------


async def read_image(image: UploadFile) -> tuple[bytes, str]:
    """The upload's bytes and its sniffed type, or the refusal. Declared
    content type AND magic bytes must both pass, under the 12 MB cap; the
    sniffed type is what reaches the model. Shared with the chat lane's photo
    turn (routes/agent.py), which validates identically — one photo contract,
    one set of sentences."""
    mime = (image.content_type or "application/octet-stream").split(";")[0].strip().lower()
    if mime not in IMAGE_MIME_ALLOWLIST:
        raise HTTPException(
            415,
            f"unsupported image type {mime!r}; expected one of "
            + ", ".join(sorted(IMAGE_MIME_ALLOWLIST)),
        )
    # read one byte past the bound — the extra byte is what proves oversize
    image_bytes = await image.read(MAX_IMAGE_BYTES + 1)
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise HTTPException(413, f"image exceeds {MAX_IMAGE_BYTES} bytes")
    sniffed = sniff_image(image_bytes[:16])
    if sniffed == "image/heic":
        raise HTTPException(415, "HEIC is not accepted by the vision API — re-encode the photo as JPEG")
    if sniffed is None:
        raise HTTPException(415, "not a JPEG, PNG or WebP image")
    return image_bytes, sniffed


@router.post("/api/logs/photo", response_model=LogCreateResponse)
async def create_log_photo(
    image: Annotated[UploadFile, File()],
    log_id: Annotated[UUID, Form(alias="id")],
    captured_at: Annotated[datetime, Form()],
    meal_hint: Annotated[Meal | None, Form()] = None,
    tz: Annotated[str | None, Form()] = None,
) -> LogCreateResponse:
    """Photo → vision → the identical cascade + staging; input_mode='photo'.
    Idempotent by client id like POST /api/logs (§8.6). `tz` is optional —
    without it the meal is inferred in the settings timezone.

    The non-streaming entry. The + sheet's photo lane goes through the chat
    (POST /api/agent, §6.4) since 2026-09-08 and shares `_stage_photo` with
    this route; nothing here changed with it."""
    image_bytes, sniffed = await read_image(image)

    import tracing  # lazy: stubbed in route tests

    # §10: one trace per run, the log id as its session — the image itself is
    # never recorded, only its type and size
    async with tracing.run(
        "photo", session_id=str(log_id), tags=["lane:photo"],
        input={"mime": sniffed, "bytes": len(image_bytes)},
    ) as trace:
        response = await _stage_photo(image_bytes, sniffed, log_id, captured_at, meal_hint, tz)
        trace.set(output=response)
        return response


async def _stage_photo(
    image_bytes: bytes,
    sniffed: str,
    log_id: UUID,
    captured_at: datetime,
    meal_hint: Meal | None,
    tz: str | None,
    emit: Emit | None = None,
    grace: int = _RERUN_GRACE_SECONDS,
    where: Fix | None = None,
) -> LogCreateResponse:
    """The claim, vision, the cascade and staging — the handler's body, inside
    the run's trace, and the chat lane's photo turn (routes/agent.py) verbatim.

    `emit` is the chat's wire (§6.4): the read shows up as a `read_photo` tool
    line the moment the bytes leave for the model, and settles with what was
    seen. The image itself never rides it — its type and size, as everywhere
    else (§8.8). Without `emit` the identical path runs silently.

    `where` (2026-09-12) is the phone's location when a camera shot was taken.
    On the web lane it becomes the places around it (services/places.py), told
    to the vision call and named on the `read_photo` line, so the chat shows
    what the model was told. The coordinates go no further than that lookup.

    `grace` is the §8.6 orphan window — how long another request may plausibly
    still own this id. It is the LANE's worst case, not the route's: a chat
    photo turn's run lives in routes/agent._RUNS for _AGENT_RERUN_GRACE_SECONDS,
    so claiming its log as an orphan at 90 s would re-run vision underneath a
    run that is merely still thinking (a second astra call, and two stagings of
    one plate). routes/logs.py:94-99 states the invariant the two must satisfy."""
    deadline.start_if_unset()  # the chat route started it at the request's start
    # §8.6 + the demo quota (routes/logs.claim_log): the claim of the client
    # id answers a stored log — a replay, a foreign id's 404 — before vision,
    # and records the attempt with the row: after the free upload checks,
    # before any vision spend; it stands even when vision fails and the row
    # is dropped below (a retry under the same id is the next attempt).
    if not await claim_log(
        """
        INSERT INTO logs (id, input_mode, raw_input, captured_at)
        VALUES (%s, 'photo', '', %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (log_id, captured_at),
    ):
        replay = await _duplicate_post_response(log_id, grace=grace)
        if replay is not None:
            return replay
        # orphaned staged log claimed (earlier attempt died mid-pipeline) →
        # re-run: for a demo, the next attempt
        await quota.check_log()

    import tracing
    from openai import OpenAIError  # lazy: `import openai` is ~0.6 s of cold boot
    from services import vision  # lazy: stubbed in route tests

    tracing.init_tracing()
    call_id = f"photo-{log_id}"
    args = {"mime": sniffed, "bytes": len(image_bytes)}
    if emit is not None:
        await emit({"type": "tool_call", "id": call_id, "name": "read_photo", "args": args})
    # §8.9: the hosted search and scraper are billed per action, so the web
    # lane is the owner's, as tier 3's is. Read once — it gates both the call
    # and what may be believed about what comes back. The places lookup is
    # billed too, and a place is only worth anything to a lane that can look
    # it up, so it rides the same gate.
    web_lane = is_owner()

    async def cafe_menu() -> CafeMeal | None:
        # the owner's, like the web lane: its rows land in the shared `foods`
        return await _cafe_meal(captured_at, tz) if web_lane else None

    try:
        (context, near), cafe = await asyncio.gather(
            _photo_context(captured_at, tz, where if web_lane else None), cafe_menu()
        )
        if near and emit is not None:
            # the line went up before the lookup; the same id names the place on it
            await emit({
                "type": "tool_call", "id": call_id, "name": "read_photo",
                "args": {**args, "near": [p.name for p in near]},
            })
        # With a café meal in hand the call runs without tools: the web lane
        # cannot reach the café's numbers (script literals no page reader
        # renders) and costs 15–20 s; the menu is matched below instead.
        web = web_lane and cafe is None
        t0 = time.monotonic()
        seen = await vision.analyze(  # its span rides in this trace (§10)
            image_bytes, sniffed, context=context, web=web,
        )
    except OpenAIError as e:
        await _drop_empty_log(log_id)
        if emit is not None:
            await emit({
                "type": "tool_result", "id": call_id, "name": "read_photo",
                "output": {"error": "unavailable"},
            })
        # the client's degrade path is voice/text, not a fake empty plate
        raise HTTPException(
            503, f"photo analysis unavailable — retry, or log it by voice or text ({type(e).__name__})"
        )
    except Exception:
        await _drop_empty_log(log_id)  # any other failure: same retry path
        if emit is not None:
            await emit({
                "type": "tool_result", "id": call_id, "name": "read_photo",
                "output": {"error": "failed"},
            })
        raise
    trace_id = tracing.current_trace_id()
    vision_ms = int((time.monotonic() - t0) * 1000)
    # §3.4b café menu: which dish each item IS, chosen while the cascade and
    # tier 3 run; menu_match never raises (None is a failed call)
    matching = None
    if cafe is not None and seen.items:
        from services import menu_match  # lazy: stubbed in route tests

        matching = asyncio.create_task(menu_match.match(seen.items, cafe))

    await db.execute("UPDATE logs SET raw_input = %s WHERE id = %s", (_summary(seen), log_id))
    meal = meal_hint or seen.meal_hint or await infer_meal(captured_at, tz)

    # One row per item vision saw, same index order — tier 0 skipped: a
    # saved-meal trigger is something you SAY; the plate is its foods.
    async with tracing.run(
        "cascade", kind="retriever", input=[v.phrase for v in seen.items]
    ) as lookup:
        resolutions = [
            await cascade.resolve_item(_parsed_item(v), i, meal, skip_tier0=True)
            for i, v in enumerate(seen.items)
        ]
        lookup.set(output=cascade_summary(resolutions))
    for r, v in zip(resolutions, seen.items):
        # A COUNT vision gave ("11 shrimp") lands on the count rule, which
        # needs a count-type serving to divide by. The cached shrimp record
        # carries only "100 g", so the count fell through as a multiplier of
        # the mass default and 11 shrimp committed as 11 x 100 g — 1,089 kcal
        # and 264 g of protein in Bill's diary, 2026-09-09. `_size_by_weight`
        # exists for exactly this and `_stage_estimate` has always called it;
        # this lane never did, and `_parsed_item` drops est_grams whenever a
        # count is present, so the 90 g vision actually measured was thrown
        # away before sizing.
        #
        # BOTH of routes/logs._stage_estimate's branches are needed, and the
        # first is the one that matters here: a photo row that needs tier 3
        # leaves this loop as `searching` with serving=None, which is what
        # every row of the shrimp plate was. Handling only the resolved case
        # would leave the count on the spoken channel for tier 3 to multiply
        # again — the original bug, untouched. So a still-searching row hands
        # tier 3 the WEIGHT instead of the count, and a row already resolved
        # onto a non-count serving is re-sized from that weight.
        grams = _grams(v)
        counted = _count(v) is not None and grams is not None and grams > 0
        if counted and r.staged.status == "searching":
            r.staged = r.staged.model_copy(update={"qty": float(grams)})
            r.unit = "g"
        elif (
            counted
            and r.staged.status == "resolved"
            and r.staged.serving is not None
            and not cascade._is_count_serving(r.staged.serving.description)
        ):
            r.staged = _size_by_weight(r.staged, float(grams))
        r.staged = _flag(r.staged, v)
        # a published dish beats whatever the cascade guessed from its name:
        # the kitchen's own numbers for its own dish (§3.4a, 2026-09-09).
        # Only when the web lane actually ran: without tools there is no page
        # to have read, so a `menu` block is a fabrication — and `foods` is
        # the shared table (§8.9), where a demo account must not be able to
        # plant a branded row with numbers nobody published.
        if web:
            r.staged = await _stage_menu_food(r.staged, v)
    await _persist(
        log_id, resolutions, meal,
        short_circuit=False, decompose_ms=vision_ms, trace_id=trace_id,
    )
    if emit is not None:
        await emit({
            "type": "tool_result", "id": call_id, "name": "read_photo",
            "output": {"seen": [v.note.strip() or v.phrase for v in seen.items]},
        })
    menu_id = f"menu-{log_id}"
    if emit is not None and matching is not None:
        await emit({
            "type": "tool_call", "id": menu_id, "name": "read_menu",
            "args": {"meal": cafe.label, "site": cafe.site, "date": cafe.day.isoformat()},
        })
    # The chat turn's reply is this lane's own, built in code (photo_reply);
    # POST /api/logs/photo answers without one, as it always has.
    def outcome(items: list[StagedItem]) -> AgentOutcome | None:
        return AgentOutcome(reply=photo_reply(seen, items)) if emit is not None else None

    if not seen.items:
        # Vision ran and saw nothing. The attempt is real and adherence-relevant
        # (§5), so the log is DISCARDED, not deleted the way a vision *failure*
        # drops its row — and it must not be left staged, which is what the old
        # client's discardLog() call prevented before this lane moved into the
        # chat. In the chat the reply already says it; `note` would be a second,
        # identical bubble (agentDone renders it), so only the plain route gets
        # one.
        await db.execute(
            "UPDATE logs SET status = 'discarded' WHERE id = %s AND status = 'staged'",
            (str(log_id),),
        )
        return LogCreateResponse(
            log_id=log_id, meal=meal, items=[], agent=outcome([]),
            note=None if emit is not None
            else "No food recognized in the photo — try another shot, or log it by voice or text.",
        )
    # the raw vision output per row (eval loop) — an extra key like spoken_qty
    async with db.tx() as conn:
        for r, v in zip(resolutions, seen.items):
            await conn.execute(
                "UPDATE item_resolutions "
                f"SET staged = staged || jsonb_build_object('vision', %s::jsonb){KEEP_LOG_KEYS_SQL} "
                "WHERE log_id = %s AND item_index = %s",
                (
                    Json({**v.model_dump(mode="json"), "model": vision.MODEL}),
                    log_id,
                    r.staged.item_index,
                ),
            )
    staged = [r.staged for r in resolutions]
    # §3.4 (2026-09-11): every row is looked up before the card is shown —
    # after the vision stamp (the not_found → estimate seam reads it) and
    # before the reply, which says what was found, not "still looking"
    pending = [it for it in staged if it.status == "searching"]
    if pending and _inline_enabled():
        look_id = f"lookup-{log_id}"
        if emit is not None:
            await emit({"type": "tool_call", "id": look_id, "name": "look_up",
                        "args": {"items": [it.phrase for it in pending]}})
        staged = await _resolve_inline(log_id, staged, reserve=_PHOTO_RESERVE_S)
        if emit is not None:
            looked = {p.item_index for p in pending}
            # a numberless placeholder (§3.4b rule 2) is "resolved" with no
            # database food — not found
            found = sum(1 for it in staged if it.item_index in looked
                        and it.status == "resolved" and it.estimated != "unresolved")
            await emit({"type": "tool_result", "id": look_id, "name": "look_up",
                        "output": {"found": found, "looked_up": len(pending)}})
    if matching is not None:
        picks = await matching
        staged, used, aside = await _stage_cafe_rows(log_id, staged, seen, cafe, picks or {})
        if emit is not None:
            await emit({
                "type": "tool_result", "id": menu_id, "name": "read_menu",
                "output": {"error": "unavailable"} if picks is None
                else {"matched": used, "set_aside": aside},
            })
    return LogCreateResponse(log_id=log_id, meal=meal, items=staged, agent=outcome(staged))


# ---------------------------------------------------------------------------
# The unresolvable item (§3.4a): an estimate row — WITHOUT numbers
# ---------------------------------------------------------------------------


async def stage_estimate_food(log_id: str, item_index: int) -> StagedItem | None:
    """Tier 3 found nothing for a photo row: persist what vision saw as a
    foods row with source='estimate' and provenance = the vision output —
    no macros, because the model must not invent calories — and stage it
    resolved, flagged estimated="unresolved", search_instead, numbers absent.
    Commit can never take it (core-macros guard); a search pick swaps a
    database food in, and KEEP_LOG_KEYS_SQL downgrades the flag to "portion".

    Seam: the tier-3 kick (routes/resolve.py) or run_tier3's not_found branch
    calls this when the row's staged jsonb carries `vision`. Returns None for
    a non-photo row or a row no longer live."""
    row = await db.fetchone(
        "SELECT staged, status, committed_at FROM item_resolutions "
        "WHERE log_id = %s AND item_index = %s",
        (str(log_id), item_index),
    )
    if row is None:
        return None
    staged = row["staged"] if isinstance(row["staged"], dict) else {}
    seen = staged.get("vision")
    if (
        not isinstance(seen, dict)
        or row["committed_at"] is not None
        or row["status"] not in _LIVE_STATUSES
    ):
        return None
    log = await db.fetchone("SELECT captured_at FROM logs WHERE id = %s", (str(log_id),))
    phrase = staged.get("phrase") or seen.get("phrase") or "unidentified food"
    grams = seen.get("est_grams")
    grams = float(grams) if isinstance(grams, (int, float)) and grams > 0 else None
    serving = {
        "serving_id": "photo",
        "description": f"~{grams:g} g (photo estimate)" if grams else "1 portion (photo estimate)",
        "grams": grams,
        "macros": {},  # deliberately empty: no numbers exist for this row
    }
    food_id = await foods_repo.persist_food(
        {
            "source": "estimate",
            "source_id": f"{log_id}:{item_index}",
            "source_url": None,
            "provenance": {
                "vision": seen,
                "model": seen.get("model"),
                "captured_at": log["captured_at"].isoformat() if log else None,
                "log_id": str(log_id),
                "item_index": item_index,
            },
            "name": phrase,
            "display_name": phrase,
            "brand": None,
            "food_type": "generic",
            "state": None,
            "state_sensitive": False,
            "servings": [serving],
            "default_serving_id": "photo",
            "per_100g": None,
            "payload": {"vision": seen},
        }
    )
    item = StagedItem(
        item_index=item_index,
        phrase=phrase,
        status="resolved",
        tier=None,
        food=FoodRef(id=food_id, name=phrase),
        serving=ServingRef(id="photo", description=serving["description"]),
        qty=1.0,
        macros=None,
        meal=Meal(staged.get("meal") or "snack"),
        confidence="low",
        servings=[ServingOption(**s) for s in cascade._serving_options([serving])],
        search_instead=True,
        estimated="unresolved",
        estimate_note=(
            f"Photo: {seen.get('note') or phrase} — no database match; "
            "search to pick a food and get its numbers"
        ),
    )
    payload = item.model_dump(mode="json", exclude_none=True)
    # keep the estimate alive for the search pick (§7.3 rule 1 channel)
    payload["spoken_qty"] = staged.get("spoken_qty")
    payload["spoken_unit"] = staged.get("spoken_unit")
    # KEEP carries the old flag ("portion") AND the old note over the payload;
    # this row IS the unresolved estimate, so both are re-applied after it,
    # and the response is the row as stored.
    row = await db.fetchone(
        f"""
        UPDATE item_resolutions
        SET status = 'resolved',
            staged = %s::jsonb{KEEP_LOG_KEYS_SQL}
                     || jsonb_build_object('estimated', 'unresolved'::text,
                                           'estimate_note', %s::text)
        WHERE log_id = %s AND item_index = %s AND {_LIVE_SQL}
        RETURNING staged
        """,
        (Json(payload), item.estimate_note, str(log_id), item_index),
    )
    return StagedItem.model_validate(row["staged"]) if row else None


# ---------------------------------------------------------------------------
# adjust_photo (§6.4, 2026-09-08): the chat tunes what the photo staged
#
# The photo lane is the front of a chat now, so "that's two eggs, not one" has
# to reach the row the photo already staged. It edits in place — the plate
# keeps what vision saw (`staged->'vision'`, the eval loop's record), keeps its
# §3.4a flag, and keeps every row the correction did not touch. Restaging the
# whole plate through log_food/log_estimate would throw all of that away to
# change one amount, which is why the agent is told not to (services/agent.py).
# ---------------------------------------------------------------------------


def _row_words(staged: dict) -> list[str]:
    """What the conversation calls one row: its food's name and its phrase."""
    food = staged.get("food") or {}
    return [n.strip().lower() for n in (food.get("name"), staged.get("phrase")) if n]


def _row_label(staged: dict) -> str:
    food = staged.get("food") or {}
    return food.get("name") or staged.get("phrase") or "?"


def _match_row(rows: list[dict], item: str) -> int | dict:
    """The one row `item` names — its food name or phrase, exact first, else a
    substring, case-insensitive — or an error listing what is on the plate.
    Same rule and the same refusal shape as routes/logs._match_meal_item."""
    key = item.strip().lower()
    hits = [i for i, r in enumerate(rows) if key in _row_words(r["staged"])] or [
        i for i, r in enumerate(rows)
        if key and any(key in w for w in _row_words(r["staged"]))
    ]
    if len(hits) == 1:
        return hits[0]
    return {
        "error": "ambiguous_item" if hits else "unknown_item",
        "message": f"'{item}' names {len(hits)} of the plate's rows — use one of these",
        "items": [_row_label(r["staged"]) for r in rows],
    }


async def _apply_amount(item: StagedItem, change: PhotoChange) -> StagedItem:
    """The corrected amount on the row's own servings. `cascade.resolve_portion`
    does the mapping — its rule 1, the §7.3 spoken-quantity channel, which is
    the identical path a spoken "two cups of rice" takes, including the
    count-serving divisor (`_pieces_per`) and the gram-serving choice. Only
    rule 1 can run here: the person always states an amount. Macros are
    recomputed from the food's own cached numbers; nothing is invented."""
    servings = [s.model_dump() for s in (item.servings or [])]
    if item.status != "resolved" or item.estimated == "unresolved" or not servings:
        # Nothing real to map onto. Either tier 3 is still looking, or this is
        # the numberless placeholder (§3.4b rule 2) whose only "serving" is the
        # photo's own guess with an empty macros block — sizing against that
        # would dress a row with no food up as a measured portion. The amount
        # rides the §7.3 spoken channel instead, which is what the search pick
        # reads when a database food finally replaces it (rule 4).
        return item.model_copy(update={"qty": float(change.grams or change.qty)})
    default_id = item.serving.id if item.serving else servings[0]["serving_id"]
    serving_id, qty, verify_me = await cascade.resolve_portion(
        {"servings": servings, "food_default_serving_id": default_id},
        float(change.qty),
        change.unit,
    )
    chosen = next((s for s in item.servings if s.serving_id == serving_id), None)
    if (
        change.unit is None
        and change.grams
        and chosen is not None
        and not cascade._is_count_serving(chosen.description)
    ):
        # a count of pieces on a food whose servings are all mass ("100 g"):
        # size it by the stated weight rather than pieces × the mass default,
        # exactly as the agent's own estimate rows are sized
        return _size_by_weight(item, float(change.grams))
    if chosen is None:
        return item.model_copy(update={"qty": qty, "confidence": "low"})
    macros = _macros_for(chosen, qty)
    return item.model_copy(
        update={
            "serving": ServingRef(id=chosen.serving_id, description=chosen.description),
            "qty": qty,
            "macros": macros,
            # the amount is now theirs, not the photo's, but it is still a
            # stated guess about a plate — never "high"
            "confidence": "low" if (verify_me or macros is None) else "medium",
        }
    )


async def adjust_rows(
    log_id: UUID, changes: list[PhotoChange], body: LogCreate
) -> dict:
    """The `adjust_photo` tool's effect (routes/logs._run_agent_lane closes
    over it): apply the corrections to one staged photo log and answer with its
    rows as they now stand.

    Every write goes through the same guards the rest of the lane uses — the
    live-row `WHERE` (`_LIVE_SQL`: never a removed row, never a committed one)
    and `KEEP_LOG_KEYS_SQL` on every `staged` rewrite (BACKLOG 57), with the
    flag and the new note re-applied *after* it, as `stage_estimate_food` does,
    because KEEP carries the row's OLD note over the payload."""
    log = await db.fetchone("SELECT status FROM logs WHERE id = %s", (str(log_id),))
    if log is None:
        return {"error": "unknown_log", "message": "that plate is not here"}
    if log["status"] != "staged":
        return {
            "error": "not_staged",
            "message": f"that plate is {log['status']}, not staged — it cannot be changed here; "
                       "if it is committed, say it is in the diary and can be edited there",
        }
    rows = await db.fetchall(
        "SELECT item_index, staged FROM item_resolutions "
        f"WHERE log_id = %s AND {_LIVE_SQL} ORDER BY item_index",
        (str(log_id),),
    )
    rows = [r for r in rows if isinstance(r["staged"], dict)]
    added: list = []
    done: list[str] = []
    # Resolve EVERY change to its row before writing any of them. A name that
    # matches nothing (or two things) is the likeliest failure here — the model
    # is naming rows from words — and applying the first two of three changes
    # and then answering with a bare error would leave the plate in a state
    # nobody asked for, which the model cannot see and would retry over.
    resolved: list[tuple[int, PhotoChange]] = []
    seen_rows: set[int] = set()
    for change in changes:
        if change.op == "add":
            added.append(change)
            continue
        hit = _match_row(rows, change.item)
        if isinstance(hit, dict):
            return hit
        if hit in seen_rows:
            return {
                "error": "repeated_item",
                "message": f"'{change.item}' is changed twice in one call — say the final amount once",
            }
        seen_rows.add(hit)
        resolved.append((hit, change))

    for hit, change in resolved:
        row = rows[hit]
        label = _row_label(row["staged"])
        if change.op == "remove":
            await db.execute(
                "UPDATE item_resolutions SET status = 'removed' "
                f"WHERE log_id = %s AND item_index = %s AND {_LIVE_SQL}",
                (str(log_id), row["item_index"]),
            )
            done.append(f"removed {label}")
            continue
        item = StagedItem.model_validate(row["staged"])
        updated = await _apply_amount(item, change)
        # §3.4b rule 4: the flag only ever downgrades, and "unresolved" becomes
        # "portion" only when the row gains a DATABASE food. Correcting the
        # amount does not give it one — stamping "portion" here would erase the
        # one thing that tells the user (and StagedRow) this row still has no
        # match and no numbers, taking its "search to pick a food" note with it.
        unresolved = item.estimated == "unresolved"
        flag = "unresolved" if unresolved else "portion"
        note = f"You said: {change.note.strip() or 'amount corrected'}"
        if unresolved:
            note += " — still no database match; search to pick a food and get its numbers"
        payload = updated.model_dump(mode="json", exclude_none=True)
        # The §7.3 spoken channel, the way stage_estimate_food carries it: a
        # full payload rewrite drops these extras and KEEP_LOG_KEYS_SQL does
        # not carry them, so they are re-applied here. A row with nothing to
        # map onto — tier 3 still looking, or the numberless placeholder, the
        # same two cases _apply_amount returns early for — gets the CORRECTED
        # amount, because that is what tier 3 or the search pick will size the
        # food it finds by. A resolved row keeps what the photo first said.
        if updated.status != "resolved" or unresolved:
            payload["spoken_qty"] = updated.qty
            payload["spoken_unit"] = change.unit or ("g" if change.grams else None)
        else:
            payload["spoken_qty"] = row["staged"].get("spoken_qty")
            payload["spoken_unit"] = row["staged"].get("spoken_unit")
        await db.execute(
            "UPDATE item_resolutions "
            f"SET staged = %s::jsonb{KEEP_LOG_KEYS_SQL} "
            "|| jsonb_build_object('estimated', %s::text, 'estimate_note', %s::text), "
            # the amount is the person's now, not the resolver's proposal —
            # the same column a portion edit in the sheet sets (§3.5, §9's
            # edit rate). proposed_* stay: they mean "what was first shown".
            "was_edited = TRUE "
            f"WHERE log_id = %s AND item_index = %s AND {_LIVE_SQL}",
            (Json(payload), flag, note, str(log_id), row["item_index"]),
        )
        done.append(f"{label} → {updated.qty:g} × {updated.serving.description}"
                    if updated.serving else f"{label} updated")
    if added:
        from services.agent import EstimateItem

        next_index = await db.fetchone(
            "SELECT coalesce(max(item_index), -1) + 1 AS n FROM item_resolutions WHERE log_id = %s",
            (str(log_id),),
        )
        meal_row = await db.fetchone(
            "SELECT meal_inferred FROM logs WHERE id = %s", (str(log_id),)
        )
        meal = Meal(meal_row["meal_inferred"]) if meal_row and meal_row["meal_inferred"] else Meal("snack")
        staged = await _stage_estimate(
            body.model_copy(update={"id": log_id}),
            meal,
            [
                EstimateItem(
                    phrase=c.item, qty=float(c.qty), unit=c.unit, grams=c.grams,
                    note=c.note.strip() or "the photo missed it",
                )
                for c in added
            ],
            start_index=int(next_index["n"]),
        )
        done.extend(f"added {it.food.name if it.food else it.phrase}" for it in staged)
    current = await _load_log_response(log_id)
    items = current.items if current is not None else []
    return {
        "changed": done,
        # the plate as it now stands, for the reply — never the numbers
        "items": [
            {
                "phrase": it.phrase,
                "status": it.status,
                "food": it.food.name if it.food else None,
                "qty": it.qty,
                "serving": it.serving.description if it.serving else None,
            }
            for it in items
        ],
        "note": "Every corrected row stays flagged as an estimate — the user adjusts it in the list or in chat.",
    }
