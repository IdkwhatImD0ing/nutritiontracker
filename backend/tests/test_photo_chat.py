"""The photo lane as a chat turn — DESIGN.md §3.4b, §6.4 (2026-09-08).

What this file pins, none of it needing a database:

1. The model a run actually uses. A conversation with a photo in it runs on
   gpt-5.6-sol at reasoning effort "low"; every other one stays on
   gpt-5.6-terra at the model's default. Nothing else in the repo sets
   reasoning on an agent, so the terra path must come out byte-identical to
   what it was — including `verbosity` staying unset, which the SDK's own
   per-model defaults would otherwise fill in.
2. `adjust_photo` is in the toolset only for a photo conversation, and its
   code-side guards refuse before any write.
3. `POST /api/agent`'s multipart form takes exactly one of `audio` or `image`.

Run:  uv run --project backend --with pytest pytest backend/tests/test_photo_chat.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
# this directory too: section 5 borrows test_agent's fake Runner
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import pytest
from types import SimpleNamespace
from fastapi import HTTPException

from agents.tool_context import ToolContext

import services.agent as agent_svc
from models import Meal, StagedItem

CAPTURED = datetime(2026, 9, 8, 19, 15, tzinfo=timezone.utc)
TZ = "America/Los_Angeles"


def _deps(**kw) -> agent_svc.AgentDeps:
    async def _never(*_a, **_k):  # the tools are never called in these tests
        raise AssertionError("no tool should run here")

    return agent_svc.AgentDeps(
        stage_food=_never, stage_estimate=_never, record_weight=_never,
        now_local=CAPTURED, unit_pref="lb", **kw
    )


def _tools(agent) -> set[str]:
    return {getattr(t, "name", None) or type(t).__name__ for t in agent.tools}


# ---------------------------------------------------------------------------
# 1. which model, at which effort
# ---------------------------------------------------------------------------


def test_a_photo_conversation_runs_on_sol_at_low_effort():
    agent = agent_svc.build_agent(_deps(photo_chat=True, adjust_photo=_deps().stage_food), {})
    assert agent.model == "gpt-5.6-sol" == agent_svc.PHOTO_MODEL
    assert agent.model_settings.reasoning is not None
    # Bill's "light mode": the API has no such value — none/low/medium/high/
    # xhigh/max — and "low" is what the Codex/ChatGPT UI calls light
    assert agent.model_settings.reasoning.effort == "low" == agent_svc.PHOTO_EFFORT
    # the web lane's setting is not lost to the reasoning one
    assert agent.model_settings.response_include == ["web_search_call.action.sources"]


def test_an_ordinary_chat_is_unchanged_by_the_photo_work():
    """The regression guard on the lane that did NOT change. `reasoning` must
    stay None: the SDK only substitutes its per-model defaults (which set both
    an effort and verbosity="low") when model_settings equals the default
    object, and this lane has always passed its own."""
    agent = agent_svc.build_agent(_deps(), {})
    assert agent.model == "gpt-5.6-terra" == agent_svc.MODEL
    assert agent.model_settings.reasoning is None
    assert agent.model_settings.verbosity is None
    assert agent.model_settings.response_include == ["web_search_call.action.sources"]


def test_pinned_ids_are_full_ids_never_a_family_alias():
    # §6: the bare alias routes to the flagship at flagship prices
    for model in (agent_svc.MODEL, agent_svc.PHOTO_MODEL):
        assert model not in ("gpt-5.6", "gpt-6")
        assert model.count("-") >= 2


# ---------------------------------------------------------------------------
# 2. the toolset and the instructions
# ---------------------------------------------------------------------------


def test_adjust_photo_exists_only_where_there_is_a_plate():
    plain = _tools(agent_svc.build_agent(_deps(), {}))
    assert "adjust_photo" not in plain
    assert {"log_food", "log_estimate", "discard_log"} <= plain

    with_plate = _tools(
        agent_svc.build_agent(_deps(photo_chat=True, adjust_photo=_deps().stage_food), {})
    )
    assert "adjust_photo" in with_plate


def test_the_photo_block_is_appended_not_interpolated():
    """The base instructions must stay byte-stable per configuration — that is
    what the prompt cache keys on — so the photo block is appended the way
    _NO_WEB_LANE is, never spliced into the body."""
    plain = agent_svc.build_agent(_deps(), {}).instructions
    photo = agent_svc.build_agent(
        _deps(photo_chat=True, adjust_photo=_deps().stage_food), {}
    ).instructions
    assert photo.startswith(plain)
    assert "adjust_photo" in photo and "adjust_photo" not in plain
    # it must steer away from the destructive path, which is the whole point
    assert "discard_log" in agent_svc._PHOTO_LANE


def test_discard_log_no_longer_claims_the_amount_correction():
    """"make that two eggs" was discard_log's own example and is now
    adjust_photo's canonical case; leaving it in both is a direct conflict."""
    assert "make that two\neggs" not in agent_svc.INSTRUCTIONS
    assert "make that two eggs" not in agent_svc.INSTRUCTIONS


