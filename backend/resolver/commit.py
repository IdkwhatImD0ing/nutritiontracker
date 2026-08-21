"""Commit — the learning writes (DESIGN.md §3.5), idempotent by construction.

One interactive transaction: CAS on logs.status → entries insert (macros
snapshotted from the foods cache; UNIQUE(log_id, item_index) tolerated
idempotently) → alias upsert with explicit-choice semantics + rejected_count →
portion-default recompute (mode of last 10) → per-item committed_at +
edited_items. Trace scores (tracing.score_commit) post after the transaction.

M0 seams: step 4 (meal-macro save_intent) is M3; tier-0 skip rules are wired
(tier == 0 never writes aliases or portion defaults) but no tier-0 rows exist
yet.
"""

from __future__ import annotations

from collections import Counter
from uuid import UUID

from psycopg.types.json import Json

import db
from models import StagedItem
from resolver.norm import norm


class LogNotFound(Exception):
    pass


class LogNotCommittable(Exception):
    """Log exists but is in a state the operation can't act on."""


def _scale_macros(macros: dict, qty: float) -> dict:
    return {
        k: round(float(v) * qty, 2)
        for k, v in macros.items()
        if isinstance(v, (int, float))
    }


def _find_serving(servings: list[dict], serving_id: str | None) -> dict | None:
    for s in servings:
        if s.get("serving_id") == serving_id:
            return s
    return None


async def commit_log(log_id: UUID, items: list[StagedItem]) -> dict:
    """§3.5. Returns {"log": row, "replayed": bool, "scores": [...]}.

    Raises LogNotFound / LogNotCommittable (routes map to 404 / 409).
    """
    scores: list[dict] = []
    async with db.tx() as conn:
        # 0. compare-and-set — a retried commit must never double-insert
        cur = await conn.execute(
            "UPDATE logs SET status='committed', committed_at=now() "
            "WHERE id = %s AND status = 'staged'",
            (log_id,),
        )
        log = await (
            await conn.execute("SELECT * FROM logs WHERE id = %s", (log_id,))
        ).fetchone()
        if log is None:
            raise LogNotFound(str(log_id))
        if cur.rowcount == 0:
            if log["status"] == "committed":
                return {"log": log, "replayed": True, "scores": []}
            raise LogNotCommittable(f"log is {log['status']}")

        res_rows = {
            r["item_index"]: r
            for r in await (
                await conn.execute(
                    "SELECT * FROM item_resolutions WHERE log_id = %s", (log_id,)
                )
            ).fetchall()
        }

        edited_count = 0
        for item in items:
            if item.status != "resolved" or item.food is None:
                continue  # searching/needs_input/not_found stay uncommitted (§3.5)
            outcome = await _commit_one(conn, log, item, res_rows.get(item.item_index))
            edited_count += 1 if outcome["was_edited"] else 0
            scores.append(outcome)

        # Rows the user removed in staging are ABSENT FROM THE PAYLOAD entirely —
        # the client submits every non-removed row whatever its status. A row
        # present as 'searching' (client hadn't seen the server-side resolution
        # yet) must stay uncommitted so the §3.5 commit-item late path works.
        payload_indexes = {item.item_index for item in items}
        for idx, r in res_rows.items():
            if (
                idx not in payload_indexes
                and r["committed_at"] is None
                and r["status"] != "removed"
            ):
                await conn.execute(
                    "UPDATE item_resolutions SET status='removed' WHERE id = %s",
                    (r["id"],),
                )

        await conn.execute(
            "UPDATE logs SET edited_items = %s WHERE id = %s", (edited_count, log_id)
        )
        log = dict(log, status="committed", edited_items=edited_count)

    _post_scores(log, scores)
    return {"log": log, "replayed": False, "scores": scores}


