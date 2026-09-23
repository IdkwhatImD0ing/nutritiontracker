"""The MCP server at POST /api/mcp (mcp_app.py), offline.

Every request goes through the assembled app with its lifespan running
(`with TestClient(main.app)`: without it the SDK's session managers have no
task group). No database: where a test is not about auth, auth.authenticate
is faked with one that binds the tenant the way the real one does, and each
tool's handler is faked on its route module (the tools look handlers up at
call time). Both protocol paths are exercised: the 2025 handshake era
(`MCP-Protocol-Version: 2025-11-25`, served statelessly with no initialize
first) and the 2026-07-28 per-request envelope.

Run:  uv run --with pytest pytest backend/tests/test_mcp.py -q
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from mcp_types import CLIENT_CAPABILITIES_META_KEY, CLIENT_INFO_META_KEY, PROTOCOL_VERSION_META_KEY

import api_keys
import auth
import db
import deadline
import main
from models import DayTotals, FoodRef, Macros, MacroTargets, Meal, ServingRef, StagedItem
from resolver import foods_repo
from routes import commit, diary, entries, foods, history, logs, weights
from routes.logs import AgentOutcome, LogCreateResponse, LogPollResponse, StagedItemOut
from services import quota
from test_auth import BASE, _keypair, _token

LEGACY, MODERN = "2025-11-25", "2026-07-28"
ERAS = pytest.mark.parametrize("era", [LEGACY, MODERN])

READ_TOOLS = {
    "whoami", "get_day", "get_history", "get_goals", "get_weights",
    "list_saved_meals", "search_my_foods", "search_foods", "get_food",
}
WRITE_TOOLS = {"log_food", "commit_log", "discard_log", "edit_entry", "delete_entry", "log_weight"}

ZERO = MacroTargets(kcal=0, protein_g=0, carb_g=0, fat_g=0)
TODAY = date(2026, 9, 23)


def _user(uid: str, scopes: tuple[str, ...], *, email: str | None = None, is_owner: bool = True) -> dict:
    return {
        "id": uid, "email": email or f"{uid}@example.test", "is_owner": is_owner,
        "via": "api_key", "scopes": frozenset(scopes), "key_id": "0" * 16,
    }


USERS = {
    "read-key": _user("reader", ("read",)),
    "write-key": _user("writer", ("read", "write")),
}


def _fake_authenticate(users: dict, delays: dict | None = None):
    """auth.authenticate's contract without a database: the bearer token names
    the user, who is bound as the request's tenant (auth._bind's two sets)."""

    async def authenticate(request):
        token = request.headers.get("authorization", "").partition(" ")[2]
        user = users.get(token)
        if user is None:
            raise HTTPException(401, "invalid API key")
        db.current_user_id.set(user["id"])
        auth.current_user.set(user)
        if delays:
            await asyncio.sleep(delays[token])
        return user

    return authenticate


def _rpc(token: str, method: str, params: dict | None = None, era: str = LEGACY):
    """(headers, body) for one JSON-RPC request, as a client of that era sends it."""
    params = dict(params or {})
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": era,
    }
    if era == MODERN:
        params["_meta"] = {
            PROTOCOL_VERSION_META_KEY: MODERN,
            CLIENT_CAPABILITIES_META_KEY: {},
            CLIENT_INFO_META_KEY: {"name": "test", "version": "0"},
        }
        headers["Mcp-Method"] = method
        if method == "tools/call":
            headers["Mcp-Name"] = params["name"]
    return headers, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


def _call(client: TestClient, token: str, tool: str, args: dict | None = None, era: str = LEGACY) -> dict:
    headers, body = _rpc(token, "tools/call", {"name": tool, "arguments": args or {}}, era)
    r = client.post("/api/mcp", headers=headers, json=body)
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _text(result: dict) -> str:
    [block] = result["content"]
    return block["text"]


async def _with_app(fn):
    """The app with its lifespan, on one event loop — for calls that must
    overlap, or that would hang if a stream were held open."""
    async with main.app.router.lifespan_context(main.app):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            return await fn(c)


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(auth, "authenticate", _fake_authenticate(USERS))
    with TestClient(main.app) as c:
        yield c


# ---------------------------------------------------------------------------
# 1. the transport: auth, statelessness, methods
# ---------------------------------------------------------------------------


def test_no_token_is_401_with_www_authenticate():
    """The real auth: no bearer token, or a malformed key (refused before any
    lookup), is a 401 JSON body with the Bearer challenge."""
    headers, body = _rpc("unused", "tools/list")
    del headers["Authorization"]
    with TestClient(main.app) as c:
        r = c.post("/api/mcp", headers=headers, json=body)
        assert (r.status_code, r.json()) == (401, {"detail": "missing bearer token"})
        assert r.headers["www-authenticate"] == "Bearer"

        r = c.post("/api/mcp", headers={**headers, "Authorization": "Bearer tally_live_nope"}, json=body)
        assert (r.status_code, r.json()) == (401, {"detail": "invalid API key"})
        assert r.headers["www-authenticate"] == "Bearer"


def test_initialize_is_stateless(client):
    headers, body = _rpc("read-key", "initialize", {
        "protocolVersion": LEGACY, "capabilities": {}, "clientInfo": {"name": "test", "version": "0"},
    })
    r = client.post("/api/mcp", headers=headers, json=body)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")  # one JSON answer, no stream
    assert "mcp-session-id" not in r.headers  # nothing to come back to
    result = r.json()["result"]
    assert result["serverInfo"]["name"] == "tally"
    assert "commit_log" in result["instructions"]


def test_get_and_delete_are_405(client):
    for method in ("GET", "DELETE"):
        r = client.request(method, "/api/mcp", headers={"Authorization": "Bearer write-key"})
        assert r.status_code == 405, method
        assert r.headers["allow"] == "POST"


def test_subscriptions_listen_is_refused(monkeypatch):
    """Its answer is a stream held open until the client leaves: refused on
    the modern path by its header, and the 2025 path has no such method."""
    monkeypatch.setattr(auth, "authenticate", _fake_authenticate(USERS))
    params = {"notifications": {"toolsListChanged": True}}

    async def go(c):
        out = {}
        for era in (LEGACY, MODERN):
            headers, body = _rpc("write-key", "subscriptions/listen", params, era)
            out[era] = await asyncio.wait_for(c.post("/api/mcp", headers=headers, json=body), 10)
        return out

    r = asyncio.run(_with_app(go))
    assert (r[MODERN].status_code, r[MODERN].json()) == (
        400, {"detail": "subscriptions/listen is not supported"},
    )
    assert r[LEGACY].json()["error"]["code"] == -32601  # method not found


# ---------------------------------------------------------------------------
# 2. scopes: the key's access picks the server
# ---------------------------------------------------------------------------


@ERAS
def test_tool_lists_follow_the_scopes(client, era):
    lists = {}
    for token in ("read-key", "write-key"):
        headers, body = _rpc(token, "tools/list", era=era)
        r = client.post("/api/mcp", headers=headers, json=body)
        assert r.status_code == 200, r.text
        lists[token] = {t["name"]: t for t in r.json()["result"]["tools"]}
    assert set(lists["read-key"]) == READ_TOOLS
    assert set(lists["write-key"]) == READ_TOOLS | WRITE_TOOLS
    for name in READ_TOOLS:
        assert lists["write-key"][name]["annotations"]["readOnlyHint"] is True, name
    assert lists["write-key"]["delete_entry"]["annotations"]["destructiveHint"] is True


@ERAS
def test_write_tool_with_a_read_only_key_is_unknown(client, monkeypatch, era):
    async def never(body):
        raise AssertionError("a read-only key must not reach the handler")

    monkeypatch.setattr(logs, "create_log_endpoint", never)
    result = _call(client, "read-key", "log_food", {"text": "two eggs"}, era)
    assert result["isError"] is True
    assert _text(result) == "Unknown tool: log_food"


def test_real_auth_picks_the_server(monkeypatch):
    """The real auth.authenticate: a read-only API key (its lookup faked) gets
    the read tools; a session JWT gets them all, as on REST."""
    monkeypatch.setenv("NEON_AUTH_BASE_URL", BASE)
    for name in ("ALLOWED_EMAIL", "APP_ORIGIN", "VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(auth, "_dotenv_loaded", True)
    monkeypatch.setattr(auth, "_last_failed_fetch", float("-inf"))
    auth._keys.clear()

    key_id, key = api_keys.generate()
    rows = {(key_id, api_keys.hash_key(key)): {
        "user_id": "user-k", "email": "demo@example.test", "email_verified": True, "scopes": ["read"],
    }}

    async def fake_fetchone(sql, params=()):
        assert sql == "SELECT * FROM api_key_auth(%s, %s)"
        return rows.get(params)

    priv, jwk = _keypair("k1")

    async def fake_jwks():
        return {"keys": [jwk]}

    monkeypatch.setattr(db, "fetchone", fake_fetchone)
    monkeypatch.setattr(auth, "_fetch_jwks", fake_jwks)
    try:
        with TestClient(main.app) as c:
            names = {}
            for who, token in (("key", key), ("session", _token(priv, "k1"))):
                headers, body = _rpc(token, "tools/list")
                r = c.post("/api/mcp", headers=headers, json=body)
                assert r.status_code == 200, r.text
                names[who] = {t["name"] for t in r.json()["result"]["tools"]}
    finally:
        auth._keys.clear()
    assert names == {"key": READ_TOOLS, "session": READ_TOOLS | WRITE_TOOLS}


# ---------------------------------------------------------------------------
# 3. errors a handler raises
# ---------------------------------------------------------------------------


@ERAS
def test_http_exception_is_a_readable_tool_error(client, monkeypatch, era):
    async def conflict(log_id):
        raise HTTPException(409, "log already committed")

    async def missing(food_id):
        raise HTTPException(404, "food not found")

    monkeypatch.setattr(logs, "discard_log", conflict)
    monkeypatch.setattr(foods, "get_food_detail", missing)

    result = _call(client, "write-key", "discard_log", {"log_id": str(uuid4())}, era)
    assert result["isError"] is True
    assert _text(result) == "Error executing tool discard_log: log already committed (HTTP 409)"

    result = _call(client, "read-key", "get_food", {"food_id": str(uuid4())}, era)
    assert _text(result) == "Error executing tool get_food: food not found (HTTP 404)"


def test_a_crash_stays_on_the_server(client, monkeypatch):
    """Anything but an HTTPException is a bug: the model sees only the tool's
    name, never the exception's text."""

    async def boom(date):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(diary, "get_diary", boom)
    result = _call(client, "read-key", "get_day")
    assert result["isError"] is True
    assert _text(result) == "Error executing tool get_day"


