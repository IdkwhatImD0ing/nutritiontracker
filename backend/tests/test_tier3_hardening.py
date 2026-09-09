"""Tier-3 hardening (V4 — BACKLOG 25, 27, 29 + the §3.4a web_transcribed gate).

1. BACKLOG 27: _write_item_row is a conditional UPDATE — live statuses accept
   the write, a removed or committed row refuses it (never resurrected).
2. BACKLOG 25: _apply_portion on the switch/answer/manual-select paths yields
   exactly what cascade.resolve_portion yields for the same alias row (§7.3
   order: spoken qty > alias mode > food mode > provider default).
3. BACKLOG 29: resolve_item_from_source whitelists the client-supplied source
   BEFORE any DB read.
4. §3.4a mode 2: the web payload schema requires source_url + citations, the
   code-side backstop rejects hollow provenance, add_food's web mode persists
   source='web' + source_url + provenance through foods_repo, and the staged
   payload carries the clickable-citation block under `web_source`.
5. Phase-3 panel (web lane): a web food's identity is URL + item + values,
   never the URL alone (two items from one chain page = two rows); add_food's
   web mode requires the run to be pinned to the chain's official domain and
   the source_url to sit on it; and run_tier3 drops a web food whose page the
   run never actually searched, cited, or fetched (estimation has no code
   path — enforced, not prompted). Offline: Runner and tracing are stubbed.

DB tests run inside one force-rollback transaction (same harness as
test_m1_findings.py) — nothing persists. No LLM calls.

Run:  cd backend && uv run --with pytest pytest tests/test_tier3_hardening.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import psycopg  # noqa: E402
import pytest  # noqa: E402
from agents.tool_context import ToolContext  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
from psycopg.types.json import Json  # noqa: E402
from pydantic import ValidationError  # noqa: E402

import db  # noqa: E402

from tenancy_harness import bind_test_tenant  # noqa: E402
import services.tier3 as tier3  # noqa: E402
from models import FoodRef, Meal, ServingRef, StagedItem  # noqa: E402
from resolver import cascade  # noqa: E402
from resolver.norm import norm  # noqa: E402

# ---------------------------------------------------------------------------
# rollback harness
# ---------------------------------------------------------------------------


@asynccontextmanager
async def isolated_db():
    conn = await psycopg.AsyncConnection.connect(db._database_url(), row_factory=dict_row)
    outer = conn.transaction(force_rollback=True)
    await outer.__aenter__()
    await bind_test_tenant(conn)  # 002: app role + the fixed test tenant, inside the rollback
    orig = (db.fetchall, db.fetchone, db.execute, db.tx)

    async def fetchall(sql, params=()):
        cur = await conn.execute(sql, params)
        return await cur.fetchall()

    async def fetchone(sql, params=()):
        cur = await conn.execute(sql, params)
        return await cur.fetchone()

    async def execute(sql, params=()):
        cur = await conn.execute(sql, params)
        return cur.rowcount

    @asynccontextmanager
    async def tx():
        async with conn.transaction():
            yield conn

    db.fetchall, db.fetchone, db.execute, db.tx = fetchall, fetchone, execute, tx
    try:
        yield conn
    finally:
        db.fetchall, db.fetchone, db.execute, db.tx = orig
        await outer.__aexit__(None, None, None)
        await conn.close()


MASS = {
    "serving_id": "100g", "description": "100 g", "grams": 100.0,
    "macros": {"kcal": 165, "protein_g": 31, "carb_g": 0, "fat_g": 3.6},
}
COUNT = {
    "serving_id": "p1", "description": "1 breast (120 g)", "grams": 120.0,
    "macros": {"kcal": 198, "protein_g": 37.2, "carb_g": 0, "fat_g": 4.3},
}


async def insert_food(conn, name, servings, default_serving):
    cur = await conn.execute(
        """
        INSERT INTO foods (source, source_id, name, display_name, food_type,
                           state_sensitive, servings, default_serving_id, payload)
        VALUES ('fdc', %s, %s, %s, 'generic', false, %s, %s, '{}'::jsonb)
        RETURNING id
        """,
        (uuid4().hex, name, name, Json(servings), default_serving),
    )
    return str((await cur.fetchone())["id"])


async def insert_log(conn):
    log_id = str(uuid4())
    await conn.execute(
        "INSERT INTO logs (id, input_mode, raw_input, captured_at) VALUES (%s, 'text', 'x', now())",
        (log_id,),
    )
    return log_id


async def seed_item(conn, log_id, idx, phrase, *, status, committed=False, staged=None):
    staged = staged or {"item_index": idx, "phrase": phrase, "status": status, "meal": "lunch"}
    await conn.execute(
        """
        INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm, status,
                                      staged, committed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (log_id, idx, phrase, norm(phrase), status, Json(staged), None),
    )
    if committed:
        await conn.execute(
            "UPDATE item_resolutions SET committed_at = now() WHERE log_id = %s AND item_index = %s",
            (log_id, idx),
        )


