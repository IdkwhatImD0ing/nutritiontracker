"""FireTrace tracing (DESIGN.md §10).

One trace per completed model or agent run, POSTed once — after the run, from
a worker thread, never in the way of the response — to the self-hosted
FireTrace deployment. Stdlib only: no SDK, no new dependency.

    async with tracing.run("decompose", kind="llm", input=text,
                           provider="openai", model=MODEL) as r:
        ...
        r.set(output=parsed, usage=response.usage)

``run()`` is the one primitive. With no trace current it starts one (a fresh
32-hex id) and is its root span; nested, it is a child span (16-hex id,
parentSpanId = the enclosing run — or the enclosing openai-agents span when
called from inside a tool). The current run rides a ContextVar, so concurrent
requests and the chat route's background task each see their own trace.
``current_trace_id()`` is what ``logs.trace_id`` / ``item_resolutions.trace_id``
store; ``score_commit()`` + ``flush()`` post the commit-time scores onto it.

openai-agents runs (tier 3, the front-door agent) need no wrapping of their
own: the SDK's tracing hooks emit a workflow span, the agent span, one
``turn N`` span per model turn holding its ``response`` span (model + token
usage) and one ``function`` span per tool call, and the processor
``init_tracing()`` registers maps them into the current trace.
A hosted ``web_search`` call has no timing of its own, so it becomes a child
tool span bounded by its model turn, marked ``hosted``.

A trace is sent only when a model actually ran (a span of kind llm/agent, or
one carrying a model): a §3.0 short-circuit or a §8.6 replay records nothing.
``FlushMiddleware`` keeps the ASGI invocation open — after the response has
gone out — until that request's sends finish, so a frozen serverless
instance cannot lose one; lifespan shutdown drains the rest. Retries only for
network errors, 429 and 5xx (three, with backoff); any other 4xx means the
payload is wrong — logged, never retried. Nothing here can raise into the app.

Secrets and personal data are redacted from input, output, metadata and
attributes before serialisation — FireTrace keeps traces forever: key-like
fields, bearer tokens, API keys, JWTs, key-bearing query strings, data URLs
and base64 blobs, e-mail addresses, phone numbers, and the agent prompt's
profile-notes line. userId is the Neon Auth ``sub``, never the address. The
key decides the environment server-side, so no environment field is sent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

log = logging.getLogger(__name__)

BASE_URL = "https://tracing.art3m1s.me"  # the endpoint is not a secret; the key is
INGEST_URL = f"{BASE_URL}/api/v1/traces"
KEY_ENV = "FIRETRACE_API_KEY"

SCHEMA_VERSION = 1
MAX_SPANS = 200  # the API's cap per trace
MAX_TAGS = 20
_MAX_NAME = 500
_MAX_TAG = 64
_MAX_STRING = 4_000  # per string value, after redaction
_MAX_ITEMS = 100  # per list / dict
_MAX_BODY = 1_500_000  # bytes; the API refuses 2 MiB requests, 768 kB stored documents
_SEND_TIMEOUT_S = 10.0
_BACKOFF_S = (1.0, 2.0, 4.0)  # three retries — only for network errors, 429, 5xx
_DRAIN_S = 20.0

_KINDS = frozenset({"llm", "agent", "tool", "chain", "retriever", "embedding", "reranker", "custom"})
_TRACE_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_SCORE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# ---------------------------------------------------------------------------
# env
# ---------------------------------------------------------------------------


def _load_env() -> None:
    """Populate os.environ from backend/.env or repo-root .env (local dev only;
    deployed environments set real env vars). Existing values win."""
    here = Path(__file__).resolve().parent
    for env_path in (here / ".env", here.parent / ".env"):
        if not env_path.is_file():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_env_loaded = False


def api_key() -> str | None:
    """The key, read at send time (per-environment scope on the host). Never
    logged, never stored anywhere else."""
    global _env_loaded
    if KEY_ENV not in os.environ and not _env_loaded:
        _env_loaded = True
        _load_env()
    return os.environ.get(KEY_ENV) or None


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _iso(value: Any) -> str:
    """ISO 8601, UTC, millisecond precision, 'Z' — for our own clock and for
    the openai-agents timestamps (isoformat with +00:00)."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if not isinstance(value, datetime):
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _now() -> str:
    return _iso(datetime.now(timezone.utc))


