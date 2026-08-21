"""Live decompose tests (DESIGN.md §6.2) — real gpt-5.6-luna calls.

Requires OPENAI_API_KEY; loaded from the repo-root .env if not already set.
Run: uv run --with pytest pytest backend/tests/test_decompose_live.py -q -rP
"""

import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def _load_env() -> None:
    env_path = BACKEND.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env()

from models import ParsedItem, ParsedLog  # noqa: E402
from services.context import AgentContext  # noqa: E402
from services.decompose import _drop_invalid_library_matches, decompose  # noqa: E402

FAKE_INDEX_ID = "1a55a1de-0000-4000-8000-000000000001"
OTHER_INDEX_ID = "2b66b2ef-0000-4000-8000-000000000002"

CTX = AgentContext(
    profile_notes="",
    targets={},
    food_index=[
        (FAKE_INDEX_ID, "Core Power Elite 42g chocolate", "Fairlife"),
        (OTHER_INDEX_ID, "chicken breast, roasted", None),
    ],
    meal_names=["usual breakfast"],
)

VALID_IDS = {FAKE_INDEX_ID, OTHER_INDEX_ID}
LATENCIES: list[float] = []


def _run(text: str, ctx: AgentContext = CTX) -> ParsedLog:
    async def go() -> ParsedLog:
        t0 = time.perf_counter()
        parsed = await decompose(text, ctx)
        LATENCIES.append(time.perf_counter() - t0)
        return parsed

    return asyncio.run(go())


def _assert_ids_in_index(parsed: ParsedLog) -> None:
    for item in parsed.items:
        assert item.library_match is None or item.library_match in VALID_IDS


def test_two_eggs_and_a_fairlife():
    parsed = _run("two eggs and a fairlife")
    assert len(parsed.items) == 2
    eggs = next(i for i in parsed.items if "egg" in i.phrase.lower())
    other = next(i for i in parsed.items if "egg" not in i.phrase.lower())
    assert eggs.qty == 2
    assert eggs.unit is None
    # no invented quantities: "a fairlife" carries no spoken number
    assert other.qty in (None, 1)
    assert all(i.phrase_kind == "food" for i in parsed.items)
    _assert_ids_in_index(parsed)


def test_meal_ref():
    parsed = _run("my usual breakfast")
    assert len(parsed.items) == 1
    assert parsed.items[0].phrase_kind == "meal_ref"
    assert parsed.items[0].qty is None
    _assert_ids_in_index(parsed)


def test_explicit_grams():
    parsed = _run("40g of whey")
    assert len(parsed.items) == 1
    assert parsed.items[0].qty == 40
    assert parsed.items[0].unit == "g"
    _assert_ids_in_index(parsed)


def test_library_match_fires():
    parsed = _run("a fairlife protein shake")
    assert len(parsed.items) == 1
    assert parsed.items[0].library_match == FAKE_INDEX_ID
    _assert_ids_in_index(parsed)


def test_invalid_library_match_dropped():
    # pure (no API): the validator discards any id not in the passed index
    parsed = ParsedLog(
        items=[
            ParsedItem(
                phrase="mystery shake",
                phrase_kind="food",
                qty=None,
                unit=None,
                state=None,
                brand=None,
                library_match="ffffffff-0000-4000-8000-00000000dead",
                meal_ref=None,
            ),
            ParsedItem(
                phrase="fairlife",
                phrase_kind="food",
                qty=None,
                unit=None,
                state=None,
                brand="Fairlife",
                library_match=FAKE_INDEX_ID,
                meal_ref=None,
            ),
        ],
        meal_hint=None,
        when_hint=None,
        save_intent=None,
    )
    out = _drop_invalid_library_matches(parsed, VALID_IDS)
    assert out.items[0].library_match is None
    assert out.items[1].library_match == FAKE_INDEX_ID


def test_zz_report_latency():
    assert LATENCIES, "no live calls recorded"
    print(
        f"\ndecompose live latency over {len(LATENCIES)} calls: "
        f"median={statistics.median(LATENCIES) * 1000:.0f}ms  "
        f"all={[f'{s * 1000:.0f}ms' for s in LATENCIES]}"
    )