async def item_row(conn, log_id, idx):
    cur = await conn.execute(
        "SELECT * FROM item_resolutions WHERE log_id = %s AND item_index = %s", (log_id, idx)
    )
    return await cur.fetchone()


def resolved_item(idx, phrase, food_id):
    return StagedItem(
        item_index=idx, phrase=phrase, status="resolved", tier=3,
        food=FoodRef(id=food_id, name="x"), serving=ServingRef(id="100g", description="100 g"),
        qty=1.0, meal=Meal.lunch, confidence="high",
    )


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. BACKLOG 27 — _write_item_row state guards
# ---------------------------------------------------------------------------


def test_write_item_row_state_guard_table():
    async def scenario():
        async with isolated_db() as conn:
            fid = await insert_food(conn, "guard food", [MASS], "100g")
            log_id = await insert_log(conn)
            cases = [  # (idx, seeded status, committed, expect write accepted)
                (0, "searching", False, True),
                (1, "needs_input", False, True),
                (2, "not_found", False, True),
                (3, "resolved", False, True),
                (4, "removed", False, False),  # user removed the row mid-run
                (5, "resolved", True, False),  # item already committed
            ]
            for idx, status, committed, _ in cases:
                await seed_item(conn, log_id, idx, f"guard {idx}", status=status, committed=committed)
            for idx, status, committed, expect in cases:
                ok = await tier3._write_item_row(
                    log_id, idx, resolved_item(idx, f"guard {idx}", fid),
                    proposed_food_id=fid, resolve_ms=5,
                    web_source={"url": "https://example.com/n", "title": "Nutrition"} if idx == 0 else None,
                )
                row = await item_row(conn, log_id, idx)
                assert bool(ok) is expect, f"idx={idx} seeded={status} committed={committed}: got {ok}"
                if expect:
                    assert row["status"] == "resolved" and row["tier"] == 3
                    assert row["proposed_food_id"] is not None
                else:
                    # untouched: status, tier, proposed_* all as seeded
                    assert row["status"] == status
                    assert row["tier"] is None and row["proposed_food_id"] is None
                    assert row["staged"]["status"] == status
            # §3.4a: the clickable-citation block rides in the staged jsonb
            row0 = await item_row(conn, log_id, 0)
            assert row0["staged"]["web_source"] == {"url": "https://example.com/n", "title": "Nutrition"}
            row1 = await item_row(conn, log_id, 1)
            assert "web_source" not in row1["staged"]

    run(scenario())


# ---------------------------------------------------------------------------
# 2. BACKLOG 25 — portion parity with cascade.resolve_portion
# ---------------------------------------------------------------------------


