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
  4. the chat's stream and an unlabeled deli item: "I had a platter of
     costco shrimp" through POST /api/agent → the agent researches the
     product, finds no label, and stages its components (shrimp, cocktail
     sauce) with estimated amounts — rows flagged estimated="portion", the
     numbers from the database entries, the commit refused until confirmed.
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
    # bound to the same tenant or RLS shows them nothing (BACKLOG 134).
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


# ---------------------------------------------------------------------------
# 4: "I had a platter of costco shrimp" — the chat's stream (POST /api/agent)
#    and an unlabeled deli item estimated from database components (§6.4,
#    §3.4a; the chat redesign, 2026-09-06)
# ---------------------------------------------------------------------------


def _post_stream(client: TestClient, log_id: str, text: str, history: list[dict] | None = None) -> list[dict]:
    t0 = time.perf_counter()
    r = client.post(
        "/api/agent",
        json={"id": log_id, "text": text, "captured_at": CAPTURED_AT, "tz": TZ, "history": history},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/x-ndjson"), r.headers
    events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    print(
        f"\n[agent live] {text!r}: {time.perf_counter() - t0:.1f}s "
        f"events={[e['type'] + (':' + e['name'] if e.get('name') else '') for e in events]}"
    )
    return events


def test_costco_shrimp_platter_is_estimated_from_database_components(client: TestClient):
    t0 = _run(db.fetchone("SELECT now() AS t"))["t"]
    log_id = str(uuid4())
    question_ids: list[str] = []
    try:
        text = "I had a platter of costco shrimp"
        events = _post_stream(client, log_id, text)
        assert events[-1]["type"] == "done", events[-1]
        # the agent asks before it guesses (2026-09-06) — which product, then
        # how much of it: answer each question with its first option as the
        # chat would, the next turn carrying the history; two rounds at most
        history: list[dict] = []
        question_ids: list[str] = []
        for _round in range(2):
            reply_ev = next(e for e in events if e["type"] == "reply")
            if not reply_ev.get("options"):
                break
            print(f"[agent live] asked: {reply_ev['text']!r} options={reply_ev['options']}")
            assert events[-1]["log"]["items"] == [], "a question must not stage anything"
            assert events[-1]["log"]["agent"]["options"] == reply_ev["options"]
            history += [{"role": "user", "text": text}, {"role": "agent", "text": reply_ev["text"]}]
            question_ids.append(log_id)
            log_id, text = str(uuid4()), reply_ev["options"][0]
            events = _post_stream(client, log_id, text, history)
            assert events[-1]["type"] == "done", events[-1]
        assert not any(e.get("options") for e in events if e["type"] == "reply"), "still asking after two answers"
        calls = [e for e in events if e["type"] == "tool_call"]
        names = [c["name"] for c in calls]
        # live web search: the line goes up before the query is known, the result brings it
        for c in calls:
            if c["name"] == "web_search" and c["args"].get("query") is None:
                res = next((e for e in events if e["type"] == "tool_result" and e["id"] == c["id"]), None)
                assert res is not None and res["output"].get("query"), "a live search line never settled"
        for c in calls:
            print(f"[agent live]   {c['name']}({json.dumps(c['args'])[:400]})")
        for e in events:
            if e["type"] == "tool_result" and e.get("name") == "web_search":
                print(f"[agent live]   web_search read {e['output'].get('sources')}")
        reply = next(e["text"] for e in events if e["type"] == "reply")
        print(f"[agent live] reply: {reply!r}")

        # the agent worked it out on its own: the platter has no label, so its
        # components were estimated — shrimp and the cocktail sauce
        assert "log_estimate" in names, f"the agent did not estimate: {names}"
        est = next(c for c in calls if c["name"] == "log_estimate")
        phrases = [it["phrase"].lower() for it in est["args"]["items"]]
        assert any("shrimp" in p for p in phrases), phrases
        assert any("sauce" in p for p in phrases), phrases
        assert "kcal" not in reply.lower() and "calorie" not in reply.lower(), reply

        log = events[-1]["log"]
        items = log["items"]
        assert len(items) >= 2, items
        assert all(it["estimated"] == "portion" and it.get("estimate_note") for it in items), items
        assert log["agent"]["reply"] == reply

        # the searching rows through tier 3, as the sheet would kick them
        final = []
        for it in items:
            if it["status"] == "searching":
                t1 = time.perf_counter()
                r = client.post(
                    "/api/resolve/tier3", json={"log_id": log_id, "item_index": it["item_index"]}
                )
                assert r.status_code == 200, r.text
                it = r.json()
                print(f"[agent live] tier 3 {it['phrase']!r}: {time.perf_counter() - t1:.1f}s -> {it['status']}")
            final.append(it)
        for it in final:
            m = it.get("macros") or {}
            print(
                f"[agent live] row {it['item_index']} {it['phrase']!r} -> {it['status']} "
                f"food={(it.get('food') or {}).get('name')!r} qty={it.get('qty')} "
                f"serving={(it.get('serving') or {}).get('description')!r} "
                f"kcal={m.get('kcal')} protein={m.get('protein_g')} "
                f"estimated={it['estimated']} note={it.get('estimate_note')!r}"
            )
        shrimp = next(it for it in final if "shrimp" in it["phrase"].lower())
        assert shrimp["status"] == "resolved", shrimp
        assert shrimp["macros"]["kcal"] > 0 and shrimp["estimated"] == "portion", shrimp  # flag survived tier 3
        # a whole platter is a lot of shrimp — not one database serving, and not
        # a count multiplied onto a 100 g serving either (133 × 100 g was 13,167 kcal)
        assert shrimp["macros"]["protein_g"] >= 40, shrimp
        assert shrimp["macros"]["kcal"] < 6000, shrimp
        # §3.4a: nothing estimated commits unconfirmed, server-side
        resolved = [it for it in final if it["status"] == "resolved"]
        r = client.post(f"/api/logs/{log_id}/commit", json={"items": resolved})
        assert r.status_code == 409 and "confirm the estimated rows first" in r.text, r.text
    finally:
        _run(_cleanup(t0, [log_id, *question_ids]))


# ---------------------------------------------------------------------------
# 5: saved meals through the agent — save what was logged, extend it by words,
#    rename it (§8.4 via §6.4; Bill's ruling 2026-09-07: the agent can do what
#    the UI can)
# ---------------------------------------------------------------------------


def _tool_calls(events: list[dict]) -> list[str]:
    return [e["name"] for e in events if e["type"] == "tool_call"]


def _log_and_commit(client: TestClient, log_id: str, text: str) -> list[dict]:
    """Stage `text` through the agent, resolve every row as the sheet would
    (the tier-3 kick, the first id-bearing answer to a question), commit the
    resolved rows — and return them. A synthetic tenant has no aliases, so
    the commit is also what teaches it the phrase (§3.5)."""
    body = _post(client, log_id, text)
    assert body["items"], body
    for it in body["items"]:
        if it["status"] == "searching":
            r = client.post("/api/resolve/tier3", json={"log_id": log_id, "item_index": it["item_index"]})
            assert r.status_code == 200, r.text
            it = r.json()
        if it["status"] == "needs_input" and it.get("question"):
            opt = next((o for o in it["question"]["options"] if o.get("source") and o.get("source_id")), None)
            assert opt is not None, it["question"]
            r = client.post(
                "/api/resolve/answer",
                json={"log_id": log_id, "item_index": it["item_index"],
                      "answer": {"label": opt["label"], "source": opt["source"], "source_id": opt["source_id"]}},
            )
            assert r.status_code == 200, r.text
    r = client.get(f"/api/logs/{log_id}")
    assert r.status_code == 200, r.text
    resolved = [it for it in r.json()["items"] if it["status"] == "resolved"]
    assert resolved, r.json()
    r = client.post(f"/api/logs/{log_id}/commit", json={"items": resolved})
    assert r.status_code == 200, r.text
    return resolved


def test_the_agent_saves_extends_and_renames_a_meal(client: TestClient):
    t0 = _run(db.fetchone("SELECT now() AS t"))["t"]
    l0, l1, l2, l3, l4 = (str(uuid4()) for _ in range(5))
    tag = uuid4().hex[:6]
    name, renamed = f"test brekkie {tag}", f"weekday brekkie {tag}"

    def macro() -> dict | None:
        return _run(db.fetchone(
            "SELECT name, trigger_phrases, items, is_active FROM meal_macros "
            "WHERE lower(name) LIKE %s ORDER BY updated_at DESC LIMIT 1",
            (f"%{tag}",),
        ))

    try:
        # a banana logged once, so this tenant knows the word (the tool's own
        # advice for a food the databases do not know yet); then the meal's
        # source: two eggs, logged and committed
        banana = _log_and_commit(client, l0, "a banana")
        resolved = _log_and_commit(client, l1, "two eggs")
        history = [
            {"role": "user", "text": "two eggs"},
            {"role": "agent", "text": "Staged the eggs.", "log_id": l1, "state": "committed",
             "items": [f"{it['food']['name']} — {it['qty']} × {it['serving']['description']}"
                       for it in resolved]},
        ]

        # 1. "save that as …": the log the conversation names, saved now
        events = _post_stream(client, l2, f"save that as {name}", history)
        assert "save_meal" in _tool_calls(events), events
        done = next(e for e in events if e["type"] == "done")["log"]
        assert done["items"] == [] and done["saved_meal"], done
        assert done["saved_meal"]["name"].lower() == name, done["saved_meal"]
        assert done["saved_meal"]["item_count"] == len(resolved)
        row = macro()
        assert row is not None and row["is_active"], row
        assert [(i["food_id"], i["serving_id"]) for i in row["items"]] == [
            (it["food"]["id"], it["serving"]["id"]) for it in resolved
        ], row
        print(f"[agent live] saved {row['name']!r} triggers={row['trigger_phrases']} items={row['items']}")

        # 2. extend by words and change a quantity in one breath
        events = _post_stream(client, l3, f"add a banana to {name} and make the eggs 3")
        calls = _tool_calls(events)
        assert "save_meal" in calls and "update_meal" in calls, calls
        results = [e for e in events if e["type"] == "tool_result"]
        print(f"[agent live] meal tool results: {[(e['name'], e['output']) for e in results]}")
        row = macro()
        assert len(row["items"]) == len(resolved) + 1, row
        egg_id = resolved[0]["food"]["id"]
        assert [float(i["qty"]) for i in row["items"] if i["food_id"] == egg_id] == [3.0], row
        assert banana[0]["food"]["id"] in [i["food_id"] for i in row["items"]], row
        print(f"[agent live] extended: {row['items']}")

        # 3. rename — and the reply never states nutrition numbers
        events = _post_stream(client, l4, f"rename {name} to {renamed}")
        assert "update_meal" in _tool_calls(events), events
        row = macro()
        assert row["name"].lower() == renamed, row
        reply = next(e for e in events if e["type"] == "reply")["text"]
        assert "kcal" not in reply.lower() and "calorie" not in reply.lower(), reply
    finally:
        _run(db.execute("DELETE FROM meal_macros WHERE lower(name) LIKE %s", (f"%{tag}",)))
        # the aliases the two commits taught this tenant — after the rows that
        # reference them (item_resolutions.alias_id) are gone
        learned = [r["food_id"] for r in _run(db.fetchall(
            "SELECT DISTINCT food_id FROM entries WHERE log_id = ANY(%s::uuid[])", ([l0, l1],)
        ))]
        _run(_cleanup(t0, [l0, l1, l2, l3, l4]))
        # a run that died inside its own cleanup leaves this tenant rows on the
        # dead date, still referencing the aliases: sweep them first
        stale = [str(r["id"]) for r in _run(db.fetchall(
            "SELECT id FROM logs WHERE captured_at::date = %s", (LOCAL_DAY,)
        ))]
        if stale:
            _run(_cleanup(t0, stale))
        if learned:
            _run(db.execute("DELETE FROM aliases WHERE food_id = ANY(%s::uuid[])", (learned,)))