def test_adjust_photo_refuses_before_it_writes():
    """The tool's code-side guards, in the log_estimate style: the callback is
    never reached, so nothing can be half-written."""
    calls: list = []

    async def record(changes):
        calls.append(changes)
        return {"changed": [], "items": []}

    state: dict = {}
    agent = agent_svc.build_agent(_deps(photo_chat=True, adjust_photo=record), state)
    tool = next(t for t in agent.tools if getattr(t, "name", "") == "adjust_photo")

    async def call(payload):
        args = json.dumps(payload)
        ctx = ToolContext(
            context=None, tool_name="adjust_photo", tool_call_id="call_1", tool_arguments=args
        )
        return json.loads(await tool.on_invoke_tool(ctx, args))

    assert asyncio.run(call({"changes": []}))["error"] == "nothing_to_change"
    # a count of pieces with no total weight would be sized as pieces × the
    # food's mass default — the same refusal log_estimate makes
    counted = {"op": "amount", "item": "eggs", "qty": 3,
               "unit": None, "grams": None, "note": "three"}
    assert asyncio.run(call({"changes": [counted]}))["error"] == "missing_grams"
    zero = {**counted, "qty": 0, "grams": 150}
    assert asyncio.run(call({"changes": [zero]}))["error"] == "invalid_qty"
    # a removal needs no amount at all
    assert "error" not in asyncio.run(
        call({"changes": [{"op": "remove", "item": "sauce", "qty": None,
                           "unit": None, "grams": None, "note": "not there"}]})
    )
    assert len(calls) == 1 and state["photo_calls"] == 1


# ---------------------------------------------------------------------------
# 3. the route takes exactly one upload
# ---------------------------------------------------------------------------


def test_agent_multipart_takes_exactly_one_of_audio_or_image():
    from io import BytesIO

    from starlette.datastructures import Headers, UploadFile

    from routes import agent as agent_routes

    def upload(name, mime):
        return UploadFile(
            file=BytesIO(b"\x89PNG\r\n\x1a\n" + b"\0" * 32), filename=name,
            headers=Headers({"content-type": mime}),
        )

    async def call(**kw):
        return await agent_routes.agent_voice(
            log_id=uuid4(), captured_at=CAPTURED, tz=TZ, **kw
        )

    for kwargs in (
        {},  # neither
        {"audio": upload("clip.webm", "audio/webm"), "image": upload("p.png", "image/png")},
    ):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(call(**kwargs))
        assert exc.value.status_code == 422
        assert "exactly one" in exc.value.detail


def test_the_multipart_schema_documents_the_photo_part():
    """typegen reads this; `audio` stops being required or the generated
    client would demand it on a photo turn."""
    from routes import agent as agent_routes

    form = agent_routes._AGENT_FORM
    assert "audio" not in form["required"]
    assert set(form["required"]) == {"id", "captured_at", "tz"}
    assert form["properties"]["image"]["format"] == "binary"


# ---------------------------------------------------------------------------
# 4. the photo lane's web lane (§3.4b, 2026-09-09)
# ---------------------------------------------------------------------------


def _sent_kwargs(monkeypatch, **analyze_kw) -> dict:
    """One analyze() call against a fake client; returns the request kwargs."""
    import services.vision as vision

    sent: dict = {}

    class _Responses:
        async def parse(self, **kwargs):
            sent.update(kwargs)
            return SimpleNamespace(
                output_parsed=vision.VisionResult(items=[], meal_hint=None), usage=None
            )

    monkeypatch.setattr(vision, "_client", SimpleNamespace(responses=_Responses()))
    try:
        asyncio.run(vision.analyze(b"\xff\xd8\xff\xe0" + b"\0" * 8, "image/jpeg", **analyze_kw))
    finally:
        vision._client = None
    return sent


