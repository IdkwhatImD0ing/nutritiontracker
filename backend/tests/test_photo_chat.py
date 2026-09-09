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

import pytest
from fastapi import HTTPException

from agents.tool_context import ToolContext

import services.agent as agent_svc

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
