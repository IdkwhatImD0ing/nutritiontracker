"""Foods cache repository (DESIGN.md §3.4, §4).

persist_food(food) upserts a normalized food dict (the shape
backend/sources/fdc.py get_food returns, plus its source/source_id identity)
on UNIQUE (source, source_id) and returns the row uuid. Cache-forever: a
conflict keeps the existing record (one row per real-world food) and only
touches refreshed_at — with one exception, which is a REPAIR rather than a
refresh: a stored row that carries no loggable serving takes the fresh
record's numbers (see persist_food).

The table is shared by every account (DESIGN §8.9), so what a caller may put
on a row is a tenancy rule: apply_label() takes a chosen display name from
the owner only, and services/tier3.add_food takes web transcriptions from the
owner only. A demo's rows carry the adapter's own names.
"""

from __future__ import annotations

from datetime import timezone
from zoneinfo import ZoneInfo

from psycopg.types.json import Json

import auth
import db
from resolver.commit import COMMITTABLE_FOOD_SQL, food_committable


def apply_label(food: dict, display_name: str | None) -> None:
    """Put a caller-chosen short name on a food about to be cached — the
    owner's only. Every account reads the cache back (manual search, the
    tier-3 candidate list the agent is told to prefer), so a demo's label —
    typed into a pick, or steered through the agent by the phrase — must not
    become the row's name. A demo's rows keep the adapter's display name."""
    if display_name and auth.is_owner():
        food["display_name"] = display_name
    elif not food.get("display_name"):
        food["display_name"] = food.get("name", "")


async def persist_food(food: dict) -> str:
    """Cache one normalized food; the row uuid, new or existing.

    Cache-forever, except that a stored row with NO loggable serving takes the
    fresh record's servings, default, per-100 and payload (BACKLOG 198). A
    provider outage can cache a food with nothing in it — FDC spent 2026-09-15
    to 09-19 answering /food/{id} for every Branded record with nutrient rows
    that named no nutrient, so anything looked up in that window stored zero
    servings — and ON CONFLICT DO UPDATE SET refreshed_at, on its own, would
    keep that row for good: every lane hides it (COMMITTABLE_FOOD_SQL), so it
    is invisible AND unrepairable, and a tier-3 run that finds the id again
    gets this row's uuid back and stages a card the commit guard then refuses.
    Repairing only an unloggable row keeps the invariant that matters — a good
    row is never rewritten under a diary that already snapshotted it — and
    the write carries provider numbers only, never the caller's display_name
    (apply_label's shared-cache rule is unchanged).
    """
    source = food.get("source")
    source_id = food.get("source_id")
    if not source or not source_id:
        raise ValueError("persist_food requires 'source' and 'source_id'")

    row = await db.fetchone(
        f"""
        INSERT INTO foods (source, source_id, source_url, provenance, name,
                           display_name, brand, food_type, state, state_sensitive,
                           servings, default_serving_id, per_100g, payload)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (source, source_id) DO UPDATE SET refreshed_at = now()
        RETURNING id, {COMMITTABLE_FOOD_SQL.format(t="foods")} AS loggable
        """,
        (
            source,
            str(source_id),
            food.get("source_url"),
            Json(food["provenance"]) if food.get("provenance") is not None else None,
            food["name"],
            food["display_name"],
            food.get("brand"),
            food["food_type"],
            food.get("state"),
            bool(food.get("state_sensitive", False)),
            Json(food["servings"]),
            food["default_serving_id"],
            Json(food["per_100g"]) if food.get("per_100g") is not None else None,
            Json(food["payload"]),
        ),
    )
    if not row["loggable"] and food_committable(food):
        # RETURNING reads the row as it now stands, so this is the stored
        # record on a conflict and the inserted one otherwise: either way,
        # nothing here can be logged and the fresh fetch can.
        await db.execute(
            "UPDATE foods SET servings = %s, default_serving_id = %s, per_100g = %s, "
            "payload = %s, refreshed_at = now() WHERE id = %s",
            (
                Json(food["servings"]),
                food["default_serving_id"],
                Json(food["per_100g"]) if food.get("per_100g") is not None else None,
                Json(food["payload"]),
                row["id"],
            ),
        )
    return str(row["id"])


# The foods this account has committed, newest first, one row per food. RLS
# scopes `entries` to the caller; the photo lane's numberless placeholders are
# never offered (they cannot be picked — routes/quick R1). Owner ruling,
# 2026-09-06: the Search lane lists these under its bar, and the chat
# agent's context carries them (§6.4) — barcode, search and photo rows learn no alias
# (§3.5), so the alias-built food index never sees them.
RECENT_FOODS_CAP = 200

