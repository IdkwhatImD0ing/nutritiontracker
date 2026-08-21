"""Foods cache repository (DESIGN.md §3.4, §4).

persist_food(food) upserts a normalized food dict (the shape
backend/sources/fdc.py get_food returns, plus its source/source_id identity)
on UNIQUE (source, source_id) and returns the row uuid. Cache-forever: a
conflict keeps the existing record (one row per real-world food) and only
touches refreshed_at.
"""

from __future__ import annotations

from psycopg.types.json import Json

import db


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
