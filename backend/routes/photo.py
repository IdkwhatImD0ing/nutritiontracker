"""POST /api/logs/photo + POST /api/resolve/confirm-estimate (DESIGN.md §3.4a, §12 M4).

The photo lane — the one place estimation is permitted, and only for what a
photo cannot tell us: portions, and items no database resolves.

  photo → services/vision.analyze (gpt-5.6-luna, structured: the foods seen
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

Nothing estimated commits without an explicit confirm: confirm-estimate sets
staged.estimate_confirmed server-side and resolver/commit.py refuses (409)
while any estimated row lacks it. The image bytes are never persisted; each
row's `vision` jsonb key keeps the raw structured output for the eval loop.
Photo rows learn no aliases and no portion defaults (commit.py reads
logs.input_mode).
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from psycopg.types.json import Json
from pydantic import BaseModel

import db
from auth import require_user
from models import FoodRef, Macros, Meal, ParsedItem, ServingOption, ServingRef, StagedItem
from resolver import cascade, foods_repo
from resolver.commit import KEEP_LOG_KEYS_SQL
from routes.logs import (
    LogCreateResponse,
    _drop_empty_log,
    cascade_summary,
    _duplicate_post_response,
    _persist,
    claim_log,
    infer_meal,
)
from services import quota

if TYPE_CHECKING:
    from services.vision import VisionItem, VisionResult

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

# Same live-row guard as tier3._write_item_row (BACKLOG 27): a confirm or an
# estimate-row write never resurrects a removed row or clobbers a committed one.
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


# ---------------------------------------------------------------------------
# POST /api/logs/photo
# ---------------------------------------------------------------------------


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
    without it the meal is inferred in the settings timezone."""
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
) -> LogCreateResponse:
    """The claim, vision, the cascade and staging — the handler's body, inside
    the run's trace."""
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
        replay = await _duplicate_post_response(log_id)
        if replay is not None:
            return replay
        # orphaned staged log claimed (earlier attempt died mid-pipeline) →
        # re-run: for a demo, the next attempt
        await quota.check_log()

    import tracing
    from openai import OpenAIError  # lazy: `import openai` is ~0.6 s of cold boot
    from services import vision  # lazy: stubbed in route tests

    tracing.init_tracing()
    t0 = time.monotonic()
    try:
        seen = await vision.analyze(image_bytes, sniffed)  # its span rides in this trace (§10)
    except OpenAIError as e:
        await _drop_empty_log(log_id)
        # the client's degrade path is voice/text, not a fake empty plate
        raise HTTPException(
            503, f"photo analysis unavailable — retry, or log it by voice or text ({type(e).__name__})"
        )
    except Exception:
        await _drop_empty_log(log_id)  # any other failure: same retry path
        raise
    trace_id = tracing.current_trace_id()
    vision_ms = int((time.monotonic() - t0) * 1000)

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
        r.staged = _flag(r.staged, v)
    await _persist(
        log_id, resolutions, meal,
        short_circuit=False, decompose_ms=vision_ms, trace_id=trace_id,
    )
    if not seen.items:
        return LogCreateResponse(
            log_id=log_id, meal=meal, items=[],
            note="No food recognized in the photo — try another shot, or log it by voice or text.",
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
    return LogCreateResponse(log_id=log_id, meal=meal, items=[r.staged for r in resolutions])


# ---------------------------------------------------------------------------
# The unresolvable item (§3.4a): an estimate row — WITHOUT numbers
# ---------------------------------------------------------------------------


async def stage_estimate_food(log_id: str, item_index: int) -> StagedItem | None:
    """Tier 3 found nothing for a photo row: persist what vision saw as a
    foods row with source='estimate' and provenance = the vision output —
    no macros, because the model must not invent calories — and stage it
    resolved, flagged estimated="unresolved", search_instead, numbers absent.
    Commit can never take it (core-macros guard) and confirm-estimate refuses
    it; a search pick swaps a database food in, and KEEP_LOG_KEYS_SQL
    downgrades the flag to "portion".

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
# POST /api/resolve/confirm-estimate — the explicit confirm (§3.4a)
# ---------------------------------------------------------------------------


class ConfirmEstimateRequest(BaseModel):
    log_id: UUID
    item_index: int


@router.post("/api/resolve/confirm-estimate", response_model=StagedItem)
async def confirm_estimate(body: ConfirmEstimateRequest) -> StagedItem:
    """Set staged.estimate_confirmed on one live estimated row. Editing the
    portion never confirms; only this does. A row without numbers (an
    estimate food) has nothing to confirm — search first."""
    row = await db.fetchone(
        "SELECT staged, status, committed_at FROM item_resolutions "
        "WHERE log_id = %s AND item_index = %s",
        (str(body.log_id), body.item_index),
    )
    if row is None:
        raise HTTPException(404, "unknown log item")
    if row["committed_at"] is not None:
        raise HTTPException(409, "item already committed")
    if row["status"] not in _LIVE_STATUSES:
        raise HTTPException(409, "item already removed")
    staged = row["staged"] if isinstance(row["staged"], dict) else {}
    if (staged.get("estimated") or "none") == "none":
        raise HTTPException(409, "row carries no estimate")
    if staged.get("status") != "resolved" or not staged.get("macros"):
        raise HTTPException(409, "nothing to confirm yet — pick a database food first")
    updated = await db.execute(
        "UPDATE item_resolutions "
        f"SET staged = (staged || jsonb_build_object('estimate_confirmed', true)){KEEP_LOG_KEYS_SQL} "
        f"WHERE log_id = %s AND item_index = %s AND {_LIVE_SQL}",
        (str(body.log_id), body.item_index),
    )
    if not updated:
        raise HTTPException(409, "item no longer live")
    return StagedItem.model_validate({**staged, "estimate_confirmed": True})