def _clip(text: Any, limit: int) -> str:
    return str(text)[:limit]


def _usage(value: Any) -> dict[str, int] | None:
    """{inputTokens, outputTokens, totalTokens} from an OpenAI usage object,
    the SDK's span usage dict, or a dict in either spelling; None when empty."""
    if value is None:
        return None
    if isinstance(value, dict):
        get = value.get
    else:
        def get(name, default=None):
            return getattr(value, name, default)
    out: dict[str, int] = {}
    names = (
        ("inputTokens", ("input_tokens", "inputTokens", "prompt_tokens")),
        ("outputTokens", ("output_tokens", "outputTokens", "completion_tokens")),
        ("totalTokens", ("total_tokens", "totalTokens")),
    )
    for ours, theirs in names:
        for name in theirs:
            v = get(name)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
                out[ours] = int(v)
                break
    if "totalTokens" not in out and "inputTokens" in out and "outputTokens" in out:
        out["totalTokens"] = out["inputTokens"] + out["outputTokens"]
    return out or None


def _maybe_json(value: Any) -> Any:
    """Tool arguments and results travel as JSON strings; show them structured."""
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in "{[":
            try:
                return json.loads(text)
            except ValueError:
                pass
    return value


# ---------------------------------------------------------------------------
# redaction — applied to every input / output / attributes / metadata value
# ---------------------------------------------------------------------------

_SECRET_KEY_RE = re.compile(
    r"api[-_]?key|secret|token|passw(?:or)?d|authorization|cookie|bearer|credential|jwt", re.I
)
_SCRUB: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"ft_live_[A-Za-z0-9_]+"), "[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "[REDACTED]"),
    (re.compile(r"\b(Bearer|Token)\s+[A-Za-z0-9._~+/=-]{8,}", re.I), r"\1 [REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[REDACTED]"),
    (re.compile(r"([?&](?:api[_-]?key|key|token|secret|access_token)=)[^&\s]+", re.I), r"\1[REDACTED]"),
    (re.compile(r"data:[\w.+/-]+;base64,[A-Za-z0-9+/=]+"), "[DATA_URL]"),
    (re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{200,}={0,2}(?![A-Za-z0-9+/=])"), "[BINARY]"),
    # anchored at a boundary and bounded, so a long run of word characters is
    # scanned once, not quadratically
    (re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63})+"), "[EMAIL]"),
    # written phone numbers: separators required between the groups, so a
    # bare digit run — a UPC/EAN barcode, an id — survives; "180.4 pounds"
    # and "2 eggs" do too
    (re.compile(r"(?<![\d.])(?:\+?\d{1,3}[\s.-])?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\d.])"), "[PHONE]"),
    # services/agent.agent_input: free text the person wrote about themselves,
    # possibly several lines — up to the prompt's next section
    (re.compile(r"(Profile notes:)[\s\S]*?(?=\n\nEarlier in this conversation|\n\nUtterance:|\Z)"),
     r"\1 [REDACTED]"),
)


def _scrub(text: str) -> str:
    extra = len(text) - _MAX_STRING
    if extra > 0:  # clip first: the patterns never scan an unbounded string
        text = text[:_MAX_STRING]
    for pattern, repl in _SCRUB:
        text = pattern.sub(repl, text)
    if extra > 0:
        text += f"…[+{extra} chars]"
    return text


