"""Offline tests for backend/tracing.py — the FireTrace helper (DESIGN.md §10).

No network: the scheduler is replaced with a recorder. Checked here: the
payload matches the ingestion contract (id formats, timestamps, only the
allowed keys, never an environment field), runs nest into one tree —
including the openai-agents SDK's spans and a run opened inside a tool —
redaction, the send-only-when-a-model-ran rule, the size and span caps,
retry classification, the score queue, and the middleware's drain.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Another test module may have left a stub in sys.modules — this file tests
# the real module.
sys.modules.pop("tracing", None)
import tracing  # noqa: E402

# the ingestion contract (https://tracing.art3m1s.me/api/v1/openapi.json):
# additionalProperties is false at every level
TRACE_KEYS = {
    "id", "name", "status", "startedAt", "endedAt", "provider", "model", "sessionId",
    "userId", "tags", "input", "output", "metadata", "usage", "costUsd", "spans",
}
SPAN_KEYS = {
    "id", "parentSpanId", "name", "kind", "status", "startedAt", "endedAt", "provider",
    "model", "input", "output", "attributes", "events", "usage", "costUsd",
}
HEX32 = re.compile(r"^[0-9a-f]{32}$")
HEX16 = re.compile(r"^[0-9a-f]{16}$")
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


@pytest.fixture
def sent(monkeypatch):
    """Every send the module schedules, as (what, url, payload dict)."""
    out: list[tuple[str, str, dict]] = []

    def fake_schedule(what, url, body):
        out.append((what, url, json.loads(body)))
        return None

    monkeypatch.setattr(tracing, "_schedule", fake_schedule)
    monkeypatch.setenv(tracing.KEY_ENV, "ft_live_test_not_a_real_key")
    return out


def test_root_and_nested_runs_form_one_trace(sent):
    async def flow():
        async with tracing.run(
            "log", input="two eggs", session_id="log-1", user_id="u1", tags=["lane:text"]
        ) as root:
            async with tracing.run(
                "decompose", kind="llm", provider="openai", model="gpt-5.6-luna", input="two eggs"
            ) as d:
                d.set(
                    output={"items": ["egg"]},
                    usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
                )
            async with tracing.run("cascade", kind="retriever", input=["egg"]) as c:
                c.set(output=[{"phrase": "egg", "status": "resolved"}])
            root.set(output={"items": 1})
        return root

    root = asyncio.run(flow())
    assert len(sent) == 1
    _, url, payload = sent[0]
    assert url == tracing.INGEST_URL
    assert set(payload) == {"schemaVersion", "trace"} and payload["schemaVersion"] == 1
    t = payload["trace"]
    assert set(t) <= TRACE_KEYS and "environment" not in t
    assert HEX32.match(t["id"]) and t["id"] == root.trace_id
    assert ISO.match(t["startedAt"]) and ISO.match(t["endedAt"]) and t["endedAt"] >= t["startedAt"]
    assert t["sessionId"] == "log-1" and t["userId"] == "u1"
    assert set(t["tags"]) == {"lane:text", "owner"}  # conftest binds the owner tenant
    assert t["provider"] == "openai" and t["model"] == "gpt-5.6-luna"
    assert t["usage"] == {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}
    assert t["input"] == "two eggs" and t["output"] == {"items": 1} and t["status"] == "ok"
    spans = t["spans"]
    assert [s["name"] for s in spans] == ["log", "decompose", "cascade"]
    root_s, dec, cas = spans
    assert root_s["parentSpanId"] is None
    assert dec["parentSpanId"] == root_s["id"] == cas["parentSpanId"]
    ids = {s["id"] for s in spans}
    assert len(ids) == 3 and all(HEX16.match(i) for i in ids)
    for s in spans:
        assert set(s) <= SPAN_KEYS and s["status"] == "ok"
        assert ISO.match(s["startedAt"]) and ISO.match(s["endedAt"])
    assert dec["kind"] == "llm" and dec["usage"]["totalTokens"] == 15
    assert cas["kind"] == "retriever" and cas["output"][0]["status"] == "resolved"
    assert tracing.current_trace_id() is None  # the context is reset after the run


def test_current_trace_id_inside_a_run(sent):
    async def flow():
        async with tracing.run("log") as root:
            assert tracing.current_trace_id() == root.trace_id
            async with tracing.run("inner"):
                assert tracing.current_trace_id() == root.trace_id

    asyncio.run(flow())


def test_no_key_means_no_trace_id_and_no_payload(monkeypatch):
    monkeypatch.delenv(tracing.KEY_ENV, raising=False)
    monkeypatch.setattr(tracing, "_env_loaded", True)  # don't fall back to .env
    monkeypatch.setattr(tracing, "_payload", lambda trace: (_ for _ in ()).throw(AssertionError("built")))

    async def flow():
        async with tracing.run("log", kind="llm", model="m") as root:
            assert tracing.current_trace_id() is None  # nothing will point at a trace that never was
        assert root.sent is None

    asyncio.run(flow())


def test_nothing_sent_when_no_model_ran(sent):
    async def flow():
        async with tracing.run("log", input="usual breakfast") as root:  # §3.0 short-circuit
            async with tracing.run("cascade", kind="retriever"):
                pass
            root.set(output={"short_circuit": True})
        async with tracing.run("resolve.answer"):  # an id-bearing answer: no model
            pass

    asyncio.run(flow())
    assert sent == []


def test_error_in_the_run_marks_the_trace_and_reraises(sent):
    class Boom(Exception):
        status_code = 503

    async def flow():
        async with tracing.run("log"):
            async with tracing.run("decompose", kind="llm", model="m"):
                raise Boom("provider down")

    with pytest.raises(Boom):
        asyncio.run(flow())
    t = sent[0][2]["trace"]
    assert t["status"] == "error"
    assert [s["status"] for s in t["spans"]] == ["error", "error"]
    attrs = t["spans"][1]["attributes"]
    assert attrs["error_type"] == "Boom" and attrs["http_status"] == 503


def test_span_cap_keeps_the_tree_linked(sent):
    async def flow():
        async with tracing.run("log"):
            async with tracing.run("agent", kind="agent", model="m") as agent:
                for i in range(205):
                    async with tracing.run(f"tool-{i}", kind="tool"):
                        async with tracing.run("inner", kind="custom"):
                            pass
                agent.set(output="done")

    asyncio.run(flow())
    t = sent[0][2]["trace"]
    assert len(t["spans"]) == tracing.MAX_SPANS
    assert t["metadata"]["spans_dropped"] == 2 + 2 * 205 - tracing.MAX_SPANS
    ids = {s["id"] for s in t["spans"]}
    assert all(s["parentSpanId"] is None or s["parentSpanId"] in ids for s in t["spans"])


def test_oversized_trace_is_truncated_not_dropped(sent):
    big = "x " * 1_950  # spaced: real text, not something the base64 rule should catch

    async def flow():
        async with tracing.run("log"):
            for i in range(150):
                async with tracing.run(f"s{i}", kind="llm", model="m", input=[big] * 3) as r:
                    r.set(output=[big] * 3)

    asyncio.run(flow())
    payload = sent[0][2]
    assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= tracing._MAX_BODY
    assert len(payload["trace"]["spans"]) == 151
    assert payload["trace"]["spans"][1]["input"] == "[truncated: trace over the size limit]"


def test_redaction():
    r = tracing.redact({
        "api_key": "abc",
        "Authorization": "Bearer xyz",
        "nested": {"password": "p", "user_token": "t"},
        "text": (
            "key ft_live_abc_def and sk-ABCDEFGHIJKLMNOPQRSTUV, Bearer AAAAAAAAAAAAAAAA, "
            "mail me@example.com, call (555) 123-4567"
        ),
        "note": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
        "url": "https://api.nal.usda.gov/fdc/v1/foods/search?api_key=SECRET123&query=egg",
        "img": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==",
        "prompt": 'Usual weight unit: lb\nProfile notes: has diabetes\n\nUtterance: "two eggs"',
        "food": "180.4 pounds this morning, 2 eggs and 3.75 lb of shrimp, 1,200 calories",
        "nan": float("nan"),
        "blob": b"\x00" * 10,
        "model": SimpleNamespace(),
    })
    assert r["api_key"] == r["Authorization"] == "[REDACTED]"
    assert r["nested"] == {"password": "[REDACTED]", "user_token": "[REDACTED]"}
    text = r["text"]
    assert "ft_live" not in text and "sk-ABC" not in text and "AAAAAAAA" not in text
    assert "[EMAIL]" in text and "example.com" not in text
    assert "[PHONE]" in text and "123-4567" not in text
    assert r["note"] == "[REDACTED]"
    assert r["url"] == "https://api.nal.usda.gov/fdc/v1/foods/search?api_key=[REDACTED]&query=egg"
    assert r["img"] == "[DATA_URL]"
    assert "has diabetes" not in r["prompt"] and 'Utterance: "two eggs"' in r["prompt"]
    notes = tracing._scrub(
        "Last weigh-in: none yet\nProfile notes: lactose intolerant\nHIV meds at 8pm\n\n"
        "trying to conceive\n\nEarlier in this conversation: nothing\n\nUtterance: \"eggs\""
    )
    assert "HIV" not in notes and "conceive" not in notes and "Earlier in this conversation" in notes
    # identifiers are not phone numbers: barcodes and all-digit uuid groups survive
    assert tracing._scrub("source=off source_id=3038350013941 upc 041331092609") == (
        "source=off source_id=3038350013941 upc 041331092609"
    )
    assert tracing._scrub("call 555-123-4567 or +1 555 123 4567") == "call [PHONE] or [PHONE]"
    # the run's own content survives: quantities, weights, prices
    assert r["food"] == "180.4 pounds this morning, 2 eggs and 3.75 lb of shrimp, 1,200 calories"
    assert r["nan"] == "nan" and r["blob"] == "[10 bytes]"
    assert isinstance(r["model"], str)


def test_scrub_clips_before_it_scans():
    import time

    t0 = time.perf_counter()
    out = tracing._scrub("a_" * 50_000)  # no '@' anywhere: the e-mail rule used to backtrack
    assert time.perf_counter() - t0 < 0.5
    assert len(out) < 4_100 and out.endswith("chars]")


def test_redaction_unwraps_models_and_caps_sizes():
    from models import ParsedItem

    item = ParsedItem(
        phrase="egg", phrase_kind="food", qty=2, unit=None, state=None, brand=None,
        library_match=None, meal_ref=None,
    )
    out = tracing.redact({"items": [item], "long": "y " * 2_500, "many": list(range(150))})
    assert out["items"][0]["phrase"] == "egg" and out["items"][0]["qty"] == 2
    assert len(out["long"]) < 4_100 and out["long"].endswith("chars]")
    assert len(out["many"]) == 101 and out["many"][-1] == "[+50 items]"


def _http_error(code: int) -> urllib.error.HTTPError:
    body = json.dumps({"error": {"code": "x", "message": "m", "requestId": "req-1"}}).encode()
    return urllib.error.HTTPError("https://example.invalid", code, "msg", {}, io.BytesIO(body))


def _posts(monkeypatch, outcomes: list):
    calls: list[str] = []
    it = iter(outcomes)

    def post(url, body, key):
        calls.append(url)
        outcome = next(it)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(tracing, "_post", post)
    return calls


def test_send_retries_only_network_429_and_5xx(monkeypatch):
    monkeypatch.setenv(tracing.KEY_ENV, "ft_live_test_not_a_real_key")
    monkeypatch.setattr(tracing.time, "sleep", lambda s: None)

    calls = _posts(monkeypatch, [_http_error(400)])  # a wrong payload: no retry
    assert tracing._send("t", "u", b"{}") is None and len(calls) == 1

    calls = _posts(monkeypatch, [_http_error(404)])
    assert tracing._send("t", "u", b"{}") is None and len(calls) == 1

    calls = _posts(monkeypatch, [_http_error(503), (201, {"ok": True})])
    assert tracing._send("t", "u", b"{}") == {"status": 201, "body": {"ok": True}}
    assert len(calls) == 2

    calls = _posts(monkeypatch, [_http_error(429), _http_error(429), (201, {})])
    assert tracing._send("t", "u", b"{}")["status"] == 201 and len(calls) == 3

    calls = _posts(monkeypatch, [urllib.error.URLError("down"), (201, {})])
    assert tracing._send("t", "u", b"{}")["status"] == 201 and len(calls) == 2

    calls = _posts(monkeypatch, [_http_error(500)] * 4)  # gives up after the retries
    assert tracing._send("t", "u", b"{}") is None and len(calls) == 4

    calls = _posts(monkeypatch, [(200, {"duplicate": True})])
    assert tracing._send("t", "u", b"{}")["body"]["duplicate"] is True

    calls = _posts(monkeypatch, [RuntimeError("unexpected")])  # never raises into the app
    assert tracing._send("t", "u", b"{}") is None

    monkeypatch.delenv(tracing.KEY_ENV)
    monkeypatch.setattr(tracing, "_env_loaded", True)  # don't fall back to .env
    calls = _posts(monkeypatch, [(201, {})])
    assert tracing._send("t", "u", b"{}") is None and calls == []  # no key: nothing posted


def test_scores_queue_then_flush(sent):
    trace_id = "a" * 32
    tracing.score_commit("trace-langfuse-era", "user_edited", 1)  # not a FireTrace id: skipped
    tracing.score_commit(trace_id, "user_edited", 1, comment="item 0")
    tracing.score_commit(trace_id, "tier", 2)
    tracing.score_commit(trace_id, "bad name!", 1)  # not [A-Za-z0-9_-]: skipped
    assert sent == []  # queued, not sent
    tracing.flush()
    assert [url for _, url, _ in sent] == [f"{tracing.INGEST_URL}/{trace_id}/scores"] * 2
    assert sent[0][2] == {"name": "user_edited", "dataType": "numeric", "value": 1, "comment": "item 0"}
    assert sent[1][2] == {"name": "tier", "dataType": "numeric", "value": 2}
    assert tracing._scores == []


def test_flush_middleware_waits_for_the_request_sends():
    async def flow():
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        loop.call_later(0.05, fut.set_result, {"status": 201})
        messages: list[dict] = []

        async def app(scope, receive, send):
            tracing._pending.get().append(fut)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok", "more_body": False})

        async def send(message):
            messages.append(message)

        async def receive():
            return {"type": "http.request"}

        await tracing.FlushMiddleware(app)({"type": "http"}, receive, send)
        assert fut.done() and len(messages) == 2
        assert tracing._pending.get() is None

        # an unhandled error: the error trace's send is awaited before it propagates
        fut2 = loop.create_future()
        loop.call_later(0.05, fut2.set_result, {"status": 201})

        async def failing(scope, receive, send):
            tracing._pending.get().append(fut2)
            raise RuntimeError("db down")

        with pytest.raises(RuntimeError):
            await tracing.FlushMiddleware(failing)({"type": "http"}, receive, send)
        assert fut2.done() and tracing._pending.get() is None

    asyncio.run(flow())


def test_sdk_span_still_open_at_root_exit_is_marked_unfinished(sent):
    from agents.tracing.span_data import FunctionSpanData

    sdk_span = SimpleNamespace(
        span_id="span_late", parent_id=None, error=None,
        span_data=FunctionSpanData(name="log_food", input=None, output=None),
        started_at="2026-09-06T21:00:00.000000+00:00", ended_at=None,
    )
    live: dict = {}

    async def flow():
        async with tracing.run("log", kind="llm", model="m"):
            tracing._sdk_span_start(live, sdk_span)  # never ends inside the run: cancelled

    asyncio.run(flow())
    sdk_span.ended_at = "2026-09-06T21:00:09.000000+00:00"
    tracing._sdk_span_end(live, sdk_span)  # too late: the trace is sent
    t = sent[0][2]["trace"]
    tool = next(s for s in t["spans"] if s["name"] == "log_food")
    assert tool["status"] == "error" and tool["attributes"]["unfinished"] is True
    assert tool["endedAt"] == t["endedAt"]


def test_agents_sdk_spans_nest_under_the_current_run(sent):
    """A real Runner.run over a fake model: the SDK's agent and tool spans
    land in the trace, and a run opened inside the tool (decompose under
    log_food) hangs off the tool's span."""
    from agents import Agent, Runner, function_tool
    from agents.items import ModelResponse
    from agents.models.interface import Model
    from agents.tracing import get_trace_provider, set_trace_processors
    from agents.usage import Usage
    from openai.types.responses import (
        ResponseFunctionToolCall,
        ResponseOutputMessage,
        ResponseOutputText,
    )

    class FakeModel(Model):
        turn = 0

        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, *, previous_response_id=None,
                               conversation_id=None, prompt=None):
            self.turn += 1
            if self.turn == 1:
                item = ResponseFunctionToolCall(
                    type="function_call", call_id="call_1", name="log_food",
                    arguments='{"text": "two eggs"}', id="fc_1",
                )
            else:
                item = ResponseOutputMessage(
                    id="msg_1", type="message", role="assistant", status="completed",
                    content=[ResponseOutputText(type="output_text", text="done", annotations=[])],
                )
            return ModelResponse(
                output=[item], usage=Usage(requests=1, input_tokens=10, output_tokens=5, total_tokens=15),
                response_id=None,
            )

        def stream_response(self, *args, **kwargs):
            raise NotImplementedError

    @function_tool
    async def log_food(text: str) -> str:
        async with tracing.run("decompose", kind="llm", provider="openai", model="gpt-5.6-luna", input=text) as d:
            d.set(output={"items": [text]}, usage={"input_tokens": 3, "output_tokens": 2})
        return json.dumps({"items": 1})

    agent = Agent(name="fake-agent", instructions="call the tool", model=FakeModel(), tools=[log_food])
    # ours alone (no export to OpenAI's dashboard), then exactly what was
    # registered before — set_trace_processors replaces the whole list
    registered = list(get_trace_provider()._multi_processor._processors)
    set_trace_processors([tracing._AgentsProcessor()])
    try:
        async def flow():
            async with tracing.run("log", input="two eggs", session_id="log-2") as root:
                async with tracing.run("agent", kind="agent", provider="openai", model="gpt-5.6-terra") as a:
                    result = await Runner.run(agent, "two eggs")
                    a.set(output=result.final_output)
                root.set(output={"reply": result.final_output})

        asyncio.run(flow())
    finally:
        set_trace_processors(registered)

    t = sent[0][2]["trace"]
    by_id = {s["id"]: s for s in t["spans"]}
    by_name = {s["name"]: s for s in t["spans"]}
    assert set(by_name) >= {"log", "agent", "Agent workflow", "fake-agent", "turn 1", "log_food", "decompose"}

    def ancestors(span):
        while span["parentSpanId"]:
            span = by_id[span["parentSpanId"]]
            yield span["name"]

    # log → agent → Agent workflow (the SDK's task span) → fake-agent → turn 1 → log_food → decompose
    assert list(ancestors(by_name["decompose"])) == [
        "log_food", "turn 1", "fake-agent", "Agent workflow", "agent", "log",
    ]
    assert by_name["fake-agent"]["kind"] == "agent" and by_name["Agent workflow"]["kind"] == "chain"
    assert by_name["turn 1"]["kind"] == "chain" and by_name["turn 1"]["attributes"]["agent"] == "fake-agent"
    tool = by_name["log_food"]
    assert tool["kind"] == "tool"
    assert tool["input"] == {"text": "two eggs"} and tool["output"] == {"items": 1}
    assert t["model"] == "gpt-5.6-terra" and t["usage"]["totalTokens"] == 5
    for s in t["spans"]:
        assert set(s) <= SPAN_KEYS and ISO.match(s["startedAt"]) and ISO.match(s["endedAt"])


