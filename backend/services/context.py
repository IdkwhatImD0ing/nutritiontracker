"""Agent context assembly — DESIGN.md §6.1.

Everything the model sees about the user comes from build_agent_context(),
assembled fresh per call from Postgres. It is not a separate memory system —
it is views over the same tables the product runs on.
"""

from dataclasses import dataclass


@dataclass
class AgentContext:
    profile_notes: str
    targets: dict
    food_index: list[tuple[str, str, str | None]]  # (index_id, display_name, brand)
    meal_names: list[str]


_SETTINGS_SQL = (
    "SELECT key, value FROM settings"
    " WHERE key IN ('profile_notes', 'targets', 'food_index')"
)

# Top-N foods by total alias use (aliases ⋈ foods, §6.1). index_id is foods.id —
# the id the resolver validates library_match against (§3.3 2b).
_FOOD_INDEX_SQL = """
SELECT f.id::text, f.display_name, f.brand
FROM aliases a
JOIN foods f ON f.id = a.food_id
GROUP BY f.id, f.display_name, f.brand
ORDER BY sum(a.use_count) DESC, max(a.last_used_at) DESC
LIMIT %s
"""

_MEAL_NAMES_SQL = "SELECT name FROM meal_macros WHERE is_active ORDER BY name"


async def build_agent_context() -> AgentContext:
    # db.py is owned by the resolver workstream and built in parallel; imported
    # lazily so this module loads (and stubs cleanly in tests) before it exists.
    import db

    # db.py's shared interface returns dict rows (integration fix: this module
    # was written against tuple rows before db.py landed).
    settings = {row["key"]: row["value"] for row in await db.fetchall(_SETTINGS_SQL)}

    food_index_cfg = settings.get("food_index") or {}
    inline_n = int(food_index_cfg.get("inline_default", 20))

    index_rows = await db.fetchall(_FOOD_INDEX_SQL, (inline_n,))
    meal_rows = await db.fetchall(_MEAL_NAMES_SQL)

    return AgentContext(
        profile_notes=settings.get("profile_notes") or "",
        targets=settings.get("targets") or {},
        food_index=[(r["id"], r["display_name"], r["brand"]) for r in index_rows],
        meal_names=[r["name"] for r in meal_rows],
    )