async def commit_item(log_id: UUID, item: StagedItem) -> dict:
    """§3.5 late-resolving items: POST /api/logs/:id/commit-item appends one
    item to an already-committed log — same per-item transaction, same
    idempotency (a replay returns the stored result without re-learning)."""
    scores: list[dict] = []
    async with db.tx() as conn:
        log = await (
            await conn.execute("SELECT * FROM logs WHERE id = %s", (log_id,))
        ).fetchone()
        if log is None:
            raise LogNotFound(str(log_id))
        if log["status"] != "committed":
            raise LogNotCommittable(f"log is {log['status']}, not committed")
        if item.status != "resolved" or item.food is None:
            raise LogNotCommittable("item is not resolved")
        res = await (
            await conn.execute(
                "SELECT * FROM item_resolutions WHERE log_id = %s AND item_index = %s",
                (log_id, item.item_index),
            )
        ).fetchone()
        if res is not None and res["committed_at"] is not None:
            return {"log": log, "replayed": True, "scores": []}
        outcome = await _commit_one(conn, log, item, res)
        scores.append(outcome)

    _post_scores(log, scores)
    return {"log": log, "replayed": False, "scores": scores}


async def _commit_one(conn, log: dict, item: StagedItem, res: dict | None) -> dict:
    """Steps 1–3 + 5 of §3.5 for one item, inside the open transaction."""
    food = await (
        await conn.execute("SELECT * FROM foods WHERE id = %s", (item.food.id,))
    ).fetchone()
    if food is None:
        raise LogNotCommittable(f"unknown food {item.food.id}")

    qty = float(item.qty) if item.qty is not None else 1.0
    serving = _find_serving(food["servings"], item.serving.id if item.serving else None)
    if serving is None:  # dangling id never produces a zero-macro entry (§4)
        serving = _find_serving(food["servings"], food["default_serving_id"])
        if serving is None:
            raise LogNotCommittable(f"food {item.food.id} has no usable serving")
    serving_id = serving["serving_id"]

    # 1. entry — macros snapshotted from the foods cache (per-serving × qty)
    macros = _scale_macros(serving["macros"], qty)
    grams = (
        round(float(serving["grams"]) * qty, 2)
        if isinstance(serving.get("grams"), (int, float))
        else None
    )

    tier = res["tier"] if res is not None else None
    proposed_food_id = res["proposed_food_id"] if res is not None else None
    # §3.5 step 2: "explicit" = the user actively chose this food — he switched
    # away from the pre-selection, OR he answered a disambiguation (chip tap /
    # question option). Both flip pick()'s rule-2 default via last_explicit_at.
    switched = proposed_food_id is not None and str(proposed_food_id) != item.food.id
    disambiguated = bool(res["was_disambiguated"]) if res is not None else False
    explicit = switched or disambiguated
    was_edited = switched or (
        res is not None
        and res["proposed_serving_id"] is not None
        and (
            res["proposed_serving_id"] != serving_id
            or (res["proposed_qty"] is not None and float(res["proposed_qty"]) != qty)
        )
    )

    # 2. alias — for each item except tier-0 expansions and removed rows.
    # Learned for the ORIGINAL spoken phrase (res["phrase"]); rejected_count
    # lands on the abandoned pre-selection only when the user actually switched.
    alias_id = None
    if tier != 0:
        phrase = res["phrase"] if res is not None else item.phrase
        alias_id = await _learn_alias(
            conn,
            phrase=phrase,
            food_id=item.food.id,
            serving_id=serving_id,
            qty=qty,
            explicit=explicit,
            proposed_food_id=proposed_food_id if switched else None,
            tier=tier,
        )

    await conn.execute(
        """
        INSERT INTO entries (logged_at, meal, food_id, serving_id, qty, grams, macros,
                             log_id, item_index, alias_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (log_id, item_index) DO NOTHING
        """,
        (
            log["captured_at"],
            item.meal.value,
            item.food.id,
            serving_id,
            qty,
            grams,
            Json(macros),
            log["id"],
            item.item_index,
            alias_id,
        ),
    )

    # telemetry: added/edited rows must not be telemetry ghosts (§5)
    if res is None:
        await conn.execute(
            """
            INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm,
                                          status, staged, tier, final_food_id,
                                          final_serving_id, final_qty, alias_id,
                                          was_edited, committed_at)
            VALUES (%s, %s, %s, %s, 'resolved', %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (log_id, item_index) DO NOTHING
            """,
            (
                log["id"],
                item.item_index,
                item.phrase,
                norm(item.phrase),
                Json(item.model_dump(mode="json")),
                tier,
                item.food.id,
                serving_id,
                qty,
                alias_id,
                was_edited,
            ),
        )
    else:
        await conn.execute(
            """
            UPDATE item_resolutions
            SET status='resolved', staged=%s, final_food_id=%s, final_serving_id=%s,
                final_qty=%s, alias_id=%s, was_edited=%s, committed_at=now()
            WHERE id = %s
            """,
            (
                Json(item.model_dump(mode="json")),
                item.food.id,
                serving_id,
                qty,
                alias_id,
                was_edited,
                res["id"],
            ),
        )

    # 3. portion default: mode of the last 10 committed portions for this alias
    if alias_id is not None and tier != 0:
        await _recompute_alias_default(conn, alias_id)

    return {
        "item_index": item.item_index,
        "was_edited": was_edited,
        "was_disambiguated": disambiguated,
        "tier": tier,
        "trace_id": res["trace_id"] if res is not None else None,
    }