def test_apply_portion_matches_cascade_for_alias_history():
    async def scenario():
        async with isolated_db() as conn:
            phrase = "parity chicken " + uuid4().hex[:6]
            fid = await insert_food(conn, "parity chicken", [MASS, COUNT], "100g")
            cur = await conn.execute(
                "INSERT INTO aliases (alias_norm, food_id, default_serving_id, default_qty, origin) "
                "VALUES (%s, %s, 'p1', 3, 'manual') RETURNING id",
                (norm(phrase), fid),
            )
            alias_id = str((await cur.fetchone())["id"])
            # committed history: p1×2 three times, p1×1 once → mode p1×2 (beats
            # the alias column default p1×3 and the provider default 100g×1)
            log_id = await insert_log(conn)
            for i, q in enumerate((2, 1, 2, 2)):
                await conn.execute(
                    """
                    INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm, status,
                        staged, alias_id, final_food_id, final_serving_id, final_qty, committed_at)
                    VALUES (%s, %s, %s, %s, 'resolved', '{}'::jsonb, %s, %s, 'p1', %s,
                            now() - (%s || ' minutes')::interval)
                    """,
                    (log_id, i, phrase, norm(phrase), alias_id, fid, q, i),
                )
            food = {"servings": [MASS, COUNT], "default_serving_id": "100g"}
            alias_row = (await conn.execute(cascade._ALIAS_SQL, (norm(phrase),)))
            alias_row = await alias_row.fetchone()

            # no spoken quantity → alias mode-of-10 (rule 2), identical on both paths
            expected = await cascade.resolve_portion(alias_row, None, None)
            got = await tier3._apply_portion(food, None, None, None, food_row_id=fid, phrase=phrase)
            assert got == expected == ("p1", 2.0, False)

            # spoken count without unit → count serving × qty (rule 1) on both
            expected = await cascade.resolve_portion(alias_row, 4.0, None)
            got = await tier3._apply_portion(food, None, 4.0, None, food_row_id=fid, phrase=phrase)
            assert got == expected == ("p1", 4.0, False)

            # spoken grams → gram serving (rule 1) on both
            expected = await cascade.resolve_portion(alias_row, 240.0, "g")
            got = await tier3._apply_portion(food, None, 240.0, "g", food_row_id=fid, phrase=phrase)
            assert got == expected == ("100g", 2.4, False)

            # a NEW phrase for the same food (no alias) → the food's most-logged
            # portion across all aliases (rule 3), not the provider default
            got = await tier3._apply_portion(
                food, None, None, None, food_row_id=fid, phrase="never spoken before"
            )
            assert got == ("p1", 2.0, False)

            # no history at all → the agent's chosen serving × 1 (rule 4)
            fid2 = await insert_food(conn, "fresh food", [MASS, COUNT], "100g")
            got = await tier3._apply_portion(food, "p1", None, None, food_row_id=fid2, phrase="fresh")
            assert got == ("p1", 1.0, False)

    run(scenario())


# ---------------------------------------------------------------------------
# 3. BACKLOG 29 — source whitelist before any read
# ---------------------------------------------------------------------------


def test_resolve_item_from_source_rejects_unlisted_source_before_db():
    async def scenario():
        orig = db.fetchone

        async def boom(*a, **kw):
            raise AssertionError("DB must not be touched for an invalid source")

        db.fetchone = boom
        try:
            for bad in ("branded", "FDC", "fdc ", "", "web;drop"):
                with pytest.raises(ValueError):
                    await tier3.resolve_item_from_source(str(uuid4()), 0, bad, "1")
        finally:
            db.fetchone = orig
        assert tier3.ALLOWED_SOURCES == ("fdc", "off", "web")

    run(scenario())


# ---------------------------------------------------------------------------
# 4. §3.4a mode 2 — the web_transcribed gate
# ---------------------------------------------------------------------------

VALUES = dict(
    serving_description="1 burger (219 g)", grams=219, kcal=590, protein_g=25,
    carb_g=46, fat_g=34, fiber_g=None, sugar_g=9, sodium_mg=1050,
)


def test_web_payload_schema_requires_url_and_citations():
    with pytest.raises(ValidationError):
        tier3.WebTranscription(page_title=None, brand=None, values=VALUES, citations=[])  # no source_url
    with pytest.raises(ValidationError):
        tier3.WebTranscription(source_url="https://x", page_title=None, brand=None, values=VALUES)  # no citations
    # schema-valid but hollow provenance → the code backstop refuses it
    hollow = tier3.WebTranscription(
        source_url="https://www.mcdonalds.com/us/en-us/product/big-mac.html",
        page_title="Big Mac", brand="McDonald's", values=VALUES, citations=[],
    )
    assert "citations" in tier3._web_transcription_error(hollow)
    bad_url = hollow.model_copy(update={
        "source_url": "mcdonalds.com/big-mac",
        "citations": [tier3.WebCitation(url="https://www.mcdonalds.com/x", title=None)],
    })
    assert "source_url" in tier3._web_transcription_error(bad_url)
    good = bad_url.model_copy(update={"source_url": "https://www.mcdonalds.com/us/en-us/product/big-mac.html"})
    assert tier3._web_transcription_error(good) is None