def redact(value: Any, _depth: int = 0) -> Any:
    """A JSON-safe copy of `value` with secrets and personal data removed and
    sizes capped. Pydantic models, dataclasses and datetimes are unwrapped."""
    if _depth > 12:
        return "[nested too deep]"
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _scrub(value)
    if isinstance(value, (bytes, bytearray)):
        return f"[{len(value)} bytes]"
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= _MAX_ITEMS:
                out["…"] = f"[+{len(value) - _MAX_ITEMS} keys]"
                break
            k = str(k)
            out[k] = "[REDACTED]" if _SECRET_KEY_RE.search(k) else redact(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        out_list = [redact(v, _depth + 1) for v in items[:_MAX_ITEMS]]
        if len(items) > _MAX_ITEMS:
            out_list.append(f"[+{len(items) - _MAX_ITEMS} items]")
        return out_list
    dump = getattr(value, "model_dump", None)  # pydantic (our models, the OpenAI types)
    if callable(dump):
        try:
            return redact(dump(mode="json"), _depth + 1)
        except Exception:
            pass
    if is_dataclass(value) and not isinstance(value, type):
        try:
            return redact(asdict(value), _depth + 1)
        except Exception:
            pass
    return _scrub(str(value))


def _dumps(obj: Any) -> bytes:
    return json.dumps(
        obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=str
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# the trace being built
# ---------------------------------------------------------------------------


@dataclass
class _Span:
    id: str
    parent_id: str | None
    name: str
    kind: str
    started_at: str
    ended_at: str | None = None
    status: str = "unset"
    provider: str | None = None
    model: str | None = None
    input: Any = None
    output: Any = None
    attributes: dict = field(default_factory=dict)
    usage: dict | None = None
    kept: bool = True  # False past MAX_SPANS: parent lookups only, never sent

    def wire(self) -> dict:
        d: dict[str, Any] = {
            "id": self.id,
            "parentSpanId": self.parent_id,
            "name": _clip(self.name, _MAX_NAME) or "span",
            "kind": self.kind if self.kind in _KINDS else "custom",
            "status": self.status if self.status in ("ok", "error") else "ok",
            "startedAt": self.started_at,
            "endedAt": self.ended_at or self.started_at,
        }
        if self.provider:
            d["provider"] = _clip(self.provider, 200)
        if self.model:
            d["model"] = _clip(self.model, 200)
        if self.input is not None:
            d["input"] = redact(self.input)
        if self.output is not None:
            d["output"] = redact(self.output)
        if self.attributes:
            d["attributes"] = redact(self.attributes)
        if self.usage:
            d["usage"] = self.usage
        return d


@dataclass
class _Trace:
    id: str
    name: str
    started_at: str
    spans: list[_Span] = field(default_factory=list)  # the kept ones, in start order
    session_id: str | None = None
    user_id: str | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    sdk_map: dict[str, str] = field(default_factory=dict)  # openai-agents span id → ours
    all_spans: dict[str, _Span] = field(default_factory=dict)
    dropped: int = 0
    finished: bool = False  # payload built: later span updates are too late

    def add_span(self, *, parent_id: str | None, **fields: Any) -> _Span:
        span_id = secrets.token_hex(8)
        while span_id in self.all_spans:
            span_id = secrets.token_hex(8)
        # a child of a dropped span hangs off the nearest kept ancestor
        while parent_id is not None:
            parent = self.all_spans.get(parent_id)
            if parent is None or parent.kept:
                break
            parent_id = parent.parent_id
        span = _Span(id=span_id, parent_id=parent_id, **fields)
        self.all_spans[span_id] = span
        if len(self.spans) < MAX_SPANS:
            self.spans.append(span)
        else:
            span.kept = False
            self.dropped += 1
        return span


_UNSET: Any = object()


class Run:
    """The handle ``run()`` yields: set what the step produced, read the ids."""

    __slots__ = ("trace", "span", "is_root", "sent")

    def __init__(self, trace: _Trace, span: _Span, is_root: bool) -> None:
        self.trace = trace
        self.span = span
        self.is_root = is_root
        self.sent: asyncio.Future | None = None  # the root's send, once scheduled

    @property
    def trace_id(self) -> str:
        return self.trace.id

    @property
    def span_id(self) -> str:
        return self.span.id

    def set(
        self,
        *,
        input: Any = _UNSET,
        output: Any = _UNSET,
        usage: Any = None,
        model: str | None = None,
        provider: str | None = None,
        status: str | None = None,
        attributes: dict | None = None,
        tags: Iterable[str] | None = None,
        session_id: Any = None,
        user_id: Any = None,
    ) -> "Run":
        s = self.span
        if input is not _UNSET:
            s.input = input
        if output is not _UNSET:
            s.output = output
        if usage is not None:
            u = _usage(usage)
            if u:
                s.usage = u
        if model:
            s.model = str(model)
        if provider:
            s.provider = str(provider)
        if status in ("ok", "error"):
            s.status = status
        if attributes:
            s.attributes.update(attributes)
        if tags:
            self.trace.tags.extend(str(t) for t in tags if t)
        if session_id:
            self.trace.session_id = str(session_id)
        if user_id:
            self.trace.user_id = str(user_id)
        return self


_current: ContextVar[Run | None] = ContextVar("firetrace_run", default=None)


def current_run() -> Run | None:
    return _current.get()


def current_trace_id() -> str | None:
    """The id of the trace this request is recording (for logs.trace_id) —
    None when tracing is off, so nothing points at a trace that never was."""
    r = _current.get()
    return r.trace.id if r is not None and api_key() else None


def _bind_user(trace: _Trace) -> None:
    """userId = the JWT sub auth.require_user bound (never the address), and
    the owner/demo tag — when a request is bound at all."""
    try:
        import auth  # lazy: this module stays importable without the app

        user = auth.current_user.get()
    except Exception:
        return
    if not user:
        return
    if user.get("id"):
        trace.user_id = str(user["id"])
    trace.tags.append("owner" if user.get("is_owner") else "demo")


def _sdk_parent(trace: _Trace) -> str | None:
    """Inside an openai-agents tool call, the tool's span is the parent."""
    try:
        from agents.tracing import get_current_span

        current = get_current_span()
    except Exception:
        return None
    if current is None:
        return None
    return trace.sdk_map.get(current.span_id)


@asynccontextmanager
async def run(
    name: str,
    *,
    kind: str = "chain",
    input: Any = None,
    provider: str | None = None,
    model: str | None = None,
    session_id: Any = None,
    user_id: Any = None,
    tags: Iterable[str] = (),
    attributes: dict | None = None,
) -> AsyncIterator[Run]:
    """One step of a run — or the run itself, when nothing is current. An
    exception in the body marks the span (and a root, the trace) ``error``
    and is re-raised; a root schedules its send on exit."""
    parent = _current.get()
    started = _now()
    if parent is None:
        trace = _Trace(id=secrets.token_hex(16), name=_clip(name, _MAX_NAME) or "run", started_at=started)
        _bind_user(trace)
        parent_id = None
    else:
        trace = parent.trace
        parent_id = _sdk_parent(trace) or parent.span.id
    span = trace.add_span(
        parent_id=parent_id,
        name=_clip(name, _MAX_NAME) or "span",
        kind=kind if kind in _KINDS else "custom",
        started_at=started,
        provider=provider,
        model=model,
        input=input,
        attributes=dict(attributes or {}),
    )
    handle = Run(trace, span, parent is None)
    handle.set(session_id=session_id, user_id=user_id, tags=list(tags))
    token = _current.set(handle)
    try:
        yield handle
    except BaseException as e:
        span.status = "error"
        span.attributes.setdefault("error_type", type(e).__name__)
        code = getattr(e, "status_code", None)
        if isinstance(code, int):
            span.attributes.setdefault("http_status", code)
        raise
    finally:
        _current.reset(token)
        span.ended_at = _now()
        if span.status == "unset":
            span.status = "ok"
        if parent is None:
            handle.sent = _finish(trace)


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------


def _payload(trace: _Trace) -> bytes:
    spans = trace.spans
    root = spans[0]
    ended = max((s.ended_at for s in spans if s.ended_at), default=_now())
    for s in spans:
        if s.ended_at is None:  # still open at root exit: a cancelled agent run
            s.ended_at = ended
            if s.status == "unset":
                s.status = "error"
            s.attributes["unfinished"] = True
    lead = next((s for s in spans if s.kind in ("llm", "agent") and s.model), None) or next(
        (s for s in spans if s.model), None
    )
    usage: dict[str, int] = {}
    for s in spans:
        for k, v in (s.usage or {}).items():
            usage[k] = usage.get(k, 0) + v
    tags: list[str] = []
    for t in trace.tags:
        t = _clip(t, _MAX_TAG)
        if t and t not in tags:
            tags.append(t)
    metadata = dict(trace.metadata)
    if trace.dropped:
        metadata["spans_dropped"] = trace.dropped
    t: dict[str, Any] = {
        "id": trace.id,
        "name": trace.name,
        "status": root.status if root.status in ("ok", "error") else "ok",
        "startedAt": trace.started_at,
        "endedAt": ended,
        "tags": tags[:MAX_TAGS],
        "metadata": redact(metadata),
        "spans": [s.wire() for s in spans],
    }
    if lead is not None:
        t["model"] = _clip(lead.model, 200)
        if lead.provider:
            t["provider"] = _clip(lead.provider, 200)
    if usage:
        t["usage"] = usage
    if trace.session_id:
        t["sessionId"] = _clip(trace.session_id, 200)
    if trace.user_id:
        t["userId"] = _clip(trace.user_id, 200)
    if root.input is not None:
        t["input"] = redact(root.input)
    if root.output is not None:
        t["output"] = redact(root.output)
    payload = {"schemaVersion": SCHEMA_VERSION, "trace": t}
    body = _dumps(payload)
    if len(body) > _MAX_BODY:  # the strings are capped; this is many spans at once
        for s in t["spans"]:
            for key in ("input", "output"):
                if key in s:
                    s[key] = "[truncated: trace over the size limit]"
        body = _dumps(payload)
    if len(body) > _MAX_BODY:
        for key in ("input", "output"):
            if key in t:
                t[key] = "[truncated: trace over the size limit]"
        body = _dumps(payload)
    return body


def _finish(trace: _Trace) -> asyncio.Future | None:
    """Root run over: build, then hand to the sender. Never raises."""
    trace.finished = True
    try:
        if not api_key():
            return None
        if not any(s.model or s.kind in ("llm", "agent") for s in trace.spans):
            log.debug("firetrace: %s ran no model; nothing recorded", trace.name)
            return None
        body = _payload(trace)
    except Exception:
        log.warning("firetrace: could not build the trace payload", exc_info=True)
        return None
    return _schedule(f"trace {trace.id} ({trace.name})", INGEST_URL, body)


# ---------------------------------------------------------------------------
# sending — worker thread, bounded retries, never raises
# ---------------------------------------------------------------------------


def _post(url: str, body: bytes, key: str) -> tuple[int, Any]:
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "tally-backend/0.1",
        },
    )
    with urllib.request.urlopen(req, timeout=_SEND_TIMEOUT_S) as resp:
        raw = resp.read()
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        return resp.status, data


