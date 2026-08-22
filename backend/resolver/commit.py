"""Commit — the learning writes (DESIGN.md §3.5), idempotent by construction.

One interactive transaction: CAS on logs.status → core-macros guard (partial
macros refuse the item as needs_input, never a partial snapshot) → entries
insert (macros snapshotted from the foods cache; UNIQUE(log_id, item_index)
tolerated idempotently) → alias upsert with explicit-choice semantics +
rejected_count → portion-default recompute (mode of last 10) → per-item
committed_at + edited_items. Trace scores post after the transaction, batched
into one Langfuse flush per commit (§11).

Tier-0 expansions (§3.1) never write aliases or portion defaults — the macro
is the memory. Step 4 (meal-macro save_intent): decompose's save_intent rides
the staged jsonb (`save_intent`, same extra-key channel as spoken_qty) or the
caller passes one explicitly; the macro is created/updated from the items
this commit wrote, trigger phrases stored post-norm(). save_meal_macro() is
shared with routes/meals.py (the "Save as meal" tap).

§7.2 / §9 (Phase-4 fix): logs.captured_at is the immutable instrumentation
anchor. A when-hint's shifted diary time rides every item's staged jsonb as
`logged_at` (written by routes/logs.py _persist) and is read back here for
entries.logged_at — metrics keep reading captured_at untouched.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from uuid import UUID

from psycopg.types.json import Json

import db
from models import SaveIntent, StagedItem
from resolver.norm import norm

# Log-level extras that ride every item's staged jsonb (routes/logs.py
# _persist): the when-hint's shifted `logged_at`, decompose's `save_intent`,
# and the ignored-hint `when_hint_note`. Every per-item rewrite of the payload
# (tier-3 result, /api/resolve re-resolution, answer, refusal, and the commit
# itself — a retry and the late commit-item re-read `logged_at` afterwards)
# must carry them forward — append this to a `staged = <new>::jsonb`
# assignment. NULL-valued keys (absent on the old row) are stripped, so
# nothing is invented.
KEEP_LOG_KEYS_SQL = (
    " || jsonb_strip_nulls(jsonb_build_object("
    "'logged_at', item_resolutions.staged->'logged_at', "
    "'save_intent', item_resolutions.staged->'save_intent', "
    "'when_hint_note', item_resolutions.staged->'when_hint_note'))"
)


class LogNotFound(Exception):
    pass


class LogNotCommittable(Exception):
    """Log exists but is in a state the operation can't act on."""


class ItemRefused(Exception):
    """One item failed the core-macros guard — refused, never partially written."""

    def __init__(self, item_index: int, reason: str):
        super().__init__(reason)
        self.item_index = item_index


_CORE_MACROS = ("kcal", "protein_g", "carb_g", "fat_g")


def _missing_core_macros(serving: dict) -> list[str]:
    """Core-macros guard (§4 snapshot invariant): entries.macros must never be
    a partial snapshot. Returns the missing/non-numeric core keys."""
    macros = serving.get("macros") or {}
    return [k for k in _CORE_MACROS if not isinstance(macros.get(k), (int, float))]


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


async def commit_log(
    log_id: UUID, items: list[StagedItem], save_intent: SaveIntent | None = None
) -> dict:
    """§3.5. Returns {"log": row, "replayed": bool, "scores": [...], "meal": ...,
    "logged_at": datetime}.

    Raises LogNotFound / LogNotCommittable (routes map to 404 / 409).
    "errors" carries per-item refusals (core-macros guard): the item stays
    uncommitted as needs_input (client shows verify-me), the rest commit.
    "meal" is the macro step 4 created/updated (explicit `save_intent`, else
    decompose's, read from the staged jsonb), or None. "logged_at" is the
    diary time the entries landed on (§7.2 when-shift, else captured_at).
    """
    scores: list[dict] = []
    errors: list[dict] = []
    meal: dict | None = None
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
        logged_at = await _log_logged_at(conn, log)
        if cur.rowcount == 0:
            if log["status"] == "committed":
                return {
                    "log": log, "replayed": True, "scores": [], "errors": [],
                    "logged_at": logged_at,
                }
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
        committed: list[dict] = []  # {food_id, serving_id, qty} — step 4's input
        for item in items:
            if item.status != "resolved" or item.food is None:
                continue  # searching/needs_input/not_found stay uncommitted (§3.5)
            res = res_rows.get(item.item_index)
            try:
                outcome = await _commit_one(conn, log, item, res, logged_at)
            except ItemRefused as e:
                # core-macros guard: refuse THIS item, never block the rest;
                # the row stays uncommitted so the §3.5 late path stays open.
                await _refuse_item(conn, log, item, res, str(e))
                errors.append({"item_index": e.item_index, "error": str(e)})
                continue
            edited_count += 1 if outcome["was_edited"] else 0
            scores.append(outcome)
            committed.append(
                {k: outcome[k] for k in ("food_id", "serving_id", "qty")}
            )

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

        # 4. meal macro intent: create/update the macro from the committed items
        intent = save_intent or _staged_save_intent(res_rows)
        if intent is not None and committed:
            meal = await save_meal_macro(
                conn, name=intent.name, trigger_phrases=None, items=committed
            )

        await conn.execute(
            "UPDATE logs SET edited_items = %s WHERE id = %s", (edited_count, log_id)
        )
        log = dict(log, status="committed", edited_items=edited_count)

    _post_scores(log, scores)
    return {
        "log": log, "replayed": False, "scores": scores, "errors": errors, "meal": meal,
        "logged_at": logged_at,
    }


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
        logged_at = await _log_logged_at(conn, log)
        if res is not None and res["committed_at"] is not None:
            return {
                "log": log, "replayed": True, "scores": [], "errors": [],
                "logged_at": logged_at,
            }
        refused: str | None = None
        try:
            outcome = await _commit_one(conn, log, item, res, logged_at)
            scores.append(outcome)
        except ItemRefused as e:
            # mark the row inside the tx so the verify-me state persists,
            # then surface the per-item error (route maps to 409)
            await _refuse_item(conn, log, item, res, str(e))
            refused = str(e)
    if refused is not None:
        raise LogNotCommittable(refused)

    _post_scores(log, scores)
    return {
        "log": log, "replayed": False, "scores": scores, "errors": [],
        "logged_at": logged_at,
    }