def test_food_from_web_shape_and_staged_citation_block():
    web = tier3.WebTranscription(
        source_url="https://www.mcdonalds.com/us/en-us/product/big-mac.html",
        page_title="Big Mac: Calories & Nutrition",
        brand="McDonald's",
        values=VALUES,
        citations=[tier3.WebCitation(url="https://www.mcdonalds.com/us/en-us/product/big-mac.html", title="Big Mac")],
    )
    food = tier3._food_from_web(web, "Big Mac")
    assert food["source"] == "web" and len(food["source_id"]) == 16
    assert food["source_url"] == web.source_url
    assert food["provenance"]["citations"][0]["url"] == web.source_url
    assert food["provenance"]["values_as_read"]["protein_g"] == 25
    assert food["servings"][0]["macros"]["protein_g"] == 25 and food["servings"][0]["grams"] == 219
    assert food["default_serving_id"] == food["servings"][0]["serving_id"]
    assert food["food_type"] == "branded" and food["brand"] == "McDonald's"
    assert not tier3._needs_state_question(food, "big mac")
    assert tier3._web_source(food) == {"url": web.source_url, "title": "Big Mac: Calories & Nutrition"}
    assert tier3._web_source({"source": "fdc", "source_url": None}) is None


def test_web_evidence_from_transcript():
    transcript = [
        {"type": "message", "role": "user", "content": "x"},
        {"type": "web_search_call", "status": "completed",
         "action": {"type": "search", "query": "big mac nutrition",
                    "sources": [{"type": "url", "url": "https://www.mcdonalds.com/a"},
                                {"type": "url", "url": "https://www.mcdonalds.com/b"}]}},
        {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "…", "annotations": [
                {"type": "url_citation", "url": "https://www.mcdonalds.com/a", "title": "Big Mac"},
                {"type": "url_citation", "url": "https://www.mcdonalds.com/a", "title": "dup"},
            ]}]},
    ]
    evidence, n = tier3._web_evidence(transcript)
    assert n == 1
    assert evidence["web_searches"] == [{"query": "big mac nutrition",
                                         "sources": ["https://www.mcdonalds.com/a", "https://www.mcdonalds.com/b"]}]
    assert evidence["url_citations"] == [{"url": "https://www.mcdonalds.com/a", "title": "Big Mac"}]
    assert tier3._web_evidence(None) == ({}, 0)


def _tool(agent, name):
    return next(t for t in agent.tools if getattr(t, "name", None) == name)


def _ctx(name, args):
    return ToolContext(context=None, tool_name=name, tool_call_id="call_1", tool_arguments=args)