def test_the_photo_web_lane_is_off_by_default(monkeypatch):
    """A demo account, and every call that does not ask for it, gets the lane
    exactly as it was: no tools, and the instructions unchanged so the prompt
    cache still hits."""
    import services.vision as vision

    sent = _sent_kwargs(monkeypatch)
    assert sent["tools"] == []
    assert sent["input"][0]["content"][0]["text"] == vision.INSTRUCTIONS
    assert "web_search" not in sent["input"][0]["content"][0]["text"]


def test_the_web_lane_offers_hosted_tools_only(monkeypatch):
    """Nothing first-party fetches anything: web_search is OpenAI's and the
    scraper is Firecrawl's own remote MCP server, which OpenAI calls. The key
    rides `authorization`, never the URL (Firecrawl's own instruction), and the
    tool surface is narrowed — the server offers two dozen tools, of which this
    lane can use two."""
    import services.vision as vision

    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    sent = _sent_kwargs(monkeypatch, web=True)
    kinds = [t["type"] for t in sent["tools"]]
    assert kinds == ["web_search", "mcp"]
    mcp = sent["tools"][1]
    assert mcp["server_url"] == "https://mcp.firecrawl.dev/v2/mcp"
    assert "fc-test-key" not in mcp["server_url"]
    assert mcp["authorization"] == "fc-test-key"
    assert mcp["allowed_tools"] == ["firecrawl_scrape", "firecrawl_search"]
    assert mcp["require_approval"] == "never"


def test_the_web_lane_degrades_to_search_without_a_key(monkeypatch):
    """No Firecrawl key configured is not an error — the lane keeps hosted
    search and simply cannot read a page it is handed."""
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    sent = _sent_kwargs(monkeypatch, web=True)
    assert [t["type"] for t in sent["tools"]] == ["web_search"]


def test_the_person_context_rides_the_user_message_not_the_cached_prefix(monkeypatch):
    """The profile notes differ per account and change whenever they are
    edited, so putting them in the developer message would break the prompt
    cache for every photo. INSTRUCTIONS (+ the fixed web block) stays the
    byte-stable prefix; the notes go in the user turn beside the image."""
    import services.vision as vision

    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    sent = _sent_kwargs(monkeypatch, context="I eat at the Pinterest SF cafeteria.", web=True)
    developer = sent["input"][0]["content"][0]["text"]
    user_text = sent["input"][1]["content"][0]["text"]
    assert developer == vision.INSTRUCTIONS + vision._WEB_LANE
    assert "Pinterest" not in developer
    assert "Pinterest SF cafeteria" in user_text
    # the guard that keeps a menu from becoming a shopping list
    assert "NEVER add an item because a menu lists it" in vision._WEB_LANE
    # …and, since Bill widened §3.4a on 2026-09-09 to let a menu's own numbers
    # through, the guards that replaced the old absolute ban: copied not
    # computed, and never loose in the free text where nothing checks them
    assert "TRANSCRIBE, never estimate" in vision._WEB_LANE
    assert "Still NEVER put a number in `phrase` or `note`" in vision._WEB_LANE


def test_a_menu_match_only_lands_when_the_page_gave_all_four_macros(monkeypatch):
    """CODE decides whether a transcribed block is usable, not the prompt.
    Measured on the real cafeteria 2026-09-09: its rendered menu publishes
    calories and nothing else (protein/carbs/fat live in a script blob no text
    scraper reads), so this is the common case, not an edge one. A row with
    calories alone must stay exactly as the cascade left it — a foods row with
    partial macros cannot commit anyway (the core-macros guard), so accepting
    one would only put a confident wrong number in front of the user."""
    import services.vision as vision
    from routes import photo

    async def run(menu):
        item = StagedItem(
            item_index=0, phrase="lamb", status="searching",
            meal=Meal("lunch"), confidence="medium",
        )
        v = vision.VisionItem(
            phrase="lamb", qty=None, unit=None, est_grams=120,
            confidence="medium", note="sliced lamb", menu=menu,
        )
        return await photo._stage_menu_food(item, v)

    full = vision.MenuMatch(
        dish="herb roasted leg of lamb",
        source_url="https://pinterest-sf.cafebonappetit.com/cafe/2026-09-08/",
        serving="1 serving", kcal=320, protein_g=28, carb_g=2, fat_g=21,
    )
    # a block the page could not fill is refused before any write
    for bad in (
        full.model_copy(update={"dish": "   "}),
        full.model_copy(update={"source_url": "not-a-url"}),
        full.model_copy(update={"protein_g": -1}),
    ):
        assert asyncio.run(run(bad)).food is None

    assert asyncio.run(run(None)).food is None