def test_quota_exceeded_is_the_friendly_message(client, monkeypatch):
    resets = datetime(2026, 9, 24, tzinfo=timezone.utc)

    async def over_quota(body):
        # the exact exception services/quota.py raises for a demo's second log
        raise quota._refusal({"resets_at": resets, "used": 1}, quota.DEMO_LOG_LIMIT,
                             "Demo accounts can log once a day")

    monkeypatch.setattr(logs, "create_log_endpoint", over_quota)
    result = _call(client, "write-key", "log_food", {"text": "a banana", "tz": "UTC"})
    assert result["isError"] is True
    assert _text(result) == (
        "Error executing tool log_food: "
        "Demo accounts can log once a day; resets at 2026-09-24T00:00:00+00:00"
    )


def test_bad_arguments_never_reach_the_handler(client, monkeypatch):
    async def never(**kwargs):
        raise AssertionError("invalid arguments must not reach the handler")

    monkeypatch.setattr(weights, "get_weights", never)
    monkeypatch.setattr(weights, "put_weight", never)
    monkeypatch.setattr(entries, "patch_entry", never)
    for tool, args in (
        ("get_weights", {"days": 0}),
        ("log_weight", {"weight": -80, "unit": "kg"}),
        ("log_weight", {"weight": 80, "unit": "stone"}),
        ("edit_entry", {"entry_id": str(uuid4()), "food_id": "banana"}),  # a name, not an id
    ):
        result = _call(client, "write-key", tool, args)
        assert result["isError"] is True, (tool, args)
        assert _text(result).startswith(f"Error executing tool {tool}: "), _text(result)