def test_add_food_web_mode_persists_provenance_and_refuses_hollow_payload():
    async def scenario():
        async with isolated_db() as conn:
            counter, cache, persisted, run_state = {"network_calls": 0}, {}, {}, {}
            agent = tier3._build_agent(counter, cache, persisted, run_state)
            add_food = _tool(agent, "add_food")
            url = "https://www.mcdonalds.com/us/en-us/product/big-mac.html"
            full = json.dumps({
                "source": "web", "source_id": None, "display_name": "Big Mac",
                "web": {"source_url": url, "page_title": "Big Mac", "brand": "McDonald's",
                        "citations": [{"url": url, "title": "Big Mac"}], "values": VALUES},
            })

            # web mode without the payload → error, nothing persisted
            args = json.dumps({"source": "web", "source_id": None, "display_name": "Big Mac", "web": None})
            out = json.loads(await add_food.on_invoke_tool(_ctx("add_food", args), args))
            assert out["error"] == "missing_web_payload" and not persisted

            # §3.4: the lane is pinned to the official site IN CODE — no pin, no web food
            out = json.loads(await add_food.on_invoke_tool(_ctx("add_food", full), full))
            assert out["error"] == "pin_required" and not persisted

            # pin_chain_domain rewrites the hosted tool's allowed_domains + the run state
            pin = _tool(agent, "pin_chain_domain")
            hosted = next(t for t in agent.tools if t.__class__.__name__ == "WebSearchTool")
            assert hosted.filters is None and run_state["pinned_domain"] is None
            args = json.dumps({"domain": "https://www.McDonalds.com/us/en-us"})
            assert json.loads(await pin.on_invoke_tool(_ctx("pin_chain_domain", args), args)) == {
                "pinned_domain": "mcdonalds.com"
            }
            assert hosted.filters.allowed_domains == ["mcdonalds.com"]
            assert run_state["pinned_domain"] == "mcdonalds.com"
            args = json.dumps({"domain": "not a domain"})
            assert json.loads(await pin.on_invoke_tool(_ctx("pin_chain_domain", args), args))["error"] == "invalid_domain"
            assert hosted.filters.allowed_domains == ["mcdonalds.com"]  # unchanged

            # a source_url off the pinned domain (third-party nutrition site) → refused
            off = json.loads(full)
            off["web"]["source_url"] = "https://www.nutritionix.com/mcdonalds/big-mac"
            off = json.dumps(off)
            out = json.loads(await add_food.on_invoke_tool(_ctx("add_food", off), off))
            assert out["error"] == "off_domain" and not persisted
            # ...a subdomain of the pinned domain is fine
            sub = json.loads(full)
            sub["web"]["source_url"] = "https://nutrition.mcdonalds.com/big-mac"
            assert tier3._host_within(tier3._url_host(sub["web"]["source_url"]), run_state["pinned_domain"])

            # pinned + official page + full payload → foods row: source='web', source_url, provenance
            out = json.loads(await add_food.on_invoke_tool(_ctx("add_food", full), full))
            assert out["source"] == "web" and out["source_id"] and out["food_id"]
            assert persisted[("web", out["source_id"])] == out["food_id"]
            row = await (await conn.execute(
                "SELECT source, source_id, source_url, provenance, servings, brand FROM foods WHERE id = %s",
                (out["food_id"],),
            )).fetchone()
            assert row["source"] == "web" and row["source_url"] == url
            assert row["provenance"]["citations"][0]["url"] == url
            assert row["provenance"]["values_as_read"]["kcal"] == 590
            assert row["servings"][0]["macros"]["protein_g"] == 25
            assert counter["network_calls"] == 0  # no remote fetch for a web food

            # the run cache serves the web food to the resolution path (no network)
            food = await tier3._get_food_cached("web", out["source_id"], counter, cache)
            assert food is not None and food["source_url"] == url
            assert counter["network_calls"] == 0
            # an unknown web id is a miss, never a remote call
            assert await tier3._get_food_cached("web", "nope", counter, cache) is None
            assert counter["network_calls"] == 0

            # the agent's model settings include the consulted-sources list (§3.4)
            assert "web_search_call.action.sources" in agent.model_settings.response_include

    run(scenario())


# ---------------------------------------------------------------------------
# 5. Phase-3 panel — web-food identity, official-domain pin, evidence gate
# ---------------------------------------------------------------------------

CALC_URL = "https://www.mcdonalds.com/us/en-us/full-menu/nutrition-calculator.html"
MCCHICKEN = dict(
    serving_description="1 sandwich (143 g)", grams=143, kcal=400, protein_g=14,
    carb_g=39, fat_g=21, fiber_g=None, sugar_g=5, sodium_mg=560,
)


def _web_payload(url=CALC_URL, values=VALUES, title="Nutrition Calculator"):
    return {"source_url": url, "page_title": title, "brand": "McDonald's",
            "citations": [{"url": url, "title": title}], "values": values}


