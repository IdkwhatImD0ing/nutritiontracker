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

import logging
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
#
# The photo lane's per-row provenance rides the same way (§3.4a,
# routes/photo.py): the raw `vision` item, the user-facing `estimate_note`,
# and the `estimated` flag — which a rewrite may only ever DOWNGRADE.
# "portion" stays "portion": a tier-3 result or search pick swaps a database
# food in (real numbers) while the portion still came from the photo.
# "unresolved" (the numberless estimate row) becomes "portion" only when
# vision actually sized the item (a count or est_grams — that portion rides
# the spoken-quantity channel into the pick); otherwise the pick's qty is the
# resolver's own and the row is "none". `vision` rides OUTSIDE the strip_nulls
# wrapper: jsonb_strip_nulls recurses, and the raw vision item legitimately
# carries null qty/unit/est_grams that the eval loop must see intact.
KEEP_LOG_KEYS_SQL = (
    " || jsonb_strip_nulls(jsonb_build_object("
    "'logged_at', item_resolutions.staged->'logged_at', "
    "'save_intent', item_resolutions.staged->'save_intent', "
    "'when_hint_note', item_resolutions.staged->'when_hint_note', "
    "'estimate_note', item_resolutions.staged->'estimate_note', "
    "'estimated', CASE item_resolutions.staged->>'estimated' "
    "WHEN 'portion' THEN to_jsonb('portion'::text) "
    "WHEN 'unresolved' THEN to_jsonb((CASE WHEN "
    "coalesce((item_resolutions.staged->'vision'->>'qty')::numeric, 0) > 0 "
    "OR coalesce((item_resolutions.staged->'vision'->>'est_grams')::numeric, 0) > 0 "
    "THEN 'portion' ELSE 'none' END)::text) END))"
    " || CASE WHEN item_resolutions.staged ? 'vision' "
    "THEN jsonb_build_object('vision', item_resolutions.staged->'vision') "
    "ELSE '{}'::jsonb END"
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

# The same guard as a WHERE clause, for the paths that must not OFFER a food
# they would later refuse (BACKLOG 184): a foods row is usable only if at
# least one serving carries all four core macros. A provider record can be
# genuinely that thin — live FDC 2756792 (Oikos Triple Zero Mixed Berry,
# dataSource Euromonitor) has kcal and protein but no carb or fat, so every
# serving of it fails _missing_core_macros and the row can never commit.
# Staged, it renders a card with no numbers at all (tier3._staged_from_food
# needs the full four), which is what the user sees. One definition, two
# dialects: change _CORE_MACROS and this together.
# `{t}` is the foods table's name or alias in the caller's query — .format() it.
COMMITTABLE_FOOD_SQL = (
    "EXISTS (SELECT 1 FROM jsonb_array_elements({t}.servings) s"
    " WHERE (s->'macros') ?& array['kcal', 'protein_g', 'carb_g', 'fat_g'])"
)


def food_committable(food: dict) -> bool:
    """Python side of COMMITTABLE_FOOD_SQL: does any serving carry the core four?
    The gate every lane applies BEFORE staging a food (search pick, tier-3
    resolution, barcode), so the commit guard below is a backstop, not the
    first time the user hears about it.

    isinstance-guarded so a malformed servings element cannot raise here while
    the SQL twin merely excludes the row."""
    return any(
        isinstance(s, dict) and not _missing_core_macros(s)
        for s in (food.get("servings") or [])
    )


def with_loggable_servings(food: dict, *, default_key: str = "default_serving_id") -> dict:
    """The food with every unloggable serving dropped and its default serving
    id repointed at a survivor (BACKLOG 184, 192).

    A PARTIAL serving goes too, not just a numberless phantom (owner's ruling,
    2026-09-09): "partials shouldn't be logged, there is no way to get the full
    thing". Nothing can complete a record's missing carb and fat, so a serving
    short of the four is a dead end wearing a verify-me badge — the user cannot
    fix it, which is the one thing the badge implies. This reverses the tier-1/2
    half of BACKLOG 7. It is also simply better: on a food with a complete
    414 g label beside a partial 100 g base, "40 g of X" used to land on the
    partial one and render blank; it now lands on the label and reads
    22.2 kcal / 4.1 P / 0.9 C / 0.4 F.

    `default_key` names the default-serving field, because the cascade's own
    row shape calls it `food_default_serving_id`.

    Answering `food_committable` is not enough on its own: the gate is per
    FOOD, but what renders and commits is one SERVING. A branded record whose
    `label` serving is a phantom and whose per-100 serving is complete passes
    the gate, and then the portion cascade picks `label` — because that is the
    provider default — so the card still shows no numbers and commit still
    refuses it. fdc._branded_servings now drops the phantom at the source, but
    rows cached BEFORE that landed still carry it, and persist_food never
    rewrites a stored servings array — so EVERY path that hydrates a foods row
    runs this: tier3._food_row, routes.quick._load_food (barcode / from-food),
    and the cascade's tier-1/2 and saved-meal expansion. Miss one and the
    phantom is staged again from the same rows (found by review on the first
    cut, which guarded only the first). Returns the food unchanged (not a copy)
    when nothing needs dropping, so the common case is free."""
    servings = food.get("servings") or []
    keep = [s for s in servings if isinstance(s, dict) and not _missing_core_macros(s)]
    if len(keep) == len(servings):
        return food
    out = dict(food)
    out["servings"] = keep
    if not any(s.get("serving_id") == food.get(default_key) for s in keep):
        # keep the provider's preference order; only move when it was dropped
        out[default_key] = keep[0]["serving_id"] if keep else None
    return out

# §3.4a / R3: rows the user never SPOKE learn nothing — a photo, barcode or
# search log has no phrase worth remembering, and a photo-estimated portion
# would poison the alias mode-of-10. Read off logs.input_mode at commit.
_NO_LEARN_MODES = frozenset({"photo", "barcode", "search"})

# The READ side of the same rule (DESIGN.md §3.5 step 3). Commit still writes
# final_* for every row — the diary needs them — so every learning read over
# committed item_resolutions (pick() rule 3, resolve_portion steps 2–3, which
# tier3._apply_portion goes through, and _recompute_alias_default) appends
# this to its WHERE: the row's log must be a spoken one, and a row still
# carrying an estimate flag is out whatever its log says. Unaliased table
# reference (`item_resolutions.`) — the readers query it without an alias.
LEARNABLE_ROWS_SQL = (
    " AND EXISTS (SELECT 1 FROM logs WHERE logs.id = item_resolutions.log_id"
    " AND logs.input_mode NOT IN ("
    + ", ".join(f"'{m}'" for m in sorted(_NO_LEARN_MODES))
    + ")) AND coalesce(item_resolutions.staged->>'estimated', 'none') = 'none'"
)


def _would_commit(item: StagedItem) -> bool:
    return item.status == "resolved" and item.food is not None


def _payload_by_index(items: list[StagedItem]) -> dict[int, StagedItem]:
    """The ONE view of the payload: the gate decides on it and the write loop
    iterates it, keyed by item_index. Two payload items under one index are
    refused outright — a dict would keep one while a list walk wrote the
    other, which is exactly how a forged row slipped past the gate."""
    payload: dict[int, StagedItem] = {}
    dupes: set[int] = set()
    for item in items:
        if item.item_index in payload:
            dupes.add(item.item_index)
        payload[item.item_index] = item
    if dupes:
        raise LogNotCommittable(f"duplicate item_index in payload: {sorted(dupes)}")
    return payload


def _payload_gate(log: dict, res_rows: dict, payload: dict[int, StagedItem]) -> None:
    """The pre-write gate, decided by stored rows only (before any write, so
    a refusal rolls the CAS back with everything else).

    A payload item whose stored row is 'removed' is refused: the user took it
    out of staging, and nothing commits or resurrects it. On a photo log a
    payload item with no stored row is refused outright: the stored row is the
    only place an estimate flag lives, so nothing commits around it.

    §3.4a's per-row estimate confirm is gone (Bill, 2026-09-09): the commit IS
    the confirmation. Six flagged rows off one photo meant six taps before the
    button would light (trace 886b7b91), which bought nothing the person had
    not already seen — the sheet renders every estimate amber with its note,
    and the flag rides through to the diary, so an estimate stays visible and
    editable after the fact. The other three §3.4a protections are untouched:
    an estimated row still learns no alias and no portion default
    (LEARNABLE_ROWS_SQL), still cannot carry a model's nutrition numbers, and
    a numberless `estimated="unresolved"` row still cannot commit at all —
    the core-macros guard refuses it, which is a fact about the row rather
    than a tap it is waiting for."""
    removed = sorted(
        idx for idx in payload if idx in res_rows and res_rows[idx]["status"] == "removed"
    )
    if removed:
        raise LogNotCommittable(f"items {removed} were removed from staging")
    if log.get("input_mode") == "photo":
        orphans = sorted(
            idx for idx, item in payload.items() if idx not in res_rows and _would_commit(item)
        )
        if orphans:
            raise LogNotCommittable(f"photo log has no staged row for items {orphans}")


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
    payload = _payload_by_index(items)
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
                    "log": log, "replayed": True, "scores": [],
                    "errors": await _stored_refusals(conn, log_id),
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

        # §3.5: the payload rules, decided by the stored rows, before any
        # write (the 409 rolls the CAS back with everything else).
        _payload_gate(log, res_rows, payload)

        edited_count = 0
        committed: list[dict] = []  # {food_id, serving_id, qty} — step 4's input
        for idx, item in payload.items():  # the view the gate checked, never the raw list
            if not _would_commit(item):
                continue  # searching/needs_input/not_found stay uncommitted (§3.5)
            res = res_rows.get(idx)
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
        for idx, r in res_rows.items():
            if (
                idx not in payload
                and r["committed_at"] is None
                and r["status"] != "removed"
            ):
                await conn.execute(
                    "UPDATE item_resolutions SET status='removed' WHERE id = %s",
                    (r["id"],),
                )

        # 4. meal macro intent: create/update the macro from the committed items
        # (decompose's save_intent, or the agent's stamp — which may carry
        # the trigger phrases the person said, §6.4)
        intent = (
            {"name": save_intent.name, "trigger_phrases": None}
            if save_intent is not None
            else _staged_save_intent(res_rows)
        )
        if intent is not None and committed:
            meal = await save_meal_macro(
                conn, name=intent["name"], trigger_phrases=intent["trigger_phrases"],
                items=committed,
            )

        await conn.execute(
            "UPDATE logs SET edited_items = %s WHERE id = %s", (edited_count, log_id)
        )
        log = dict(log, status="committed", edited_items=edited_count)

    _post_scores(log, scores)
    # Which diary days this commit actually wrote to. One log is usually one
    # day, but not always: the agent lane can call log_food and log_estimate in
    # a single turn and each shifts its own rows, so the entries land on two.
    # A caller told only `logged_at` would compute one day's totals and paint
    # them over a tracker that is now wrong about both.
    days = sorted({o["logged_at"].date() for o in scores if o.get("logged_at")})
    return {
        "log": log, "replayed": False, "scores": scores, "errors": errors, "meal": meal,
        "logged_at": logged_at, "logged_at_days": days,
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
        # the same gate as commit_log over this one row
        _payload_gate(
            log, {res["item_index"]: res} if res is not None else {}, _payload_by_index([item])
        )
        refused: str | None = None
        try:
            outcome = await _commit_one(conn, log, item, res, logged_at)
            scores.append(outcome)
            try:
                await _extend_saved_meal(conn, log_id, res, outcome)
            except MealInvalid:
                # Saved-meal bookkeeping is a side effect of the entry, never a
                # condition of it. validate_meal_items re-checks the WHOLE
                # stored list, so a food deleted out of the macro since it was
                # saved raises here — and an uncaught raise inside this tx
                # would roll back the diary row that just committed, losing the
                # very item the late path exists to save. The meal keeps what
                # it had; the entry stands. (MealInvalid only: a psycopg error
                # has already aborted the transaction and must propagate.)
                logging.getLogger(__name__).warning(
                    "late item %s committed; saved meal left unchanged", item.item_index
                )
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


async def _extend_saved_meal(conn, log_id: UUID, res: dict | None, outcome: dict) -> None:
    """§3.5: the late path finishes what step 4 started.

    Step 4 builds the macro from the rows that had resolved by commit time, so
    "save this as my usual lunch" said while tier 3 is still searching stores a
    lunch missing whatever it was searching for — and because save_meal_macro
    REPLACES the item list, re-saving over a complete meal shrinks it. An item
    that lands afterwards has to join the meal too, or the saved meal is
    permanently whatever happened to be fast enough.

    Silent when the log carried no save intent, or when the macro it named is
    gone (deleted or renamed in Library between the two calls) — a late item is
    not a reason to resurrect a meal the user removed."""
    # The stamp is on every non-removed row of the log (logs.py
    # _stamp_save_intent), so the row this call already holds answers it — no
    # read at all on the ordinary late item, which is almost all of them. Only
    # an ADDED row (res is None) has to go looking.
    intent = _staged_save_intent({0: res} if res is not None else {})
    if intent is None and res is None:
        rows = await (
            await conn.execute(
                "SELECT item_index, staged FROM item_resolutions "
                "WHERE log_id = %s AND staged ? 'save_intent' LIMIT 1",
                (log_id,),
            )
        ).fetchall()
        intent = _staged_save_intent({r["item_index"]: r for r in rows})
    if intent is None:
        return
    existing = await (
        await conn.execute(
            "SELECT id, items FROM meal_macros WHERE is_active AND lower(name) = lower(%s) "
            "ORDER BY updated_at DESC LIMIT 1",
            (intent["name"].strip(),),
        )
    ).fetchone()
    if existing is None:
        return
    item = {k: outcome[k] for k in ("food_id", "serving_id", "qty")}
    items = list(existing["items"] or ())
    if any(
        str(i.get("food_id")) == str(item["food_id"])
        and i.get("serving_id") == item["serving_id"]
        for i in items
        if isinstance(i, dict)
    ):
        return  # already in the meal — a re-run must not duplicate the row
    await conn.execute(
        "UPDATE meal_macros SET items = %s, updated_at = now() WHERE id = %s",
        (Json(await validate_meal_items(conn, items + [item])), existing["id"]),
    )


async def _log_logged_at(conn, log: dict) -> datetime:
    """§7.2: the diary time for this log's entries — the when-hint's shifted
    `logged_at` carried on any of the log's item rows (routes/logs.py
    _persist; KEEP_LOG_KEYS_SQL keeps it through rewrites), else the
    immutable capture time. One indexed read per commit."""
    row = await (
        await conn.execute(
            "SELECT staged->>'logged_at' AS logged_at FROM item_resolutions "
            "WHERE log_id = %s AND staged ? 'logged_at' "
            # ORDER BY, because an unordered LIMIT 1 is a coin flip when the
            # rows disagree — the agent lane can stamp two (log_food and
            # log_estimate each run their own when-shift in one turn), and the
            # first row is at least a stable, explainable choice
            "ORDER BY item_index LIMIT 1",
            (log["id"],),
        )
    ).fetchone()
    if row is not None and row["logged_at"]:
        try:
            return datetime.fromisoformat(row["logged_at"])
        except ValueError:
            pass
    return log["captured_at"]


def _row_logged_at(res: dict | None, fallback: datetime) -> datetime:
    """The diary time for ONE entry: its own row's shifted `logged_at` when it
    has one, else the log's.

    A log is not always one moment. The agent lane can call log_food and
    log_estimate in a single turn, each shifting its own items, so stamping
    every entry with one row's value silently moved food onto another day."""
    staged = res["staged"] if res is not None and isinstance(res.get("staged"), dict) else None
    stamped = staged.get("logged_at") if staged else None
    if stamped:
        try:
            return datetime.fromisoformat(stamped)
        except ValueError:
            pass
    return fallback


async def _commit_one(
    conn, log: dict, item: StagedItem, res: dict | None, logged_at: datetime
) -> dict:
    """Steps 1–3 + 5 of §3.5 for one item, inside the open transaction."""
    if res is None and log.get("input_mode") == "photo":
        # a photo log's rows are the ones the server staged (§3.4a) — the gate
        # refuses these before any write; the INSERT branch below stays closed
        # to them whatever the caller
        raise LogNotCommittable(f"photo log has no staged row for item {item.item_index}")
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

    staged_prev = res["staged"] if res is not None and isinstance(res["staged"], dict) else {}
    # §3.4a: the STORED flag is what the committed payload records, whatever
    # the client sent (a row the server never stored carries none; the flag
    # comes from the photo lane or the agent's log_estimate, §6.4). It decides
    # the alias gate below and rides into the diary, so a client may not
    # un-flag its own estimate by relabelling the row.
    item = item.model_copy(update={"estimated": staged_prev.get("estimated") or "none"})

    # 2. alias — for each item except tier-0 expansions, removed rows, rows
    # the user never spoke (photo/barcode/search logs: _NO_LEARN_MODES), and —
    # whatever the log — a row the agent sized as an estimate (§6.4): its
    # phrase is the agent's component name, its portion a guess.
    # Learned for the ORIGINAL spoken phrase (res["phrase"]); rejected_count
    # lands on the abandoned pre-selection only when the user actually switched.
    alias_id = None
    if (
        tier != 0
        and log.get("input_mode") not in _NO_LEARN_MODES
        and (staged_prev.get("estimated") or "none") == "none"
    ):
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
        state_answer = staged_prev.get("state_answer")
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
            _row_logged_at(res, logged_at),
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
        # the day THIS entry landed on — rows of one log can disagree, so the
        # caller has to know which days it actually touched, not just one
        "logged_at": _row_logged_at(res, logged_at),
    }


async def _stored_refusals(conn, log_id: UUID) -> list[dict]:
    """The refusals a PREVIOUS commit of this log recorded, in the shape the
    live path returns them.

    A retried commit used to answer `errors: []` without reading a row, so a
    lost response (a timeout, a backgrounded phone) took every refusal with it:
    the client builds its retry set from this list alone, and a row nothing
    forwards is a row nothing ever chases. `_refuse_item` parks the reason in
    the staged jsonb, so the second answer can be the same as the first."""
    rows = await (
        await conn.execute(
            "SELECT item_index, staged->>'commit_error' AS commit_error "
            "FROM item_resolutions "
            "WHERE log_id = %s AND committed_at IS NULL AND status = 'needs_input' "
            "  AND staged ? 'commit_error' "
            "ORDER BY item_index",
            (log_id,),
        )
    ).fetchall()
    return [{"item_index": r["item_index"], "error": r["commit_error"]} for r in rows]


async def _refuse_item(conn, log: dict, item: StagedItem, res: dict | None, reason: str) -> None:
    """Core-macros refusal: flip the row to needs_input (client shows
    verify-me via GET /api/logs/:id), keep it uncommitted, write nothing to
    entries/aliases. The refusal reason rides in the staged jsonb
    (`commit_error` — same extra-key precedent as spoken_qty)."""
    staged_prev = res["staged"] if res is not None and isinstance(res["staged"], dict) else {}
    refused = item.model_copy(
        update={
            "status": "needs_input",
            "confidence": "low",
            # §3.4a: the stored row's flag, never the client's (as in _commit_one)
            "estimated": staged_prev.get("estimated") or "none",
        }
    )
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
            ON CONFLICT (user_id, alias_norm, food_id) DO UPDATE
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
            f"""
            SELECT final_serving_id, final_qty FROM item_resolutions
            WHERE alias_id = %s AND committed_at IS NOT NULL
              AND final_serving_id IS NOT NULL AND final_qty IS NOT NULL{LEARNABLE_ROWS_SQL}
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


def _staged_save_intent(res_rows: dict) -> dict | None:
    """The save_intent persisted on the staged jsonb — decompose's, written
    by logs.py _persist (`{name}`), or the agent's stamp (`{name,
    trigger_phrases}`, logs.py _stamp_save_intent) — first row that carries
    one, as `{"name", "trigger_phrases"}`."""
    for r in res_rows.values():
        staged = r["staged"]
        intent = staged.get("save_intent") if isinstance(staged, dict) else None
        if isinstance(intent, dict) and (intent.get("name") or "").strip():
            triggers = intent.get("trigger_phrases")
            return {
                "name": intent["name"].strip(),
                "trigger_phrases": triggers if isinstance(triggers, list) else None,
            }
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
    flush per commit (§11 budget), not one per score. The flush hands the
    batch to FireTrace's background sender (§10) — it never blocks."""
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
            tracing.flush()  # the one flush per commit — a background send (§10)
    except Exception:
        pass