# ---------------------------------------------------------------------------
# 5. a chat turn that carries an image (§6.4, 2026-09-09)
# ---------------------------------------------------------------------------

_PIXEL = "data:image/png;base64,iVBORw0KGgo="


def test_words_alone_still_reach_the_model_as_a_plain_string():
    """The image path must not change the shape of every other run: no deps
    image, no list — the SDK sees exactly the string it always saw."""
    from test_agent import _fake_runner  # the fake that drives real closures

    runner = _fake_runner([], "Noted.")
    agent_svc._load_runner()
    real, agent_svc.Runner = agent_svc.Runner, runner
    try:
        asyncio.run(agent_svc.run_agent("two eggs", _deps()))
    finally:
        agent_svc.Runner = real
    assert isinstance(runner.input, str)
    assert "two eggs" in runner.input


def test_an_attached_image_rides_beside_the_words_never_instead_of_them():
    """§6.4: the picture is added to the turn, so everything the agent was
    told still reaches it — the same prompt, with the image next to it."""
    from test_agent import _fake_runner

    runner = _fake_runner([], "That looks like the small size.")
    deps = _deps(image=_PIXEL)
    agent_svc._load_runner()
    real, agent_svc.Runner = agent_svc.Runner, runner
    try:
        asyncio.run(agent_svc.run_agent("is this the small one?", deps))
    finally:
        agent_svc.Runner = real

    sent = runner.input
    assert isinstance(sent, list) and len(sent) == 1
    content = sent[0]["content"]
    assert sent[0]["role"] == "user"
    kinds = [part["type"] for part in content]
    assert kinds == ["input_text", "input_image"]
    # the same prompt a text turn would have got, unabridged
    assert content[0]["text"] == agent_svc.agent_input("is this the small one?", deps)
    assert content[1]["image_url"] == _PIXEL


def test_a_photo_with_words_runs_the_agent_and_a_bare_photo_does_not():
    """The dispatch §3.4b rests on: words make it a conversation turn the
    model can see; a plate on its own stays the deterministic vision parse,
    which spends nothing on a chat model."""
    from io import BytesIO

    from starlette.datastructures import Headers, UploadFile

    from routes import agent as agent_routes

    took: list[str] = []

    async def to_agent(*_a, **_k):
        took.append("agent")

    async def to_parse(*_a, **_k):
        took.append("parse")

    def upload():
        return UploadFile(
            file=BytesIO(b"\xff\xd8\xff\xe0" + b"\0" * 32), filename="plate.jpg",
            headers=Headers({"content-type": "image/jpeg"}),
        )

    real_agent = agent_routes._agent_image_turn
    real_parse = agent_routes._agent_photo
    agent_routes._agent_image_turn = to_agent
    agent_routes._agent_photo = to_parse
    try:
        for said in ("is this the small size?", "   ", None):
            asyncio.run(
                agent_routes.agent_voice(
                    log_id=uuid4(), captured_at=CAPTURED, tz=TZ,
                    image=upload(), text=said,
                )
            )
    finally:
        agent_routes._agent_image_turn = real_agent
        agent_routes._agent_photo = real_parse

    # whitespace is not a question — it must not buy a model run
    assert took == ["agent", "parse", "parse"]


def test_the_multipart_schema_documents_the_said_part():
    """typegen reads this; without it the generated client cannot send the
    words that turn a photo into a chat turn."""
    from routes import agent as agent_routes

    said = agent_routes._AGENT_FORM["properties"]["text"]
    assert said["type"] == "string"
    assert "text" not in agent_routes._AGENT_FORM["required"]


def test_a_chat_image_is_held_to_the_photo_lane_s_own_contract():
    """One photo contract: the chat turn validates through the photo lane's
    reader, so an unsupported upload is refused with the same sentence — and
    before the claim, so no log id is spent on it."""
    from io import BytesIO

    from starlette.datastructures import Headers, UploadFile

    from routes import agent as agent_routes

    # declared a JPEG, is not one: the magic-byte sniff runs here too
    lying = UploadFile(
        file=BytesIO(b"not an image at all, whatever it says"), filename="plate.jpg",
        headers=Headers({"content-type": "image/jpeg"}),
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            agent_routes._agent_image_turn(
                lying, "what is this?", uuid4(), CAPTURED, TZ, None, None
            )
        )
    assert exc.value.status_code == 415
    assert exc.value.detail == "not a JPEG, PNG or WebP image"