def test_web_food_identity_is_url_plus_item_never_url_alone():
    """Blocker: chain pages/calculators list many items under ONE url, and
    persist_food's ON CONFLICT returns the existing row — a URL-only identity
    staged the second item's numbers over the first item's row."""
    big_mac = tier3.WebTranscription(**_web_payload())
    mcchicken = tier3.WebTranscription(**_web_payload(values=MCCHICKEN))
    a = tier3._food_from_web(big_mac, "Big Mac")
    b = tier3._food_from_web(mcchicken, "McChicken")
    assert a["source_id"] != b["source_id"] and a["source_url"] == b["source_url"]
    # the same item re-read identically → the same identity (dedup survives)
    assert tier3._food_from_web(big_mac, "big mac")["source_id"] == a["source_id"]
    assert tier3._food_from_web(big_mac, "The Big Mac")["source_id"] == a["source_id"]
    # a changed label (new published values) is a new identity, never a silent overwrite
    relabeled = tier3.WebTranscription(**_web_payload(values={**VALUES, "kcal": 580}))
    assert tier3._food_from_web(relabeled, "Big Mac")["source_id"] != a["source_id"]

    async def scenario():
        async with isolated_db() as conn:
            counter, cache, persisted, run_state = {"network_calls": 0}, {}, {}, {}
            agent = tier3._build_agent(counter, cache, persisted, run_state)
            run_state["pinned_domain"] = "mcdonalds.com"
            add_food = _tool(agent, "add_food")
            ids = []
            for name, values in (("Big Mac", VALUES), ("McChicken", MCCHICKEN), ("Big Mac", VALUES)):
                args = json.dumps({"source": "web", "source_id": None, "display_name": name,
                                   "web": _web_payload(values=values)})
                out = json.loads(await add_food.on_invoke_tool(_ctx("add_food", args), args))
                assert "error" not in out, out
                ids.append(out["food_id"])
            assert ids[0] != ids[1], "two items from one URL collapsed into one foods row"
            assert ids[0] == ids[2], "the same item re-added must dedup to its row"
            rows = await (await conn.execute(
                "SELECT display_name, source_url, servings FROM foods WHERE id = ANY(%s::uuid[]) "
                "ORDER BY display_name",
                (ids[:2],),
            )).fetchall()
            assert [r["display_name"] for r in rows] == ["Big Mac", "McChicken"]
            assert {r["source_url"] for r in rows} == {CALC_URL}
            assert rows[0]["servings"][0]["macros"]["kcal"] == 590
            assert rows[1]["servings"][0]["macros"]["kcal"] == 400

    run(scenario())


def test_web_evidence_host_rules():
    assert tier3._url_host("https://WWW.McDonalds.com/us/x") == "mcdonalds.com"
    assert tier3._url_host("nutrition.mcdonalds.com/x") == ""  # no scheme → no host
    assert tier3._url_host("") == ""
    ev = {"web_searches": [{"query": "q", "sources": ["https://www.mcdonalds.com/a"]}]}
    assert tier3._web_evidence_gap("https://mcdonalds.com/b", ev, {}) is None
    assert tier3._web_evidence_gap("https://nutrition.mcdonalds.com/b", ev, {}) is None
    assert "never consulted" in tier3._web_evidence_gap("https://www.nutritionix.com/x", ev, {})
    assert "nothing" in tier3._web_evidence_gap("https://www.mcdonalds.com/b", {}, {})
    # fetch_page reads and url_citation annotations count as evidence too
    assert tier3._web_evidence_gap(
        "https://www.chipotle.com/n", {}, {"fetched_hosts": {"chipotle.com"}}
    ) is None
    cited = {"url_citations": [{"url": "https://www.sweetgreen.com/menu", "title": None}]}
    assert tier3._web_evidence_gap("https://sweetgreen.com/menu", cited, {}) is None
    # open_page / find actions carry their url (no `sources` list)
    ev, n = tier3._web_evidence([{"type": "web_search_call", "action": {
        "type": "open_page", "url": "https://www.chipotle.com/nutrition-calculator"}}])
    assert n == 1 and ev["web_searches"] == [
        {"query": None, "sources": ["https://www.chipotle.com/nutrition-calculator"]}
    ]


class _FakeRun:
    def __init__(self, out, transcript):
        self._out, self._transcript = out, transcript

    def final_output_as(self, _cls):
        return self._out

    def to_input_list(self):
        return list(self._transcript)


def _fake_runner(transcript, *, pin="mcdonalds.com", fetch=None, url=CALC_URL):
    """A Runner whose run() drives the REAL tool closures (pin → optional
    fetch_page → add_food web mode) and answers with the web food it created."""

    class FakeRunner:
        @staticmethod
        async def run(agent, agent_input, max_turns):
            if pin:
                args = json.dumps({"domain": pin})
                await _tool(agent, "pin_chain_domain").on_invoke_tool(_ctx("pin_chain_domain", args), args)
            if fetch:
                args = json.dumps({"url": fetch})
                page = json.loads(await _tool(agent, "fetch_page").on_invoke_tool(_ctx("fetch_page", args), args))
                assert "error" not in page, page
            args = json.dumps({"source": "web", "source_id": None, "display_name": "Big Mac",
                               "web": _web_payload(url=url)})
            out = json.loads(await _tool(agent, "add_food").on_invoke_tool(_ctx("add_food", args), args))
            assert "error" not in out, out
            return _FakeRun(tier3.Tier3Result(
                status="resolved", source="web", source_id=out["source_id"], serving_id="web",
                display_name="Big Mac", confidence="high", alternates=[], question=None,
            ), transcript)

    return FakeRunner


