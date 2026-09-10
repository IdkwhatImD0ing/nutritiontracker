"""Wire contracts for Tally — pydantic models, defined once (DESIGN.md §2, §5, §6.2).

FastAPI's /openapi.json is generated from these; frontend TypeScript is generated
from that via scripts/typegen.sh. Never hand-maintain the TS side.
"""

from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Meal(str, Enum):
    breakfast = "breakfast"
    lunch = "lunch"
    dinner = "dinner"
    snack = "snack"


class Macros(BaseModel):
    """§5: core macros required, fiber optional, extra micros allowed (all numeric)."""

    model_config = ConfigDict(extra="allow")
    __pydantic_extra__: dict[str, float] = Field(init=False)  # type: ignore[assignment]

    kcal: float
    protein_g: float
    carb_g: float
    fat_g: float
    fiber_g: float | None = None


# ---------------------------------------------------------------------------
# StagedItem — the wire type the whole UX pivots on (§5)
# ---------------------------------------------------------------------------


class FoodRef(BaseModel):
    id: str
    name: str
    brand: str | None = None


class ServingRef(BaseModel):
    id: str
    description: str


class ServingOption(BaseModel):
    """One row of foods.servings, surfaced on staged rows so the §7.1 portion
    sheet can list every serving the database defines — with per-single-serving
    macros for the live preview."""

    serving_id: str
    description: str
    grams: float | None = None
    macros: dict[str, float] | None = None


class Alternate(BaseModel):
    source: str | None = None
    source_id: str | None = None
    food_id: str | None = None
    label: str
    usual: bool | None = None


class QuestionOption(BaseModel):
    label: str
    source: str | None = None
    source_id: str | None = None


class Question(BaseModel):
    text: str
    options: list[QuestionOption]


class WebSource(BaseModel):
    """§3.4a: the citation for a web-transcribed (chain) food — the UI must
    render it as a visible, clickable link (OpenAI usage-terms requirement)."""

    url: str
    title: str


class StagedItem(BaseModel):
    item_index: int
    phrase: str  # as spoken; for tier-0 expansions: the food display name
    status: Literal["resolved", "searching", "needs_input", "not_found"]
    tier: Literal[0, 1, 2, 3] | None = None
    food: FoodRef | None = None
    serving: ServingRef | None = None
    qty: float | None = None
    macros: Macros | None = None  # per current qty, from cache — display only
    meal: Meal  # per-item; header chip propagates (§7.1)
    alternates: list[Alternate] | None = None
    state_question: bool | None = None  # one-time cooked/raw toggle (§3.4a)
    question: Question | None = None
    confidence: Literal["high", "medium", "low"] | None = None
    servings: list[ServingOption] | None = None  # the food's serving list (§7.1 portion sheet)
    search_instead: bool | None = None  # §3.3 rule 3 "search instead" affordance
    web_source: WebSource | None = None  # §3.4a citation link (web-transcribed foods)
    # §3.4a photo lane (M4) — what vision guessed: "portion" on a database
    # food whose amount came from the photo, "unresolved" on an item no
    # database matched (its foods row is source='estimate'; it carries NO
    # numbers, cannot be confirmed or committed, and routes the user to
    # Search). Renders flagged; the commit transaction refuses the row until
    # estimate_confirmed is set, and only POST /api/resolve/confirm-estimate
    # sets it (routes/photo.py).
    estimated: Literal["none", "portion", "unresolved"] = "none"
    estimate_note: str | None = None  # user-facing: what vision saw / why it is a guess
    estimate_confirmed: bool | None = None


# ---------------------------------------------------------------------------
# Decompose output (§6.2) — LLM structured-output contract, not a route body.
# Nullable fields are required-but-nullable (matches the spec and OpenAI strict mode).
# ---------------------------------------------------------------------------


class ParsedItem(BaseModel):
    phrase: str  # quantity words removed
    phrase_kind: Literal["food", "meal_ref"]
    qty: float | None  # explicit only — never invented
    unit: str | None  # as spoken: "bottle","scoop","g","oz","cup"
    state: Literal["cooked", "raw", "dry"] | None
    brand: str | None
    library_match: str | None  # id from the inlined personal food index (§3.3)
    meal_ref: str | None  # id from the inlined saved-meals list (§3.1)


