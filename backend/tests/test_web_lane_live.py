"""Live tests for the tier-3 restaurant web lane (V4 — DESIGN.md §3.4 rule 2a,
§3.4a mode 2, §8.2 chains-only ruling).

1. sources.web.fetch_page — the deterministic fallback: a static page yields
   extracted text; non-http schemes and private hosts are refused offline.
2. THE §3.4a/§8.2 watch item: seed a searching chain item, run_tier3 →
   web-transcribed result with source_url on the chain's official domain (the
   run pinned it; the evidence gate let it through), the published protein in
   the staged macros, non-empty citations, a foods row carrying provenance,
   and the staged payload's `web_source` citation block. Hits the real Neon
   DB (inside one force-rollback transaction — nothing persists), the real
   OpenAI Responses API (hosted web_search + gpt-5.6-terra), and real Langfuse
   tracing. Prints web-lane latency and the web_search action count.

   Gate case: "Chick-fil-A chicken sandwich" — a server-rendered official page
   that fetch_page reads (11s live, 2026-08-21). Known-hostile case: "Big Mac"
   — mcdonalds.com product pages time out for every non-browser client (20s
   ReadTimeout with the app UA and a Chrome UA alike) and the hosted reader
   extracts the JS-rendered panel only sometimes (1 of 4 pinned runs), so the
   agent honestly answers not_found instead of estimating; kept as a
   non-strict xfail so a pass still proves the path and a miss is not noise.

Run:  cd backend && uv run --with pytest pytest tests/test_web_lane_live.py -q -s
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
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
from psycopg.rows import dict_row  # noqa: E402
from psycopg.types.json import Json  # noqa: E402

import db  # noqa: E402
import services.tier3 as tier3  # noqa: E402
from sources import web  # noqa: E402

pytestmark = pytest.mark.live


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. fetch_page — deterministic fallback
# ---------------------------------------------------------------------------


def test_fetch_page_static_page_returns_text():
    out = run(web.fetch_page("https://example.com/"))
    assert "error" not in out, out
    assert out["url"].startswith("https://example.com")
    assert out["title"] and "example" in out["title"].lower()
    assert "example domain" in out["text"].lower()
    assert "<" not in out["text"]  # tags stripped
    assert out["truncated"] is False


def test_fetch_page_policy_refusals_are_offline():
    assert run(web.fetch_page("file:///etc/passwd"))["error"]
    assert run(web.fetch_page("ftp://example.com/x"))["error"]
    assert run(web.fetch_page("http://localhost:8000/"))["error"] == "host not allowed"
    assert run(web.fetch_page("http://127.0.0.1/"))["error"] == "host not allowed"
    assert run(web.fetch_page("http://10.0.0.5/nutrition"))["error"] == "host not allowed"
    assert run(web.fetch_page("http://foo.internal/"))["error"] == "host not allowed"


def test_extract_prefers_main_and_strips_chrome():
    html = """<html><head><title>Big Mac &amp; Nutrition</title><style>x{}</style></head>
    <body><nav>Menu Home About</nav><script>var a=1;</script>
    <main><h1>Big Mac</h1><table><tr><td>Calories</td><td>590</td></tr>
    <tr><td>Protein</td><td>25 g</td></tr></table></main><footer>legal</footer></body></html>"""
    title, text = web._extract(html)
    assert title == "Big Mac & Nutrition"
    assert "Menu Home" not in text and "legal" not in text and "var a" not in text
    assert "Calories 590" in text and "Protein 25 g" in text


# ---------------------------------------------------------------------------
# 2. Big Mac through the web lane — live
# ---------------------------------------------------------------------------


@asynccontextmanager
async def isolated_db():
    conn = await psycopg.AsyncConnection.connect(db._database_url(), row_factory=dict_row)
    outer = conn.transaction(force_rollback=True)
    await outer.__aenter__()
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


async def seed_searching(conn, phrase: str, meal: str = "lunch") -> str:
    log_id = str(uuid4())
    await conn.execute(
        "INSERT INTO logs (id, input_mode, raw_input, captured_at) VALUES (%s, 'text', %s, now())",
        (log_id, phrase),
    )
    await conn.execute(
        """
        INSERT INTO item_resolutions (log_id, item_index, phrase, phrase_norm, status, staged)
        VALUES (%s, 0, %s, %s, 'searching', %s)
        """,
        (log_id, phrase, phrase.lower(),
         Json({"item_index": 0, "phrase": phrase, "status": "searching", "meal": meal})),
    )
    return log_id


CASES = [
    pytest.param(
        "Chick-fil-A chicken sandwich", "chick-fil-a.com", (27, 31), (380, 460),
        id="chick-fil-a",
    ),
    pytest.param(
        "Big Mac", "mcdonalds.com", (22, 28), (500, 700),
        id="big-mac",
        marks=pytest.mark.xfail(
            strict=False,
            reason="mcdonalds.com product pages time out for non-browser clients and the "
                   "hosted reader extracts the JS-rendered panel only sometimes (1/4 pinned "
                   "runs, 2026-08-21); the agent then answers not_found rather than estimate",
        ),
    ),
]


@pytest.mark.parametrize("phrase, host, protein_range, kcal_range", CASES)
def test_chain_item_resolves_through_web_lane(phrase, host, protein_range, kcal_range):
    async def scenario():
        async with isolated_db() as conn:
            log_id = await seed_searching(conn, phrase)
            t0 = time.perf_counter()
            item = await tier3.run_tier3(log_id, 0)
            latency = time.perf_counter() - t0

            row = await (await conn.execute(
                "SELECT status, tier, staged, proposed_food_id::text, trace_id, agent_transcript, "
                "network_calls, resolve_ms FROM item_resolutions WHERE log_id = %s AND item_index = 0",
                (log_id,),
            )).fetchone()
            transcript = row["agent_transcript"] or []
            if isinstance(transcript, str):
                transcript = json.loads(transcript)
            searches = [t for t in transcript if isinstance(t, dict) and t.get("type") == "web_search_call"]
            calls = [t.get("name") for t in transcript if isinstance(t, dict) and t.get("type") == "function_call"]
            print(
                f"\n[web lane live] {phrase!r}: {latency:.1f}s status={item.status} "
                f"confidence={item.confidence} web_search_actions={len(searches)} "
                f"function_calls={calls} network_calls={row['network_calls']}"
            )

            assert item.status == "resolved", f"expected resolved, got {item.status}: {item}"
            assert item.tier == 3 and item.food is not None and item.macros is not None
            assert row["status"] == "resolved" and row["proposed_food_id"] == item.food.id

            frow = await (await conn.execute(
                "SELECT source, source_id, source_url, provenance, brand, display_name, servings "
                "FROM foods WHERE id = %s",
                (item.food.id,),
            )).fetchone()
            assert frow["source"] == "web", f"not the web lane: source={frow['source']} {frow['display_name']}"
            assert host in (frow["source_url"] or ""), frow["source_url"]
            prov = frow["provenance"] or {}
            assert prov.get("citations"), "foods.provenance carries no citations"
            assert all(c.get("url", "").startswith("http") for c in prov["citations"])
            assert prov.get("values_as_read", {}).get("protein_g") is not None
            assert searches, "no web_search_call in the archived transcript"
            # §3.4: the lane is pinned to the official domain (add_food refuses otherwise)
            assert "pin_chain_domain" in calls, calls
            # §4: provenance also records what the hosted search actually consulted —
            # and the evidence gate let this food through, so the page's host is in it
            assert prov.get("web_searches"), "provenance lacks the web_search sources list"
            consulted = {tier3._url_host(u) for s in prov["web_searches"] for u in s["sources"]}
            consulted |= {tier3._url_host(c["url"]) for c in prov.get("url_citations", [])}
            print(f"[web lane live] consulted hosts={sorted(consulted)}")
            # §9: hosted search actions count as network calls (plus any fetch_page)
            assert row["network_calls"] >= len(searches) >= 1, row["network_calls"]

            # the published panel, per item (Big Mac ~25 g protein; CFA sandwich 29 g)
            assert item.qty == 1.0, item.qty
            assert protein_range[0] <= item.macros.protein_g <= protein_range[1], item.macros
            assert kcal_range[0] <= item.macros.kcal <= kcal_range[1], item.macros
            assert not item.state_question

            # §3.4a REQUIREMENT: the staged payload carries the clickable citation
            staged = row["staged"]
            ws = staged.get("web_source")
            assert ws and host in ws["url"] and ws["title"], staged
            assert row["trace_id"], "trace_id not stamped"
            print(
                f"[web lane live] food='{frow['display_name']}' brand={frow['brand']!r} "
                f"serving='{item.serving.description}' macros={item.macros.model_dump(exclude_none=True)} "
                f"source_url={frow['source_url']} citations={len(prov['citations'])} "
                f"web_source={ws}"
            )

    run(scenario())