async def _learn_alias(
    conn,
    *,
    phrase: str,
    food_id: str,
    serving_id: str,
    qty: float,
    explicit: bool,
    proposed_food_id,
    tier,
) -> str:
    """Upsert (alias_norm(phrase) → final food_id) with explicit-choice
    semantics; the final food after any user edits is what gets learned."""
    alias_norm = norm(phrase)
    if explicit and proposed_food_id is not None:
        # negative evidence on the pre-selection he switched away from
        await conn.execute(
            "UPDATE aliases SET rejected_count = rejected_count + 1 "
            "WHERE alias_norm = %s AND food_id = %s",
            (alias_norm, proposed_food_id),
        )
    origin = "tier3" if tier == 3 else ("manual" if tier is None else "edit")
    row = await (
        await conn.execute(
            """
            INSERT INTO aliases (alias_norm, food_id, default_serving_id, default_qty,
                                 origin, use_count, last_used_at, last_explicit_at)
            VALUES (%s, %s, %s, %s, %s, 1, now(), CASE WHEN %s THEN now() END)
            ON CONFLICT (alias_norm, food_id) DO UPDATE
            SET use_count = aliases.use_count + 1,
                last_used_at = now(),
                last_explicit_at = COALESCE(EXCLUDED.last_explicit_at,
                                            aliases.last_explicit_at)
            RETURNING id
            """,
            (alias_norm, food_id, serving_id, qty, origin, explicit),
        )
    ).fetchone()
    return str(row["id"])


async def _recompute_alias_default(conn, alias_id: str) -> None:
    hist = await (
        await conn.execute(
            """
            SELECT final_serving_id, final_qty FROM item_resolutions
            WHERE alias_id = %s AND committed_at IS NOT NULL
              AND final_serving_id IS NOT NULL AND final_qty IS NOT NULL
            ORDER BY committed_at DESC LIMIT 10
            """,
            (alias_id,),
        )
    ).fetchall()
    pairs = [(h["final_serving_id"], float(h["final_qty"])) for h in hist]
    if not pairs:
        return
    counts = Counter(pairs)
    top = max(counts.values())
    tied = {p for p, c in counts.items() if c == top}
    for p in pairs:  # tie → most recent
        if p in tied:
            await conn.execute(
                "UPDATE aliases SET default_serving_id = %s, default_qty = %s WHERE id = %s",
                (p[0], p[1], alias_id),
            )
            return


def _post_scores(log: dict, scores: list[dict]) -> None:
    """§3.5 step 5 / §10: post outcome scores onto the stored trace ids.
    Best-effort — tracing failure must never fail a commit."""
    if not scores:
        return
    try:
        import tracing

        for s in scores:
            for trace_id in {log.get("trace_id"), s.get("trace_id")} - {None}:
                tracing.score_commit(
                    trace_id,
                    "user_edited",
                    1 if s["was_edited"] else 0,
                    comment=f"item {s['item_index']}",
                )
                tracing.score_commit(
                    trace_id,
                    "was_disambiguated",
                    1 if s.get("was_disambiguated") else 0,
                    comment=f"item {s['item_index']}",
                )
                if s["tier"] is not None:
                    tracing.score_commit(
                        trace_id, "tier", int(s["tier"]), comment=f"item {s['item_index']}"
                    )
        tracing.flush()
    except Exception:
        pass