# ---------------------------------------------------------------------------
# 4. the caller inside a tool
# ---------------------------------------------------------------------------


@ERAS
def test_each_call_runs_as_its_own_caller(monkeypatch, era):
    """Five demo users at once, bound in one order and served in the reverse:
    whoami reads auth.current_user, and its quota query sees
    db.current_user_id — both must be the caller's own."""
    users = {f"k{i}": _user(f"user-{i}", ("read",), is_owner=False) for i in range(5)}
    delays = {f"k{i}": 0.02 * (5 - i) for i in range(5)}
    seen: list[str] = []

    async def fake_fetchone(sql, params=()):
        assert params == ("log",)  # quota.demo_status's count
        tenant = db.current_user_id.get()
        seen.append(tenant)
        await asyncio.sleep(0.01)
        return {"used": int(tenant.rsplit("-", 1)[1]), "resets_at": datetime(2026, 9, 24, tzinfo=timezone.utc)}

    monkeypatch.setattr(auth, "authenticate", _fake_authenticate(users, delays))
    monkeypatch.setattr(db, "fetchone", fake_fetchone)

    async def whoami(c, token):
        headers, body = _rpc(token, "tools/call", {"name": "whoami", "arguments": {}}, era)
        r = await c.post("/api/mcp", headers=headers, json=body)
        assert r.status_code == 200, r.text
        return r.json()["result"]["structuredContent"]

    async def go(c):
        return await asyncio.gather(*(whoami(c, token) for token in users))

    results = asyncio.run(_with_app(go))
    for i, me in enumerate(results):
        assert me["email"] == f"user-{i}@example.test"
        assert me["is_owner"] is False
        assert me["demo"]["used_today"] == i  # the quota row of this caller's tenant
    assert sorted(seen) == [f"user-{i}" for i in range(5)]