async def _log_logged_at(conn, log: dict) -> datetime:
    """§7.2: the diary time for this log's entries — the when-hint's shifted
    `logged_at` carried on any of the log's item rows (routes/logs.py
    _persist; KEEP_LOG_KEYS_SQL keeps it through rewrites), else the
    immutable capture time. One indexed read per commit."""
    row = await (
        await conn.execute(
            "SELECT staged->>'logged_at' AS logged_at FROM item_resolutions "
            "WHERE log_id = %s AND staged ? 'logged_at' LIMIT 1",
            (log["id"],),
        )
    ).fetchone()
    if row is not None and row["logged_at"]:
        try:
            return datetime.fromisoformat(row["logged_at"])
        except ValueError:
            pass
    return log["captured_at"]


async def _commit_one(
    conn, log: dict, item: StagedItem, res: dict | None, logged_at: datetime
) -> dict:
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

    # core-macros guard (BACKLOG 7): NEVER silently snapshot partial macros —
    # runs before any write so a refusal leaves the item wholly uncommitted.
    missing = _missing_core_macros(serving)
    if missing:
        raise ItemRefused(
            item.item_index,
            f"serving '{serving_id}' of '{food['display_name']}' is missing core "
            f"macros ({', '.join(missing)}) — verify the food before committing",
        )

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
    # was_edited — the honest §9 edit signal (BACKLOG 4): any food/serving/qty
    # delta proposed-vs-final, OR an explicit edit already recorded on the row
    # (/api/resolve sets was_edited=TRUE; an edit that resolves back to the
    # original proposal must not read as untouched). The qty delta counts even
    # when no serving was proposed. was_disambiguated is never written here, so
    # a truthy value recorded by the answer flow survives commit untouched.
    serving_delta = (
        res is not None
        and res["proposed_serving_id"] is not None
        and res["proposed_serving_id"] != serving_id
    )
    qty_delta = (
        res is not None
        and res["proposed_qty"] is not None
        and float(res["proposed_qty"]) != qty
    )
    prior_edit = bool(res["was_edited"]) if res is not None else False
    was_edited = switched or serving_delta or qty_delta or prior_edit

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
        # §3.4a: a known cooked/raw/dry intent (staged jsonb `state_answer`,
        # written by the resolver) persists to the alias — the state question
        # is asked at most once per phrase.
        staged_prev = res["staged"] if res is not None else None
        state_answer = (
            staged_prev.get("state_answer") if isinstance(staged_prev, dict) else None
        )
        if state_answer in ("cooked", "raw", "dry"):
            await conn.execute(
                "UPDATE aliases SET state_answer = %s WHERE id = %s",
                (state_answer, alias_id),
            )

    await conn.execute(
        """
        INSERT INTO entries (logged_at, meal, food_id, serving_id, qty, grams, macros,
                             log_id, item_index, alias_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (log_id, item_index) DO NOTHING
        """,
        (
            logged_at,
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
            f"""
            UPDATE item_resolutions
            SET status='resolved', staged=%s::jsonb{KEEP_LOG_KEYS_SQL},
                final_food_id=%s, final_serving_id=%s,
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
        "food_id": item.food.id,
        "serving_id": serving_id,
        "qty": qty,
    }


async def _refuse_item(conn, log: dict, item: StagedItem, res: dict | None, reason: str) -> None:
    """Core-macros refusal: flip the row to needs_input (client shows
    verify-me via GET /api/logs/:id), keep it uncommitted, write nothing to
    entries/aliases. The refusal reason rides in the staged jsonb
    (`commit_error` — same extra-key precedent as spoken_qty)."""
    refused = item.model_copy(update={"status": "needs_input", "confidence": "low"})
    staged_payload = refused.model_dump(mode="json", exclude_none=True)
    staged_payload["commit_error"] = reason
    if res is None:
        # added rows must not be telemetry ghosts (§5)
        await conn.execute(
            """
            INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm,
                                          status, staged)
            VALUES (%s, %s, %s, %s, 'needs_input', %s)
            ON CONFLICT (log_id, item_index) DO NOTHING
            """,
            (log["id"], item.item_index, item.phrase, norm(item.phrase), Json(staged_payload)),
        )
    else:
        await conn.execute(
            "UPDATE item_resolutions SET status='needs_input', "
            f"staged = %s::jsonb{KEEP_LOG_KEYS_SQL} WHERE id = %s",
            (Json(staged_payload), res["id"]),
        )


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


# ---------------------------------------------------------------------------
# §3.5 step 4 — meal macros (shared with routes/meals.py)
# ---------------------------------------------------------------------------


class MealInvalid(Exception):
    """A macro write that cannot be stored as given (routes map to 422)."""


def _staged_save_intent(res_rows: dict) -> SaveIntent | None:
    """decompose's save_intent, persisted on the staged jsonb by logs.py
    (extra key `save_intent: {name}`) — first row that carries one."""
    for r in res_rows.values():
        staged = r["staged"]
        intent = staged.get("save_intent") if isinstance(staged, dict) else None
        if isinstance(intent, dict) and (intent.get("name") or "").strip():
            return SaveIntent(name=intent["name"].strip())
    return None


def norm_triggers(phrases: list[str] | None, name: str) -> list[str]:
    """§3.1: trigger phrases are stored as norm() output, deduped, empties
    dropped; none given → the meal's own name ("usual breakfast")."""
    out: list[str] = []
    for p in phrases or []:
        k = norm(p)
        if k and k not in out:
            out.append(k)
    if not out and norm(name):
        out.append(norm(name))
    if not out:
        raise MealInvalid("a saved meal needs at least one trigger phrase")
    return out


async def validate_meal_items(conn, items: list[dict]) -> list[dict]:
    """[{food_id, serving_id, qty}] — food cached, serving on that food,
    qty > 0. Returns the canonical list (str ids, float qty)."""
    if not items:
        raise MealInvalid("a saved meal needs at least one item")
    ids = sorted({str(i.get("food_id")) for i in items})
    try:
        [UUID(i) for i in ids]
    except ValueError:
        raise MealInvalid("food_id must be a uuid")
    foods = {
        str(f["id"]): f
        for f in await (
            await conn.execute(
                "SELECT id, servings FROM foods WHERE id = ANY(%s::uuid[])", (ids,)
            )
        ).fetchall()
    }
    out: list[dict] = []
    for i in items:
        food = foods.get(str(i.get("food_id")))
        if food is None:
            raise MealInvalid(f"unknown food {i.get('food_id')}")
        serving_id = i.get("serving_id")
        if _find_serving(food["servings"], serving_id) is None:
            raise MealInvalid(f"food {i['food_id']} has no serving '{serving_id}'")
        qty = float(i.get("qty") if i.get("qty") is not None else 1)
        if qty <= 0:
            raise MealInvalid("qty must be positive")
        out.append({"food_id": str(i["food_id"]), "serving_id": serving_id, "qty": qty})
    return out


async def save_meal_macro(
    conn, *, name: str, trigger_phrases: list[str] | None, items: list[dict]
) -> dict:
    """Create-or-update the active macro named `name` (case-insensitive):
    items are replaced; trigger_phrases replace the stored ones when given,
    else an update keeps them / a create defaults to norm(name). Returns the
    meal_macros row."""
    name = name.strip()
    if not name:
        raise MealInvalid("a saved meal needs a name")
    items = await validate_meal_items(conn, items)
    existing = await (
        await conn.execute(
            "SELECT id, trigger_phrases FROM meal_macros "
            "WHERE is_active AND lower(name) = lower(%s) "
            "ORDER BY updated_at DESC LIMIT 1",
            (name,),
        )
    ).fetchone()
    if existing is not None:
        triggers = (
            norm_triggers(trigger_phrases, name)
            if trigger_phrases
            else existing["trigger_phrases"]
        )
        row = await (
            await conn.execute(
                "UPDATE meal_macros SET trigger_phrases = %s, items = %s, "
                "updated_at = now() WHERE id = %s RETURNING *",
                (triggers, Json(items), existing["id"]),
            )
        ).fetchone()
    else:
        row = await (
            await conn.execute(
                "INSERT INTO meal_macros (name, trigger_phrases, items) "
                "VALUES (%s, %s, %s) RETURNING *",
                (name, norm_triggers(trigger_phrases, name), Json(items)),
            )
        ).fetchone()
    return row


def _post_scores(log: dict, scores: list[dict]) -> None:
    """§3.5 step 5 / §10: post outcome scores onto the stored trace ids.
    Best-effort — tracing failure must never fail a commit.

    BACKLOG 16: batched — tracing.score_commit is queue-only; exactly ONE
    blocking flush per commit (§11 budget), not one per score."""
    if not scores:
        return
    try:
        import tracing

        posted = False
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
                posted = True
        if posted:
            tracing.flush()  # the one blocking flush per commit (§10 serverless rule)
    except Exception:
        pass