def _error_body(e: urllib.error.HTTPError) -> tuple[str, str, str]:
    try:
        err = (json.loads(e.read() or b"{}") or {}).get("error") or {}
    except Exception:
        err = {}
    return (
        str(err.get("code") or ""),
        _clip(err.get("message") or "", 300),
        str(err.get("requestId") or ""),
    )


def _send(what: str, url: str, body: bytes) -> dict | None:
    """Blocking; runs in a worker thread. {"status", "body"} on 2xx, else
    None. Never raises; never logs the key or the payload."""
    key = api_key()
    if not key:
        return None
    attempts = len(_BACKOFF_S) + 1
    for i in range(attempts):
        delay = _BACKOFF_S[i] if i < len(_BACKOFF_S) else None
        try:
            status, data = _post(url, body, key)
        except urllib.error.HTTPError as e:
            code, message, request_id = _error_body(e)
            if e.code == 429 or e.code >= 500:
                if delay is None:
                    log.warning(
                        "firetrace: %s not recorded after %d attempts (HTTP %s %s)",
                        what, attempts, e.code, code,
                    )
                    return None
                log.info("firetrace: %s got HTTP %s; retrying in %.0fs", what, e.code, delay)
                time.sleep(delay)
                continue
            # any other 4xx: the payload (or the id, or the key) is wrong — fix
            # it, don't retry. A 404 on a score is a trace that was never
            # stored: a pre-FireTrace id, or a send that failed.
            log.warning(
                "firetrace: %s rejected: HTTP %s %s — %s (requestId %s)",
                what, e.code, code, message, request_id,
            )
            return None
        except OSError as e:  # URLError, timeouts, refused connections
            if delay is None:
                log.warning(
                    "firetrace: %s not recorded after %d attempts (%s)",
                    what, attempts, type(e).__name__,
                )
                return None
            log.info("firetrace: %s hit %s; retrying in %.0fs", what, type(e).__name__, delay)
            time.sleep(delay)
            continue
        except Exception:
            log.warning("firetrace: %s failed", what, exc_info=True)
            return None
        if status == 200 and isinstance(data, dict) and data.get("duplicate"):
            log.warning("firetrace: %s was a duplicate — the same id and body were sent twice", what)
        else:
            log.debug("firetrace: %s recorded (HTTP %s)", what, status)
        return {"status": status, "body": data if isinstance(data, dict) else {}}
    return None