_RECENT_FOODS_SQL = """
SELECT f.id::text AS id, f.source, f.source_id, f.display_name, f.brand
FROM (
  SELECT food_id, max(created_at) AS last_at, max(logged_at) AS last_logged
  FROM entries GROUP BY food_id
) e
JOIN foods f ON f.id = e.food_id
WHERE f.source <> 'estimate'
ORDER BY e.last_at DESC, e.last_logged DESC
LIMIT %s
"""


async def recent_foods(limit: int = RECENT_FOODS_CAP) -> list[dict]:
    """(id, source, source_id, display_name, brand) per food, newest commit
    first, at most RECENT_FOODS_CAP rows."""
    return await db.fetchall(_RECENT_FOODS_SQL, (max(1, min(int(limit), RECENT_FOODS_CAP)),))


# The same rows recent_foods() lists, carrying the AMOUNT each food was last
# logged at — what the agent's past_foods tool reads (§6.4). recent_foods()
# groups the amount away and its two callers want it that way, so this is a
# second statement rather than a parameter on the first.
#
# The name-search rule is defined once, here, and imported by
# routes/foods._local_hits: that one searches the shared cache, this one only
# what THIS account has eaten (RLS on `entries`), and the two must not drift.
SIM_FLOOR = 0.15  # typo net; substring matches pass regardless via ILIKE


def like_pattern(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# DISTINCT ON (food_id) → one row per food, the most recent amount: the
# question is "what did I have", not "what do I usually have" (the mode lives
# in cascade._portion_mode and answers a different one). Ordering is on
# logged_at, not created_at as recent_foods uses — the person speaks in diary
# days ("my previous day's"), so a back-dated log belongs where they put it.
# similarity(x, NULL) is NULL for every row, so one statement serves both
# modes: NULLS LAST collapses to pure recency when there is no query.
# `entries_user_food_logged_idx` (004) is what keeps the DISTINCT ON from
# sorting the account's whole history on every call.
_PAST_FOODS_SQL = """
SELECT e.food_id::text AS food_id, f.display_name, f.brand,
       e.serving_id, e.qty, e.grams, e.meal, e.logged_at,
       (SELECT s->>'description' FROM jsonb_array_elements(f.servings) s
         WHERE s->>'serving_id' = e.serving_id LIMIT 1) AS serving,
       similarity(f.display_name || ' ' || coalesce(f.brand, ''), %(q)s::text) AS sim
FROM (
  SELECT DISTINCT ON (food_id)
         food_id, serving_id, qty, grams, meal, logged_at, created_at
  FROM entries
  ORDER BY food_id, logged_at DESC, created_at DESC
) e
JOIN foods f ON f.id = e.food_id
WHERE f.source <> 'estimate'
  AND (%(q)s::text IS NULL
       OR (f.display_name || ' ' || coalesce(f.brand, '')) ILIKE %(like)s
       OR similarity(f.display_name || ' ' || coalesce(f.brand, ''), %(q)s::text)
          > %(floor)s)
ORDER BY sim DESC NULLS LAST, e.logged_at DESC, e.created_at DESC
LIMIT %(limit)s
"""


async def past_foods(
    query: str | None = None, limit: int = RECENT_FOODS_CAP, tz: str = "UTC"
) -> list[dict]:
    """One row per food this account has logged — (food_id, display_name,
    brand, serving_id, qty, grams, meal, day, serving) — carrying the amount
    it was last logged at. `query` searches the whole history by name; None
    lists the most recent. Never macros: the agent may read amounts, never
    numbers (§6.4). `serving` is None when the stored serving_id has since
    left the food's servings array.

    `day` is derived here rather than by `AT TIME ZONE`: `tz` is an
    unvalidated client string, and Postgres resolves zone names from its own
    catalog, so a name ZoneInfo knows and the server does not would raise
    inside the tool instead of falling back."""
    q = (query or "").strip() or None
    rows = await db.fetchall(
        _PAST_FOODS_SQL,
        {
            "q": q,
            "like": like_pattern(q) if q else None,
            "floor": SIM_FLOOR,
            "limit": max(1, min(int(limit), RECENT_FOODS_CAP)),
        },
    )
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = timezone.utc
    for r in rows:
        r["day"] = r.pop("logged_at").astimezone(zone).date()
    return rows