# ---------------------------------------------------------------------------
# 5. how the tools call their handlers
# ---------------------------------------------------------------------------


def test_read_tools_pass_every_argument(client, monkeypatch):
    """A handler's Query() default is a FieldInfo outside FastAPI, so every
    tool names every argument — defaults included."""
    got: dict = {}

    def record(name, response):
        async def handler(**kwargs):
            got[name] = kwargs
            return response
        return handler

    monkeypatch.setattr(weights, "get_weights", record("weights", weights.WeightsResponse(latest=None, entries=[])))
    monkeypatch.setattr(history, "get_history", record("history", history.HistoryResponse(
        range="w", timezone="UTC", start=TODAY, end=TODAY, targets=ZERO, streak=0,
        baseline_days_per_week=0, days=[], weeks=[],
    )))
    monkeypatch.setattr(diary, "get_diary", record("diary", diary.DiaryResponse(
        date=TODAY, meals={}, totals=DayTotals(kcal=0, protein_g=0, carb_g=0, fat_g=0, targets=ZERO),
    )))
    monkeypatch.setattr(foods, "search_foods", record("search", foods.FoodSearchResponse(
        query="egg", local=[], generic=[], branded=[],
    )))
    monkeypatch.setattr(foods_repo, "past_foods", record("past", [{
        "food_id": "f1", "display_name": "Greek yogurt", "brand": None, "serving_id": "s1",
        "serving": "1 cup", "qty": Decimal("1.5"), "grams": None, "meal": "breakfast", "day": TODAY,
    }]))

    async def tz_name():
        return "America/Los_Angeles"

    monkeypatch.setattr(diary, "tz_name", tz_name)

    results = {}
    for tool, args in (
        ("get_weights", {}), ("get_history", {}), ("get_day", {}), ("search_foods", {"query": "egg"}),
        ("search_my_foods", {}),
    ):
        results[tool] = _call(client, "read-key", tool, args)
        assert results[tool]["isError"] is False, (tool, _text(results[tool]))
    assert got == {
        "weights": {"days": 90},
        "history": {"rng": "w"},
        "diary": {"date": None},
        "search": {"q": "egg", "source": "local"},  # USDA only when asked
        "past": {"query": None, "limit": 20, "tz": "America/Los_Angeles"},
    }
    [yogurt] = results["search_my_foods"]["structuredContent"]["result"]
    assert (yogurt["food"], yogurt["qty"], yogurt["day"]) == ("Greek yogurt", 1.5, "2026-09-23")

    _call(client, "read-key", "search_foods", {"query": "egg", "include_usda": True})
    _call(client, "read-key", "get_day", {"date": "2026-09-01"})
    assert got["search"] == {"q": "egg", "source": "all"}
    assert got["diary"] == {"date": date(2026, 9, 1)}


def test_log_food_stages_and_reports_rows(client, monkeypatch):
    sent: dict = {}

    async def tz_name():
        return "America/Los_Angeles"

    async def create(body):
        sent["body"], sent["clock"] = body, deadline.left()
        return LogCreateResponse(
            log_id=body.id, meal=Meal.breakfast,
            items=[
                StagedItem(
                    item_index=0, phrase="eggs", status="resolved", meal=Meal.breakfast, qty=2,
                    food=FoodRef(id="f1", name="Egg, whole, cooked"),
                    serving=ServingRef(id="s1", description="1 large"),
                    macros=Macros(kcal=156, protein_g=12.6, carb_g=1.1, fat_g=10.6),
                ),
                StagedItem(item_index=1, phrase="mystery bar", status="searching", meal=Meal.breakfast),
            ],
            agent=AgentOutcome(reply="Staged two eggs; still looking up the bar."),
        )

    monkeypatch.setattr(diary, "tz_name", tz_name)
    monkeypatch.setattr(logs, "create_log_endpoint", create)
    result = _call(client, "write-key", "log_food", {"text": "two eggs and a mystery bar"})
    assert result["isError"] is False, _text(result)

    body = sent["body"]
    assert body.text == "two eggs and a mystery bar"
    assert body.tz == "America/Los_Angeles"  # the account's, when the caller names none
    assert body.captured_at.tzinfo is not None
    assert sent["clock"] is not None and sent["clock"] > 100  # the budget started first

    out = result["structuredContent"]
    assert out["log_id"] == str(body.id)
    assert out["meal"] == "breakfast"
    assert out["reply"] == "Staged two eggs; still looking up the bar."
    eggs, bar = out["items"]
    assert (eggs["index"], eggs["status"], eggs["food"], eggs["qty"], eggs["serving"]) == (
        0, "resolved", "Egg, whole, cooked", 2, "1 large",
    )
    assert eggs["macros"]["kcal"] == 156
    assert (bar["index"], bar["status"], bar["food"]) == (1, "searching", None)