_pending: ContextVar[list[asyncio.Future] | None] = ContextVar("firetrace_pending", default=None)
_inflight: set[asyncio.Future] = set()
# our own pool: a send blocks its thread through the retries, and the loop's
# default executor is what getaddrinfo (every new outbound connection) uses
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="firetrace")


def _schedule(what: str, url: str, body: bytes) -> asyncio.Future | None:
    """Run the send off the event loop. Registered with the request's pending
    list (FlushMiddleware) and the process-wide set (lifespan drain). Never
    raises — it runs from run()'s finally."""
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop (a sync script): a plain thread
            threading.Thread(
                target=_send, args=(what, url, body), name="firetrace-send", daemon=True
            ).start()
            return None
        fut = loop.run_in_executor(_executor, _send, what, url, body)
    except Exception as e:  # the executor or the interpreter is shutting down
        log.warning("firetrace: %s not scheduled (%s)", what, type(e).__name__)
        return None
    _inflight.add(fut)
    fut.add_done_callback(_inflight.discard)
    pending = _pending.get()
    if pending is not None:
        pending.append(fut)
    return fut


async def drain(futures: Iterable[asyncio.Future] | None = None, timeout: float = _DRAIN_S) -> None:
    """Wait (bounded) for sends: the given ones, else everything in flight."""
    todo = [f for f in (list(futures) if futures is not None else list(_inflight)) if not f.done()]
    if not todo:
        return
    _, still = await asyncio.wait(todo, timeout=timeout)
    if still:
        log.warning("firetrace: %d send(s) still in flight after %.0fs", len(still), timeout)