@asynccontextmanager
async def _offline_run(runner):
    """run_tier3 with no OpenAI and no FireTrace — only the (rolled-back) DB."""

    class _Run:
        trace_id = "trace-offline"
        span_id = "span-offline"

        def set(self, **kw):
            return self

    @asynccontextmanager
    async def run(name, **kw):
        yield _Run()

    tr = tier3.tracing
    # Every SDK name _load_sdk binds, snapshotted and put back: this fixture's
    # whole claim is that the module is left as it was found, and restoring
    # Runner alone would leave the other six bound to the real SDK for whatever
    # runs next. `install_agents_hook` joins init_tracing in the stubbed set —
    # _load_sdk calls it directly, and the real one registers a live FireTrace
    # processor on the SDK that no teardown here would remove.
    sdk_names = (
        "Agent", "MaxTurnsExceeded", "ModelSettings", "Runner",
        "WebSearchTool", "WebSearchFilters", "function_tool",
    )
    sdk_orig = {name: getattr(tier3, name) for name in sdk_names}
    orig = (tr.init_tracing, tr.install_agents_hook, tr.run, tr.flush)
    tier3.Runner = runner
    tr.init_tracing, tr.install_agents_hook = lambda: None, lambda: None
    tr.run, tr.flush = run, lambda: None
    try:
        yield
    finally:
        for name, value in sdk_orig.items():
            setattr(tier3, name, value)
        tr.init_tracing, tr.install_agents_hook, tr.run, tr.flush = orig


def test_run_tier3_drops_web_food_without_real_evidence():
    """Major: 'estimation has no code path' enforced after the run — a web
    food whose page the run never searched/cited/fetched is demoted to
    not_found and its row dropped; real evidence (search sources, or a
    fetch_page read) lets it through with macros from the persisted row."""
    searched = [{"type": "web_search_call", "status": "completed",
                 "action": {"type": "search", "query": "big mac nutrition",
                            "sources": [{"type": "url", "url": CALC_URL}]}}]

    async def fake_fetch(url):
        return {"url": url, "title": "Nutrition Calculator", "text": "Big Mac Protein 25 g", "truncated": False}

    async def web_rows(conn):
        return await (await conn.execute(
            "SELECT id::text AS id, provenance FROM foods WHERE source = 'web' AND source_url = %s", (CALC_URL,)
        )).fetchall()

    async def scenario():
        async with isolated_db() as conn:
            log_id = await insert_log(conn)
            for idx in range(4):
                await seed_item(conn, log_id, idx, "Big Mac", status="searching")

            # 1. add_food from memory on turn 1 (no web_search_call, no fetch) → dropped
            async with _offline_run(_fake_runner([])):
                item = await tier3.run_tier3(log_id, 0)
            assert item.status == "not_found" and item.food is None and item.web_source is None
            row = await item_row(conn, log_id, 0)
            assert row["status"] == "not_found" and row["proposed_food_id"] is None
            assert row["staged"].get("web_source") is None
            assert await web_rows(conn) == [], "the unverified web food must not survive"
            gate = [t for t in row["agent_transcript"] if t.get("type") == "usual_web_gate"]
            assert gate and "never consulted" in gate[0]["web_evidence_gap"]

            # 2. a search whose sources list the page → resolved, numbers from the row
            async with _offline_run(_fake_runner(searched)):
                item = await tier3.run_tier3(log_id, 1)
            assert item.status == "resolved" and item.food is not None
            assert item.macros.protein_g == 25 and item.macros.kcal == 590
            assert item.web_source.url == CALC_URL
            rows = await web_rows(conn)
            assert len(rows) == 1 and rows[0]["id"] == item.food.id
            assert rows[0]["provenance"]["web_searches"][0]["sources"] == [CALC_URL]
            row = await item_row(conn, log_id, 1)
            assert row["status"] == "resolved" and str(row["proposed_food_id"]) == item.food.id
            assert row["staged"]["web_source"]["url"] == CALC_URL

            # 3. a page the run never touched, even with a real search elsewhere → dropped
            elsewhere = [{"type": "web_search_call", "action": {"type": "search", "query": "x",
                          "sources": [{"type": "url", "url": "https://www.mcdonalds.com/us/en-us/product/fries.html"}]}}]
            async with _offline_run(_fake_runner(elsewhere, url="https://www.mcdonalds.com/us/en-us/product/big-mac.html")):
                item = await tier3.run_tier3(log_id, 2)
            # same host consulted → passes (host-level evidence; the page itself is the model's claim)
            assert item.status == "resolved"
            async with _offline_run(_fake_runner(
                [{"type": "web_search_call", "action": {"type": "search", "query": "x",
                  "sources": [{"type": "url", "url": "https://www.burgerking.com/menu"}]}}],
            )):
                await seed_item(conn, log_id, 4, "Big Mac", status="searching")
                item = await tier3.run_tier3(log_id, 4)
            assert item.status == "not_found"

            # 4. fetch_page as the evidence (JS-calculator fallback) → resolved
            orig_fetch = tier3.web.fetch_page
            tier3.web.fetch_page = fake_fetch
            try:
                async with _offline_run(_fake_runner([], fetch=CALC_URL)):
                    item = await tier3.run_tier3(log_id, 3)
            finally:
                tier3.web.fetch_page = orig_fetch
            assert item.status == "resolved" and item.web_source.url == CALC_URL
            row = await item_row(conn, log_id, 3)
            assert row["network_calls"] == 1  # the fetch

    run(scenario())