class SaveIntent(BaseModel):
    name: str


class ParsedLog(BaseModel):
    items: list[ParsedItem]
    meal_hint: Meal | None  # only if spoken ("for breakfast I had…")
    when_hint: str | None  # "this morning", "yesterday lunch" — verbatim, for traces
    # §7.2: the MODEL resolves the spoken time words against the NOW line in
    # its input (BACKLOG 189). 0 = the local day of the utterance, -1 = the day
    # before, and so on; null when no time reference was spoken. The server
    # only does the arithmetic — no phrase is ever pattern-matched, so a form
    # nobody enumerated ("the day before yesterday", "Sunday") still lands.
    when_day_offset: int | None
    # the meal the TIME words imply ("last night" -> snack, "yesterday
    # morning" -> breakfast), which is not the same thing as meal_hint (a meal
    # the user named outright). meal_hint outranks it, exactly as before.
    when_meal: Meal | None
    save_intent: SaveIntent | None


# ---------------------------------------------------------------------------
# Route request/response models (§5)
# ---------------------------------------------------------------------------


class AgentTurn(BaseModel):
    """One earlier turn of the Agent chat (§6.4), as the client remembers it:
    what the person said, what the agent replied, and — for a turn that
    staged a log — the log's id, its rows in words (never macros) and where
    it stands, so a follow-up ("actually I only had half") can name it."""

    role: Literal["user", "agent"]
    text: str
    log_id: UUID | None = None
    items: list[str] | None = None  # "Plain cooked shrimp — 12.5 × 4 large" …
    state: Literal["staged", "committed", "discarded"] | None = None


class LogCreate(BaseModel):
    id: UUID  # client-generated — the idempotency key (§8.6)
    text: str
    captured_at: datetime
    tz: str
    capture_fallback: bool = False
    capture_error: str | None = None
    # the + sheet's Agent lane (§6.4): route the utterance through the
    # front-door agent after the §3.0 short-circuit (food and/or a weigh-in)
    agent: bool = False
    # the chat so far (the last turns, oldest first) — what the agent reads to
    # understand references, answers to its own question, and corrections
    history: list[AgentTurn] | None = None
    # §3.4b in the chat (2026-09-08): the staged photo log this conversation is
    # still tuning. Set, this turn is part of a photo conversation — it runs on
    # services/agent.PHOTO_MODEL and gets the `adjust_photo` tool, closed over
    # THIS log, so the model never names one. The client derives it from its own
    # chat (the newest photo result card that is still staged) and stops sending
    # it once that log commits or is discarded. Untrusted like every client
    # field: the tool re-reads the log and refuses anything not staged, and RLS
    # keeps it inside the tenant.
    photo_log_id: UUID | None = None


class LogResponse(BaseModel):
    log_id: UUID
    meal: Meal
    items: list[StagedItem]
    # §7.2: the diary time these rows will commit to, when the utterance's time
    # words moved it off the capture day ("yesterday", "last night"). It used to
    # live ONLY in the staged jsonb — in no response field and on no card — so a
    # day the model got wrong could not be caught before commit and could not be
    # explained after. Null when nothing shifted, which is the ordinary case.
    logged_at: datetime | None = None


class CommitRequest(BaseModel):
    items: list[StagedItem]


class ResolveRequest(BaseModel):
    log_id: UUID
    item_index: int
    phrase: str
    qty: float | None = None
    unit: str | None = None


class Tier3KickRequest(BaseModel):
    log_id: UUID
    item_index: int


class MacroTargets(BaseModel):
    kcal: float
    protein_g: float
    carb_g: float
    fat_g: float


class DayTotals(BaseModel):
    """Consumed totals for the day vs targets (§5: commit response, diary)."""

    kcal: float
    protein_g: float
    carb_g: float
    fat_g: float
    targets: MacroTargets