async def drain_pending(timeout: float = _DRAIN_S) -> None:
    """Wait for the sends scheduled in this context. For a task that outlives
    its request (the chat route's run after a disconnect): the middleware may
    already have returned, so the task waits for its own send before it ends."""
    pending = _pending.get()
    if pending:
        await drain(pending, timeout)


class FlushMiddleware:
    """Pure ASGI: once the response has gone out — or the app has raised —
    wait (bounded) for the sends this request scheduled. The client never
    waits; a 500's error trace is awaited before the error propagates."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        token = _pending.set([])
        try:
            await self.app(scope, receive, send)
        finally:
            pending = _pending.get() or []
            _pending.reset(token)
            if pending:
                await drain(pending)


# ---------------------------------------------------------------------------
# scores (resolver/commit.py): queued by score_commit, sent by flush
# ---------------------------------------------------------------------------

_scores: list[tuple[str, dict]] = []


def score_commit(trace_id: str, name: str, value: Any, comment: str | None = None) -> None:
    """Queue a score onto a stored trace id (§10 outcome loop). Nothing is sent
    until flush() — the commit path batches its scores and flushes once."""
    if not trace_id or not _TRACE_ID_RE.match(str(trace_id)):
        log.debug("firetrace: score %s skipped — %r is not a FireTrace trace id", name, trace_id)
        return
    if not _SCORE_NAME_RE.match(str(name)):
        log.warning("firetrace: score name %r is not [A-Za-z0-9_-]{1,64}; skipped", name)
        return
    if isinstance(value, bool):
        data_type: str = "boolean"
        val: Any = value
    elif isinstance(value, (int, float)):
        data_type, val = "numeric", value
    else:
        data_type, val = "categorical", str(value)
    score: dict[str, Any] = {"name": name, "dataType": data_type, "value": val}
    if comment:
        score["comment"] = _scrub(str(comment))[:2000]
    _scores.append((str(trace_id), score))


def flush() -> None:
    """Hand every queued score to the background sender. Non-blocking."""
    while _scores:
        trace_id, score = _scores.pop(0)
        _schedule(
            f"score {score['name']} on {trace_id}",
            f"{INGEST_URL}/{trace_id}/scores",
            _dumps(score),
        )


# ---------------------------------------------------------------------------
# openai-agents hook: the SDK's spans → spans of the current trace
# ---------------------------------------------------------------------------


def _sdk_identity(data: Any) -> tuple[str, str]:
    kind = getattr(data, "type", "") or "custom"
    if kind == "agent":
        return getattr(data, "name", None) or "agent", "agent"
    if kind == "response":
        return "response", "llm"
    if kind == "generation":
        return "generation", "llm"
    if kind == "function":
        return getattr(data, "name", None) or "tool", "tool"
    if kind == "mcp_tools":
        return "mcp_tools", "tool"
    if kind == "task":  # the SDK's workflow span ("Agent workflow")
        return getattr(data, "name", None) or "workflow", "chain"
    if kind == "turn":  # one model turn + the tool calls it made
        return f"turn {getattr(data, 'turn', '?')}", "chain"
    if kind == "handoff":
        return f"handoff to {getattr(data, 'to_agent', None) or '?'}", "agent"
    return getattr(data, "name", None) or kind, "custom"


def _hosted_web_searches(trace: _Trace, parent: _Span, response: Any) -> None:
    """web_search_call items of a model turn: no timing of their own, so a
    child tool span bounded by the turn (query in, consulted sources out —
    the same items services/tier3._web_evidence reads)."""
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "web_search_call":
            continue
        action = getattr(item, "action", None)
        sources = [
            u for s in (getattr(action, "sources", None) or []) if (u := getattr(s, "url", None))
        ]
        url = getattr(action, "url", None)
        if url and url not in sources:
            sources.append(url)
        child = trace.add_span(
            parent_id=parent.id,
            name="web_search",
            kind="tool",
            started_at=parent.started_at,
            ended_at=parent.ended_at,
            status="ok" if getattr(item, "status", "completed") in (None, "completed") else "error",
            provider="openai",
            input={"query": getattr(action, "query", None), "action": getattr(action, "type", None)},
            output={"sources": sources},
            attributes={"hosted": True},
        )
        child.attributes["response_id"] = getattr(response, "id", None)


def _sdk_fill(trace: _Trace, ours: _Span, data: Any) -> None:
    kind = getattr(data, "type", "")
    if kind == "response":
        response = getattr(data, "response", None)
        ours.provider = "openai"
        ours.model = getattr(response, "model", None) or ours.model
        ours.usage = _usage(getattr(data, "usage", None)) or _usage(getattr(response, "usage", None))
        if getattr(data, "input", None) is not None:
            ours.input = data.input
        if response is not None:
            ours.output = list(getattr(response, "output", None) or [])
            if getattr(response, "id", None):
                ours.attributes["response_id"] = response.id
            _hosted_web_searches(trace, ours, response)
    elif kind == "generation":
        ours.provider = "openai"
        ours.model = getattr(data, "model", None)
        ours.usage = _usage(getattr(data, "usage", None))
        ours.input = getattr(data, "input", None)
        ours.output = getattr(data, "output", None)
    elif kind == "function":
        ours.input = _maybe_json(getattr(data, "input", None))
        ours.output = _maybe_json(getattr(data, "output", None))
    elif kind == "agent":
        for key in ("tools", "output_type", "handoffs"):
            value = getattr(data, key, None)
            if value:
                ours.attributes[key] = value
    elif kind == "custom":
        ours.attributes.update(getattr(data, "data", None) or {})
    elif kind == "turn":
        ours.attributes["agent"] = getattr(data, "agent_name", None)
        ours.attributes["turn"] = getattr(data, "turn", None)
    elif kind == "task":
        pass  # the usage it carries is the model turns' own, already summed from them
    else:  # handoff, guardrail, transcription, speech, mcp_tools …
        if getattr(data, "model", None):
            ours.model = data.model
        try:
            ours.attributes.update(data.export())
        except Exception:
            pass


def _sdk_span_start(live: dict, span: Any) -> None:
    current = _current.get()
    if current is None:
        return  # an agent run outside any recorded run: nothing to attach to
    trace = current.trace
    parent = trace.sdk_map.get(span.parent_id) if getattr(span, "parent_id", None) else None
    name, kind = _sdk_identity(span.span_data)
    ours = trace.add_span(
        parent_id=parent or current.span.id, name=name, kind=kind, started_at=_iso(span.started_at)
    )
    trace.sdk_map[span.span_id] = ours.id
    live[span.span_id] = (trace, ours)


def _sdk_span_end(live: dict, span: Any) -> None:
    entry = live.pop(span.span_id, None)
    if entry is None:
        return
    trace, ours = entry
    if trace.finished:  # sent already, marked unfinished there (a cancelled run)
        return
    ours.ended_at = _iso(span.ended_at)
    error = getattr(span, "error", None)
    if error:
        ours.status = "error"
        message = error.get("message") if isinstance(error, dict) else str(error)
        ours.attributes["error"] = _clip(_scrub(str(message or "error")), 500)
    else:
        ours.status = "ok"
    _sdk_fill(trace, ours, span.span_data)


class _AgentsProcessor:
    """agents.tracing.TracingProcessor by duck type (the SDK only calls these).
    Registered once per process; `module` is re-pointed at this module on
    every init so a reloaded tracing module (the tests do that) is the one
    the hook talks to."""

    def __init__(self) -> None:
        self.live: dict[str, tuple[_Trace, _Span]] = {}
        self.module: Any = sys.modules[__name__]

    def on_trace_start(self, trace: Any) -> None:
        pass

    def on_trace_end(self, trace: Any) -> None:
        pass

    def on_span_start(self, span: Any) -> None:
        try:
            self.module._sdk_span_start(self.live, span)
        except Exception:
            log.debug("firetrace: agents span start ignored", exc_info=True)

    def on_span_end(self, span: Any) -> None:
        try:
            self.module._sdk_span_end(self.live, span)
        except Exception:
            log.debug("firetrace: agents span end ignored", exc_info=True)

    def shutdown(self) -> None:
        pass

    def force_flush(self) -> None:
        pass


_initialized = False


def init_tracing() -> None:
    """Idempotent: read .env, say whether FireTrace is on, hook the
    openai-agents tracer. Safe to call from every AI route."""
    global _initialized
    if _initialized:
        return
    _initialized = True
    _load_env()
    if api_key():
        log.info("firetrace: on (%s)", INGEST_URL)
    else:
        log.info("firetrace: off — %s is not set", KEY_ENV)
    try:
        import agents.tracing as agents_tracing

        processor = getattr(agents_tracing, "_firetrace_processor", None)
        if processor is None:
            processor = _AgentsProcessor()
            agents_tracing._firetrace_processor = processor
            agents_tracing.add_trace_processor(processor)
        processor.module = sys.modules[__name__]
    except Exception:
        log.warning("firetrace: openai-agents tracing hook not installed", exc_info=True)