def test_fetch_page_refuses_a_repeat_of_a_failed_url():
    """Bot-hostile chain pages time out for any non-browser client (McDonald's,
    live 2026-08-21); a retry of the same URL burns a turn + a timeout for the
    same answer, so the tool refuses it immediately and counts no network call.
    A successful fetch records its host as evidence for the post-run gate."""
    calls = []

    async def fake_fetch(url):
        calls.append(url)
        if "timeout" in url:
            raise TimeoutError("boom")
        if "pdf" in url:
            return {"url": url, "error": "unsupported content type 'application/pdf'"}
        return {"url": url.replace("chick-fil-a.com", "www.chick-fil-a.com"), "title": "t", "text": "Protein 29 g", "truncated": False}

    async def scenario():
        counter, cache, persisted, run_state = {"network_calls": 0}, {}, {}, {}
        agent = tier3._build_agent(counter, cache, persisted, run_state)
        fetch = _tool(agent, "fetch_page")
        orig = tier3.web.fetch_page
        tier3.web.fetch_page = fake_fetch
        try:
            bad = json.dumps({"url": "https://www.mcdonalds.com/timeout.html"})
            out = await fetch.on_invoke_tool(_ctx("fetch_page", bad), bad)
            assert "TimeoutError" in out and "failed" in out  # sanitized failure text
            out = json.loads(await fetch.on_invoke_tool(_ctx("fetch_page", bad), bad))
            assert "already failed" in out["error"]
            assert calls == ["https://www.mcdonalds.com/timeout.html"]  # no second network call
            assert counter["network_calls"] == 1

            pdf = json.dumps({"url": "https://www.mcdonalds.com/menu.pdf"})
            assert "error" in json.loads(await fetch.on_invoke_tool(_ctx("fetch_page", pdf), pdf))
            assert "already failed" in json.loads(await fetch.on_invoke_tool(_ctx("fetch_page", pdf), pdf))["error"]
            assert run_state["fetched_hosts"] == set()  # failures are not evidence

            good = json.dumps({"url": "https://chick-fil-a.com/menu/chicken-sandwich"})
            out = json.loads(await fetch.on_invoke_tool(_ctx("fetch_page", good), good))
            assert out["text"] == "Protein 29 g"
            assert run_state["fetched_hosts"] == {"chick-fil-a.com"}
            assert counter["network_calls"] == 3
        finally:
            tier3.web.fetch_page = orig

    run(scenario())
