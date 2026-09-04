"""Foods cache repository (DESIGN.md §3.4, §4).

persist_food(food) upserts a normalized food dict (the shape
backend/sources/fdc.py get_food returns, plus its source/source_id identity)
on UNIQUE (source, source_id) and returns the row uuid. Cache-forever: a
conflict keeps the existing record (one row per real-world food) and only
touches refreshed_at.

The table is shared by every account (DESIGN §8.9), so what a caller may put
on a row is a tenancy rule: apply_label() takes a chosen display name from
the owner only, and services/tier3.add_food takes web transcriptions from the
owner only. A demo's rows carry the adapter's own names.
"""

from __future__ import annotations

from psycopg.types.json import Json

import auth
import db


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
    source = food.get("source")
    source_id = food.get("source_id")
    if not source or not source_id:
        raise ValueError("persist_food requires 'source' and 'source_id'")

    row = await db.fetchone(
        """
        INSERT INTO foods (source, source_id, source_url, provenance, name,
                           display_name, brand, food_type, state, state_sensitive,
                           servings, default_serving_id, per_100g, payload)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (source, source_id) DO UPDATE SET refreshed_at = now()
        RETURNING id
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
    return str(row["id"])