def test_commit_log_sends_every_kept_row(client, monkeypatch):
    log_id = uuid4()
    rows = [
        StagedItemOut(
            item_index=i, phrase=phrase, status=status, meal=Meal.lunch,
            food=FoodRef(id=f"f{i}", name=phrase) if status == "resolved" else None,
            serving=ServingRef(id="s", description="1 serving") if status == "resolved" else None,
            qty=1 if status == "resolved" else None,
            stale=True if status == "searching" else None,
        )
        for i, (phrase, status) in enumerate(
            [("rice", "resolved"), ("soda", "resolved"), ("mystery sauce", "searching")]
        )
    ]
    committed: list = []

    async def get_log(log_id):
        return LogPollResponse(log_id=log_id, meal=Meal.lunch, items=rows)

    async def commit_log(log_id, body):
        committed.append((log_id, body))
        return commit.CommitResponse(
            kcal=300, protein_g=6, carb_g=60, fat_g=1, targets=ZERO, totals_day=TODAY,
        )

    monkeypatch.setattr(logs, "get_log", get_log)
    monkeypatch.setattr(commit, "commit_log", commit_log)

    result = _call(client, "write-key", "commit_log",
                   {"log_id": str(log_id), "drop_items": [1], "save_as_meal": "usual lunch"})
    assert result["isError"] is False, _text(result)
    [(sent_id, body)] = committed
    assert sent_id == log_id
    # every row to keep goes back (a missing one would be marked removed), as
    # plain StagedItems — the poll-only `stale` flag stays behind
    assert [i.item_index for i in body.items] == [0, 2]
    assert all(type(i) is StagedItem for i in body.items)
    assert body.save_intent.name == "usual lunch"
    out = result["structuredContent"]
    assert out["kcal"] == 300 and out["totals_day"] == "2026-09-23"
    assert out["not_saved"] == [{"index": 2, "status": "searching", "phrase": "mystery sauce"}]

    # a row index the log does not have is refused before anything is written
    result = _call(client, "write-key", "commit_log", {"log_id": str(log_id), "drop_items": [7]})
    assert result["isError"] is True
    assert _text(result) == "Error executing tool commit_log: this log has no staged row [7]"
    assert len(committed) == 1


def test_edit_entry_sends_the_canonical_food_id(client, monkeypatch):
    """patch_entry compares food ids as strings: an uppercase copy of the
    entry's own food must arrive lowercase, or it would read as a food change."""
    entry_id, food_id = uuid4(), uuid4()
    sent: list = []

    async def patch_entry(entry_id, body):
        sent.append((entry_id, body))
        return entries.EntryPatchResponse(
            entry=diary.DiaryEntry(
                id=entry_id, meal=Meal.lunch, food=FoodRef(id=body.food_id, name="Rice"),
                serving=ServingRef(id="s1", description="1 cup"), qty=1,
                macros=Macros(kcal=200, protein_g=4, carb_g=44, fat_g=0.4),
                logged_at=datetime(2026, 9, 23, 12, tzinfo=timezone.utc),
            ),
            totals=DayTotals(kcal=200, protein_g=4, carb_g=44, fat_g=0.4, targets=ZERO),
        )

    monkeypatch.setattr(entries, "patch_entry", patch_entry)
    result = _call(client, "write-key", "edit_entry",
                   {"entry_id": str(entry_id), "food_id": str(food_id).upper()})
    assert result["isError"] is False, _text(result)
    [(sent_id, body)] = sent
    assert sent_id == entry_id
    assert (body.qty, body.serving_id, body.food_id) == (None, None, str(food_id))
