"""Wire contracts for Usual — pydantic models, defined once (DESIGN.md §2, §5, §6.2).

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
    when_hint: str | None  # "this morning", "yesterday lunch"
    save_intent: SaveIntent | None


# ---------------------------------------------------------------------------
# Route request/response models (§5)
# ---------------------------------------------------------------------------


class LogCreate(BaseModel):
    id: UUID  # client-generated — the idempotency key (§8.6)
    text: str
    captured_at: datetime
    tz: str
    capture_fallback: bool = False
    capture_error: str | None = None


class LogResponse(BaseModel):
    log_id: UUID
    meal: Meal
    items: list[StagedItem]


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