def test_response_span_maps_model_usage_and_hosted_search(sent):
    """The SDK's `response` span (one per model turn) → an llm span with the
    model and usage, and a hosted web_search_call in its output → a child
    tool span bounded by the turn."""
    from agents.tracing.span_data import ResponseSpanData

    source = SimpleNamespace(url="https://www.costco.com/shrimp")
    search = SimpleNamespace(
        type="web_search_call", status="completed",
        action=SimpleNamespace(type="search", query="costco shrimp platter", sources=[source], url=None),
    )
    response = SimpleNamespace(
        id="resp_1", model="gpt-5.6-terra", output=[search],
        usage=SimpleNamespace(input_tokens=100, output_tokens=20, total_tokens=120),
    )
    data = ResponseSpanData(response=response, input=[{"role": "user", "content": "hi"}], usage=None)
    sdk_span = SimpleNamespace(
        span_id="span_abc", parent_id=None, span_data=data, error=None,
        started_at="2026-09-06T21:00:00.000000+00:00", ended_at="2026-09-06T21:00:02.500000+00:00",
    )

    async def flow():
        live: dict = {}
        async with tracing.run("log"):
            tracing._sdk_span_start(live, sdk_span)
            tracing._sdk_span_end(live, sdk_span)

    asyncio.run(flow())
    t = sent[0][2]["trace"]
    root, llm, ws = t["spans"]
    assert llm["name"] == "response" and llm["kind"] == "llm" and llm["parentSpanId"] == root["id"]
    assert llm["model"] == "gpt-5.6-terra" and llm["provider"] == "openai"
    assert llm["usage"] == {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120}
    assert llm["startedAt"] == "2026-09-06T21:00:00.000Z" and llm["endedAt"] == "2026-09-06T21:00:02.500Z"
    assert ws["name"] == "web_search" and ws["kind"] == "tool" and ws["parentSpanId"] == llm["id"]
    assert ws["startedAt"] == llm["startedAt"] and ws["endedAt"] == llm["endedAt"]
    assert ws["input"]["query"] == "costco shrimp platter"
    assert ws["output"] == {"sources": ["https://www.costco.com/shrimp"]}
    assert ws["attributes"]["hosted"] is True
    assert t["model"] == "gpt-5.6-terra" and t["usage"]["totalTokens"] == 120


def test_init_registers_the_agents_hook_once(monkeypatch):
    import agents.tracing as agents_tracing

    monkeypatch.setattr(tracing, "_initialized", False)
    tracing.init_tracing()
    first = agents_tracing._firetrace_processor
    monkeypatch.setattr(tracing, "_initialized", False)
    tracing.init_tracing()
    assert agents_tracing._firetrace_processor is first and first.module is tracing
