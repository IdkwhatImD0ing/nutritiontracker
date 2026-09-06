"""Agent mode, live (DESIGN.md §6.4; §3.4 rule 2b) — the owner's two asks, 2026-09-06.

In-process TestClient over the assembled app against the LIVE services:
Neon, gpt-5.6-luna (the front-door agent and decompose), the gpt-5.6-terra
tier-3 agent with the hosted web_search, USDA FDC, Langfuse. Three stories:

  1. a weigh-in alone: "I weighed myself this morning, 180.4 pounds" → a
     weights row (81.83 kg, unit lb, the utterance's local day, this log's
     id), zero items, a one-sentence reply that names the number.
  2. food and a weight in one breath: "two eggs and a fairlife, and I'm at
     180 pounds today" → eggs (qty 2) and fairlife staged through the
     ordinary pipeline AND the weigh-in recorded.
  3. the retail-product web lane: "costco tonkotsu ramen" → the agent stages
     it, the client kicks tier 3, and the item resolves with real numbers —
     from FDC Branded when the product is there, else transcribed from the
     brand's or Costco's official page with a source_url (source='web'). A
     which-variant question with id-bearing options counts: the first is
     tapped, as the sheet would. Prints which lane answered and the run's
     tool calls. `not_found` fails the test.

Every row is deleted in the finally block (weights first — its FK points at
the log; a food the run created after t0 only when nothing else references
it). Dated 2000-01-06 so diary noise never lands on a real day; the tenant is
a synthetic id, so the owner's own settings and weigh-ins are never touched.

Run:  cd backend && uv run --with pytest pytest tests/test_agent_live.py -q -s -m live
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

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
from tenancy_harness import override_user  # noqa: E402
import main  # noqa: E402

pytestmark = pytest.mark.live

CAPTURED_AT = "2000-01-06T16:15:00+00:00"  # 08:15 in Los Angeles, a dead date
TZ = "America/Los_Angeles"
LOCAL_DAY = "2000-01-06"
LB_PER_KG = 2.2046226


def _run(coro):
    return asyncio.run(coro)


USER_ID = "e2e-agent"  # a synthetic tenant: the owner's own rows are never touched


@pytest.fixture()
def client():
    # override_user binds the tenant inside the REQUEST's context only; the
    # test's own reads and the cleanup run in this context, so they must be
    # bound to the same tenant or RLS shows them nothing (BACKLOG 133).
    user = {"id": USER_ID, "email": "billzhangsc@gmail.com", "is_owner": True}
    t_id = db.current_user_id.set(USER_ID)
    t_user = auth.current_user.set(user)
    main.app.dependency_overrides[auth.require_user] = override_user(
        email=user["email"], user_id=USER_ID
    )
    try:
        with TestClient(main.app) as c:
            yield c
    finally:
        main.app.dependency_overrides.pop(auth.require_user, None)
        auth.current_user.reset(t_user)
        db.current_user_id.reset(t_id)


def _post(client: TestClient, log_id: str, text: str) -> dict:
    t0 = time.perf_counter()
    r = client.post(
        "/api/logs",
        json={"id": log_id, "text": text, "captured_at": CAPTURED_AT, "tz": TZ, "agent": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    print(
        f"\n[agent live] {text!r}: {time.perf_counter() - t0:.1f}s items="
        f"{[(it['phrase'], it['status'], it.get('qty')) for it in body['items']]} "
        f"agent={body.get('agent')}"
    )
    return body


async def _cleanup(t0, log_ids: list[str]) -> None:
    try:
        await db.execute("DELETE FROM weights WHERE log_id = ANY(%s::uuid[])", (log_ids,))
        await db.execute("DELETE FROM weights WHERE day = %s", (LOCAL_DAY,))
    except psycopg.errors.UndefinedTable:
        pass  # 003 not applied yet: nothing was written there
    await db.execute("DELETE FROM entries WHERE log_id = ANY(%s::uuid[])", (log_ids,))
    await db.execute("DELETE FROM item_resolutions WHERE log_id = ANY(%s::uuid[])", (log_ids,))
    await db.execute("DELETE FROM logs WHERE id = ANY(%s::uuid[])", (log_ids,))
    new_foods = await db.fetchall("SELECT id FROM foods WHERE created_at >= %s", (t0,))
    for row in new_foods:
        try:
            await db.execute("DELETE FROM foods WHERE id = %s", (row["id"],))
        except Exception:
            pass  # referenced by a pre-existing row — leave it cached


def _squash(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


# ---------------------------------------------------------------------------
# 1 + 2: the weigh-in, alone and with food
# ---------------------------------------------------------------------------


def test_agent_records_a_weigh_in_and_stages_food(client: TestClient, needs_003):
    t0 = _run(db.fetchone("SELECT now() AS t"))["t"]
    l1, l2 = str(uuid4()), str(uuid4())
    try:
        # -- 1. a weigh-in alone ------------------------------------------
        body = _post(client, l1, "I weighed myself this morning, 180.4 pounds")
        assert body["items"] == [], body
        agent = body["agent"]
        assert agent and agent["reply"].strip(), body
        w = agent["weight"]
        assert w is not None, "the weigh-in was not recorded"
        assert w["unit"] == "lb" and w["source"] == "agent" and w["log_id"] == l1
        assert w["day"] == LOCAL_DAY, w
        assert abs(w["weight_kg"] - 180.4 / LB_PER_KG) < 0.01, w
        assert "180.4" in agent["reply"], agent["reply"]  # the number came from the tool result
        row = _run(db.fetchone("SELECT weight_kg, unit, day::text FROM weights WHERE log_id = %s", (l1,)))
        assert row is not None and row["unit"] == "lb" and row["day"] == LOCAL_DAY
        # no macros in the reply — the sheet shows those
        assert "kcal" not in agent["reply"].lower() and "calorie" not in agent["reply"].lower()

        # -- 2. food and a weight in one breath --------------------------
        body = _post(client, l2, "two eggs and a fairlife, and I'm at 180 pounds today")
        items = body["items"]
        assert len(items) >= 2, items
        eggs = next((it for it in items if "egg" in it["phrase"].lower()), None)
        fairlife = next((it for it in items if "fairlife" in _squash(it["phrase"])), None)
        assert eggs is not None and fairlife is not None, [it["phrase"] for it in items]
        assert eggs["qty"] == 2, eggs
        assert all(it["status"] in ("resolved", "searching", "needs_input") for it in items)
        agent = body["agent"]
        assert agent and agent["weight"] is not None, body
        assert agent["weight"]["log_id"] == l2 and agent["weight"]["day"] == LOCAL_DAY
        assert abs(agent["weight"]["weight_kg"] - 180 / LB_PER_KG) < 0.01
        # the same local day: replaced in place, one row for the day
        assert _run(db.fetchone("SELECT count(*) AS n FROM weights WHERE day = %s", (LOCAL_DAY,)))["n"] == 1
        # the food part reached decompose as food only: no "pounds" row
        assert not any("pound" in it["phrase"].lower() or "180" in it["phrase"] for it in items), items
    finally:
        _run(_cleanup(t0, [l1, l2]))


# ---------------------------------------------------------------------------
# 3: "costco tonkotsu ramen" — a retail product through the web lane
# ---------------------------------------------------------------------------


def test_costco_tonkotsu_ramen_resolves_through_the_agent(client: TestClient):
    t0 = _run(db.fetchone("SELECT now() AS t"))["t"]
    log_id = str(uuid4())
    try:
        body = _post(client, log_id, "costco tonkotsu ramen")
        assert body["agent"] and body["agent"]["weight"] is None
        assert len(body["items"]) == 1, body["items"]
        item = body["items"][0]
        assert "ramen" in item["phrase"].lower(), item
        if item["status"] == "searching":
            t1 = time.perf_counter()
            r = client.post("/api/resolve/tier3", json={"log_id": log_id, "item_index": item["item_index"]})
            assert r.status_code == 200, r.text
            item = r.json()
            print(f"[agent live] tier 3: {time.perf_counter() - t1:.1f}s status={item['status']}")
        if item["status"] == "needs_input" and item.get("question"):
            opts = item["question"]["options"]
            print(f"[agent live] question: {item['question']['text']!r} options={[o['label'] for o in opts]}")
            opt = next((o for o in opts if o.get("source") and o.get("source_id")), None)
            assert opt is not None, "a question with no id-bearing option — nothing to tap"
            r = client.post(
                "/api/resolve/answer",
                json={"log_id": log_id, "item_index": item["item_index"],
                      "answer": {"label": opt["label"], "source": opt["source"], "source_id": opt["source_id"]}},
            )
            assert r.status_code == 200, r.text
            item = r.json()

        row = _run(db.fetchone(
            "SELECT status, agent_transcript, network_calls FROM item_resolutions "
            "WHERE log_id = %s AND item_index = 0", (log_id,),
        ))
        transcript = row["agent_transcript"] or []
        if isinstance(transcript, str):
            transcript = json.loads(transcript)
        calls = [t.get("name") for t in transcript if isinstance(t, dict) and t.get("type") == "function_call"]
        searches = [t for t in transcript if isinstance(t, dict) and t.get("type") == "web_search_call"]
        print(f"[agent live] function_calls={calls} web_search_actions={len(searches)} network_calls={row['network_calls']}")

        assert item["status"] == "resolved", f"expected resolved, got {item['status']}: {item}"
        assert item["food"] and item["macros"] and item["macros"]["kcal"] > 0, item
        food = _run(db.fetchone(
            "SELECT source, source_url, display_name, brand, provenance FROM foods WHERE id = %s",
            (item["food"]["id"],),
        ))
        print(
            f"[agent live] resolved via {food['source']}: {food['display_name']!r} brand={food['brand']!r} "
            f"serving={item['serving']['description']!r} qty={item['qty']} "
            f"macros={ {k: item['macros'][k] for k in ('kcal', 'protein_g', 'carb_g', 'fat_g')} } "
            f"source_url={food['source_url']} web_source={item.get('web_source')}"
        )
        assert food["source"] in ("fdc", "web"), food
        if food["source"] == "web":
            # §3.4a: the page is real and cited, and the row carries the link
            assert food["source_url"] and food["source_url"].startswith("http"), food
            assert (food["provenance"] or {}).get("citations"), food
            assert item.get("web_source") and item["web_source"]["url"] == food["source_url"]
            assert searches, "a web food with no web_search_call in the archived transcript"
    finally:
        _run(_cleanup(t0, [log_id]))