# ---------------------------------------------------------------------------
# 6. review fixes, 2026-09-09 — the three ways an image turn could go quiet
# ---------------------------------------------------------------------------


def test_an_attached_image_is_never_treated_as_a_memorized_phrase():
    """§3.0's short-circuit runs before the agent lane, so a photo sent with
    "my usual breakfast" would have staged the saved meal and dropped the
    picture — the question unanswered and a plate nobody photographed."""
    from routes import logs as logs_route

    asked: list[str] = []

    async def short_circuit_all(text, meal):
        asked.append(text)
        raise AssertionError("the short circuit must not see an image turn")

    async def agent_lane(body, meal, emit=None, image=None):
        return image

    async def infer_meal(*_a, **_k):
        return Meal("lunch")

    body = SimpleNamespace(
        id=uuid4(), text="my usual breakfast", captured_at=CAPTURED, tz=TZ, agent=True
    )
    real = (
        logs_route.cascade.short_circuit_all,
        logs_route._run_agent_lane,
        logs_route.infer_meal,
    )
    logs_route.cascade.short_circuit_all = short_circuit_all
    logs_route._run_agent_lane = agent_lane
    logs_route.infer_meal = infer_meal
    try:
        got = asyncio.run(logs_route._run_pipeline(body, None, "data:image/jpeg;base64,AA=="))
    finally:
        (
            logs_route.cascade.short_circuit_all,
            logs_route._run_agent_lane,
            logs_route.infer_meal,
        ) = real

    assert asked == []  # not consulted at all
    assert got == "data:image/jpeg;base64,AA=="  # the image reached the agent

    # and without one it is consulted exactly as before
    async def none(text, meal):
        asked.append(text)
        return None

    logs_route.cascade.short_circuit_all = none
    logs_route._run_agent_lane = agent_lane
    logs_route.infer_meal = infer_meal
    try:
        asyncio.run(logs_route._run_pipeline(body, None, None))
    finally:
        (
            logs_route.cascade.short_circuit_all,
            logs_route._run_agent_lane,
            logs_route.infer_meal,
        ) = real
    assert asked == ["my usual breakfast"]


def test_an_image_turn_claims_a_photo_log_so_its_rows_never_learn():
    """§3.5: what this turn stages was identified from pixels. Claimed as
    `text` it would pass commit's `_NO_LEARN_MODES` gate, and a portion the
    model read off an image would become the next spoken log's default."""
    from io import BytesIO

    from starlette.datastructures import Headers, UploadFile

    from routes import agent as agent_routes

    seen: dict = {}

    async def claim(body, mode="text"):
        seen["mode"] = mode
        seen["text"] = body.text
        return SimpleNamespace(items=[])  # a replay: stops before the stream

    def stream(_gen):
        return "streamed"

    real = (agent_routes.claim_text_log, agent_routes._stream, agent_routes._replay)
    agent_routes.claim_text_log = claim
    agent_routes._stream = stream
    agent_routes._replay = lambda *a, **k: None
    try:
        asyncio.run(
            agent_routes._agent_image_turn(
                UploadFile(
                    file=BytesIO(b"\xff\xd8\xff\xe0" + b"\0" * 32), filename="p.jpg",
                    headers=Headers({"content-type": "image/jpeg"}),
                ),
                "is this the small size?", uuid4(), CAPTURED, TZ, None, None,
            )
        )
    finally:
        agent_routes.claim_text_log, agent_routes._stream, agent_routes._replay = real

    assert seen["mode"] == "photo"
    assert seen["text"] == "is this the small size?"


def test_words_sent_with_audio_are_refused_rather_than_dropped():
    """`text` means "said about a picture". Beside audio the transcript is the
    utterance, so the field has nowhere to go — and silently ignoring it would
    hide the caller's mistake behind a turn that looks like it worked."""
    from io import BytesIO

    from starlette.datastructures import Headers, UploadFile

    from routes import agent as agent_routes

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            agent_routes.agent_voice(
                log_id=uuid4(), captured_at=CAPTURED, tz=TZ,
                audio=UploadFile(
                    file=BytesIO(b"\x1aE\xdf\xa3" + b"\0" * 32), filename="clip.webm",
                    headers=Headers({"content-type": "audio/webm"}),
                ),
                text="and a banana",
            )
        )
    assert exc.value.status_code == 422
    assert "`text` belongs with `image`" in exc.value.detail
