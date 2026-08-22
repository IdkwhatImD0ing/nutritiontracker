"""M3 end-to-end (integrator) — meals, dashboard, experiment, live
(DESIGN.md §3.0, §3.1, §3.5, §5, §7.4, §9, §12 M3).

In-process TestClient over the assembled app (main.app, every M3 router
wired) against the LIVE Neon DB, with the auth dependency overridden. The
story is the M3 exit gate — "'my usual breakfast' stages instantly, and the
alias-hit-rate curve is visible and rising":

  1. A committed two-item log (seeded foods; commit via the real route) →
     POST /api/meals/from-log saves it as "usual breakfast <tag>" with the
     trigger "my usual breakfast <tag>" (stored post-norm()).
  2. POST /api/logs "My usual breakfast <tag>!" → tier-0 rows stage from the
     whole-utterance short-circuit with ZERO decompose (the decompose entry
     point is patched to raise; logs.short_circuit=true, decompose_ms NULL).
  3. Commit → entries written from the macro's stored portions; NO alias is
     learned for the trigger (tier-0 rule, §3.1/§3.5).
  4. GET /api/history?range=w|3d|y shows today; GET /api/metrics?range=4w
     returns the §9 series with today's hits; GET/compute on /api/settings;
     GET /api/aliases?phrase=; GET /api/foods/:id; GET/DELETE /api/meals —
     a deactivated macro no longer triggers.

Self-cleaning: every row this test creates (entries, item_resolutions, logs,
the learned alias, the macro, the two foods) is deleted in the finally block.
No LLM call is made.

Run:  uv run --with pytest pytest backend/tests/test_e2e_m3.py -q -s
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg.types.json import Json

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _load_env() -> None:
    env_path = BACKEND.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import db  # noqa: E402
import main  # noqa: E402
from resolver.norm import norm  # noqa: E402

pytestmark = pytest.mark.live

TZ = "America/New_York"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def client():
    main.app.dependency_overrides[auth.require_user] = lambda: {
        "email": "billzhangsc@gmail.com",
        "sub": "e2e-m3",
    }
    try:
        with TestClient(main.app) as c:
            yield c
    finally:
        main.app.dependency_overrides.pop(auth.require_user, None)


# ---------------------------------------------------------------------------
# seeding + cleanup (live DB)
# ---------------------------------------------------------------------------


async def _insert_food(name: str, serving: dict, brand: str | None = None) -> str:
    row = await db.fetchone(
        """
        INSERT INTO foods (source, source_id, name, display_name, brand, food_type,
                           servings, default_serving_id, payload)
        VALUES ('fdc', %s, %s, %s, %s, 'generic', %s, %s, '{}'::jsonb)
        RETURNING id
        """,
        (f"e2e-m3-{uuid4().hex[:12]}", name, name, brand, Json([serving]), serving["serving_id"]),
    )
    return str(row["id"])


async def _insert_staged_log(log_id: str, raw: str, items: list[dict]) -> None:
    """A staged log with resolved tier-1 rows, as POST /api/logs would have
    persisted them — without a decompose call."""
    await db.execute(
        "INSERT INTO logs (id, input_mode, raw_input, captured_at) VALUES (%s, 'text', %s, now())",
        (log_id, raw),
    )
    for it in items:
        await db.execute(
            """
            INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm, status,
                                          staged, tier, proposed_food_id,
                                          proposed_serving_id, proposed_qty)
            VALUES (%s, %s, %s, %s, 'resolved', %s, 1, %s, %s, %s)
            """,
            (
                log_id, it["item_index"], it["phrase"], norm(it["phrase"]), Json(it),
                it["food"]["id"], it["serving"]["id"], it["qty"],
            ),
        )


async def _cleanup(log_ids: list[str], tag: str, food_ids: list[str]) -> None:
    await db.execute("DELETE FROM entries WHERE log_id = ANY(%s::uuid[])", (log_ids,))
    await db.execute("DELETE FROM item_resolutions WHERE log_id = ANY(%s::uuid[])", (log_ids,))
    await db.execute("DELETE FROM logs WHERE id = ANY(%s::uuid[])", (log_ids,))
    await db.execute("DELETE FROM aliases WHERE alias_norm LIKE %s", (f"%{tag}%",))
    await db.execute("DELETE FROM meal_macros WHERE name LIKE %s", (f"%{tag}%",))
    if food_ids:
        await db.execute("DELETE FROM foods WHERE id = ANY(%s::uuid[])", (food_ids,))


def _staged(idx: int, phrase: str, food_id: str, name: str, serving: dict, qty: float) -> dict:
    return {
        "item_index": idx,
        "phrase": phrase,
        "status": "resolved",
        "tier": 1,
        "food": {"id": food_id, "name": name, "brand": None},
        "serving": {"id": serving["serving_id"], "description": serving["description"]},
        "qty": qty,
        "macros": {k: v * qty for k, v in serving["macros"].items()},
        "meal": "breakfast",
        "confidence": "high",
    }


# ---------------------------------------------------------------------------
# the story
# ---------------------------------------------------------------------------


def test_m3_usual_breakfast_stages_instantly_and_the_curves_render(client: TestClient, monkeypatch):
    tag = uuid4().hex[:8]
    egg_serving = {
        "serving_id": "large",
        "description": "1 large (50 g)",
        "grams": 50.0,
        "macros": {"kcal": 72.0, "protein_g": 6.3, "carb_g": 0.4, "fat_g": 4.8},
    }
    toast_serving = {
        "serving_id": "slice",
        "description": "1 slice (32 g)",
        "grams": 32.0,
        "macros": {"kcal": 80.0, "protein_g": 4.0, "carb_g": 14.0, "fat_g": 1.0},
    }
    l1, l2 = str(uuid4()), str(uuid4())
    food_ids: list[str] = []
    meal_id: str | None = None
    try:
        egg_id = _run(_insert_food(f"zz egg {tag}", egg_serving))
        toast_id = _run(_insert_food(f"zz toast {tag}", toast_serving))
        food_ids = [egg_id, toast_id]

        # -- 1. a committed two-item log → Save as meal ----------------------
        items1 = [
            _staged(0, f"zz egg {tag}", egg_id, f"zz egg {tag}", egg_serving, 3),
            _staged(1, f"zz toast {tag}", toast_id, f"zz toast {tag}", toast_serving, 2),
        ]
        _run(_insert_staged_log(l1, f"three zz egg {tag} and two zz toast {tag}", items1))
        r = client.post(f"/api/logs/{l1}/commit", json={"items": items1})
        assert r.status_code == 200, r.text
        assert r.json()["errors"] == []
        # tier-1 rows learned their aliases (the ordinary path, for contrast)
        learned = _run(db.fetchall("SELECT alias_norm FROM aliases WHERE alias_norm LIKE %s", (f"%{tag}%",)))
        assert {a["alias_norm"] for a in learned} == {f"zz egg {tag}", f"zz toast {tag}"}

        r = client.post(
            "/api/meals/from-log",
            json={"log_id": l1, "name": f"usual breakfast {tag}",
                  "trigger_phrases": [f"my usual breakfast {tag}"]},
        )
        assert r.status_code == 200, r.text
        meal = r.json()
        meal_id = meal["id"]
        assert meal["is_active"] is True
        assert meal["trigger_phrases"] == [norm(f"my usual breakfast {tag}")] == [f"usual breakfast {tag}"]
        assert [(i["food_id"], i["serving_id"], i["qty"]) for i in meal["items"]] == [
            (egg_id, "large", 3.0), (toast_id, "slice", 2.0),
        ]
        assert meal["items"][0]["food_name"] == f"zz egg {tag}"

        # -- 2. "my usual breakfast" → tier 0, zero decompose ---------------
        import services.decompose as decompose_mod

        async def _no_llm(*a, **kw):
            raise AssertionError("decompose must not run for a macro trigger (§3.0)")

        monkeypatch.setattr(decompose_mod, "decompose", _no_llm)

        alias_count_before = _run(db.fetchone("SELECT count(*) AS n FROM aliases"))["n"]
        t0 = time.perf_counter()
        r = client.post(
            "/api/logs",
            json={"id": l2, "text": f"My usual breakfast {tag}!",
                  "captured_at": datetime.now(timezone.utc).isoformat(), "tz": TZ},
        )
        staged_s = time.perf_counter() - t0
        assert r.status_code == 200, r.text
        body = r.json()
        items2 = body["items"]
        print(f"\n[m3] 'my usual breakfast' staged in {staged_s * 1000:.0f} ms -> {len(items2)} tier-0 rows")
        assert len(items2) == 2, items2
        assert [it["item_index"] for it in items2] == [0, 1]
        assert all(it["status"] == "resolved" and it["tier"] == 0 for it in items2), items2
        assert [(it["food"]["id"], it["serving"]["id"], it["qty"]) for it in items2] == [
            (egg_id, "large", 3.0), (toast_id, "slice", 2.0),
        ]
        assert items2[0]["macros"]["protein_g"] == pytest.approx(6.3 * 3)
        assert all(it["meal"] == body["meal"] for it in items2)
        assert staged_s < 5.0, f"tier 0 took {staged_s:.1f}s"

        log2 = _run(db.fetchone("SELECT * FROM logs WHERE id = %s", (l2,)))
        assert log2["short_circuit"] is True and log2["decompose_ms"] is None
        rows2 = _run(db.fetchall(
            "SELECT item_index, tier, status, phrase FROM item_resolutions WHERE log_id = %s ORDER BY item_index",
            (l2,),
        ))
        assert [(x["item_index"], x["tier"], x["status"]) for x in rows2] == [(0, 0, "resolved"), (1, 0, "resolved")]
        assert rows2[0]["phrase"] == f"zz egg {tag}"  # §5: tier-0 phrase = food display name

        # -- 3. commit → entries from the stored portions, NO alias learned --
        r = client.post(f"/api/logs/{l2}/commit", json={"items": items2})
        assert r.status_code == 200, r.text
        assert r.json()["errors"] == []
        entries2 = _run(db.fetchall(
            "SELECT food_id::text AS food_id, serving_id, qty, alias_id, macros FROM entries "
            "WHERE log_id = %s ORDER BY item_index",
            (l2,),
        ))
        assert [(e["food_id"], e["serving_id"], float(e["qty"])) for e in entries2] == [
            (egg_id, "large", 3.0), (toast_id, "slice", 2.0),
        ]
        assert all(e["alias_id"] is None for e in entries2)
        assert float(entries2[0]["macros"]["protein_g"]) == pytest.approx(6.3 * 3)
        alias_count_after = _run(db.fetchone("SELECT count(*) AS n FROM aliases"))["n"]
        assert alias_count_after == alias_count_before  # tier 0 never writes aliases
        assert _run(db.fetchone(
            "SELECT 1 AS hit FROM aliases WHERE alias_norm = %s", (f"usual breakfast {tag}",)
        )) is None
        committed2 = _run(db.fetchall(
            "SELECT committed_at, alias_id FROM item_resolutions WHERE log_id = %s", (l2,)
        ))
        assert all(x["committed_at"] is not None and x["alias_id"] is None for x in committed2)

        # -- 4a. history: today shows up in every range ----------------------
        r = client.get("/api/history", params={"range": "w"})
        assert r.status_code == 200, r.text
        hist = r.json()
        assert hist["range"] == "w" and len(hist["days"]) == 7 and hist["weeks"] == []
        today = next(d for d in hist["days"] if d["date"] == hist["end"])
        assert today["logged"] is True and today["entries"] >= 4
        assert today["protein_g"] >= 2 * (6.3 * 3 + 4.0 * 2) - 0.01
        assert hist["streak"] >= 1
        assert set(hist["targets"]) == {"kcal", "protein_g", "carb_g", "fat_g"}
        r = client.get("/api/history", params={"range": "3d"})
        assert r.status_code == 200, r.text
        today3 = next(d for d in r.json()["days"] if d["date"] == hist["end"])
        assert today3["meals"] and "breakfast" in today3["meals"]
        r = client.get("/api/history", params={"range": "y"})
        assert r.status_code == 200 and len(r.json()["weeks"]) == 52
        assert r.json()["weeks"][-1]["days_logged"] >= 1

        # -- 4b. metrics: the §9 series, with today's tier-0 hits -----------
        r = client.get("/api/metrics", params={"range": "4w"})
        assert r.status_code == 200, r.text
        m = r.json()
        assert len(m["weeks"]) == 4
        for key in ("latency", "hit_rate", "adherence", "secondary"):
            assert [p["week"] for p in m[key]] == m["weeks"], key
        this_week = m["hit_rate"][-1]
        assert this_week["items"] >= 4 and this_week["hits"] >= 4
        assert this_week["rate"] is not None and 0 < this_week["rate"] <= 1
        assert this_week["rate_incl_discarded"] is not None
        assert m["secondary"][-1]["short_circuit_logs"] >= 1
        assert m["latency"][-1]["committed_logs"] >= 2
        assert m["latency"][-1]["commit_ms_p50"] is not None
        assert m["adherence"][-1]["days_logged"] >= 1
        assert isinstance(m["duplicate_suspects"], list)
        print(
            f"[m3] metrics this week: items={this_week['items']} hits={this_week['hits']} "
            f"rate={this_week['rate']:.2f} short_circuit_logs={m['secondary'][-1]['short_circuit_logs']}"
        )

        # -- 4c. settings + the goal wizard's compute (nothing saved) -------
        r = client.get("/api/settings")
        assert r.status_code == 200, r.text
        assert {"body", "targets", "meal_windows", "profile_notes", "timezone",
                "cronometer_baseline_days_per_week"} <= set(r.json())
        r = client.post(
            "/api/settings/goals/compute",
            json={"height_cm": 180, "weight_kg": 82, "age": 28, "sex": "male",
                  "activity_level": "moderate", "goal_weight_kg": 76, "weekly_rate_kg": 0.5},
        )
        assert r.status_code == 200, r.text
        g = r.json()
        assert g["bmr"] > 0 and g["tdee"] > g["bmr"] and g["daily_delta_kcal"] < 0
        assert set(g["targets"]) == {"kcal", "protein_g", "carb_g", "fat_g"}
        assert g["targets"]["kcal"] < g["tdee"]

        # -- 4d. alias sheet + food detail -----------------------------------
        r = client.get("/api/aliases", params={"phrase": f"My zz egg {tag}"})
        assert r.status_code == 200, r.text
        al = r.json()
        assert al["phrase_norm"] == f"zz egg {tag}" and len(al["aliases"]) == 1
        assert al["aliases"][0]["food_id"] == egg_id and al["aliases"][0]["usual"] is True
        r = client.get(f"/api/foods/{egg_id}")
        assert r.status_code == 200, r.text
        fd = r.json()
        assert fd["display_name"] == f"zz egg {tag}" and len(fd["servings"]) == 1
        assert fd["payload"] == {} and fd["badge"]["source"] == "fdc"
        assert [a["alias_norm"] for a in fd["aliases"]] == [f"zz egg {tag}"]

        # -- 4e. meals list + deactivate → the trigger goes quiet -------------
        r = client.get("/api/meals")
        assert r.status_code == 200 and meal_id in {x["id"] for x in r.json()}
        r = client.delete(f"/api/meals/{meal_id}")
        assert r.status_code == 200 and r.json()["is_active"] is False
        r = client.get("/api/meals")
        assert meal_id not in {x["id"] for x in r.json()}
        r = client.get("/api/meals", params={"include_inactive": "1"})
        assert meal_id in {x["id"] for x in r.json()}
        from models import Meal
        from resolver import cascade

        assert _run(cascade.short_circuit_all(f"My usual breakfast {tag}!", Meal.breakfast)) is None
    finally:
        _run(_cleanup([l1, l2], tag, food_ids))
