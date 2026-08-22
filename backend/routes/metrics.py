"""GET /api/metrics?range= — the §9 weekly series for the /experiment page.

Primary series (queries over committed logs, bucketed by the local ISO week of
``captured_at`` in the settings timezone):
- latency:  median + p90 of committed_at − captured_at (ms), and separately
            staged_ms (capture → list rendered)
- hit_rate: count(tier ≤ 2 OR short_circuit) / count(*) over items in
            committed logs — plus the companion over committed + DISCARDED
            logs (a committed-only denominator would flatter the curve)
- adherence: distinct local dates with ≥ 1 entry per week, alongside
            cronometer_baseline_days_per_week from settings (0)

Secondary series: edit rate, disambiguation rate, tier-3 resolutions, short-
circuit share, discarded logs, capture failures. Plus the §4 duplicate-suspect
food pairs (a snapshot, not a series). Removed rows (status='removed') are
excluded from every item-level series — they never resolved into the diary.

Every week in the range is emitted (gaps are zero counts / null rates), so the
charts show silence as silence. Exposes an APIRouter; the integrator wires it.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import db
from auth import require_user

router = APIRouter(dependencies=[Depends(require_user)])

_RANGE_RE = re.compile(r"^(\d{1,3})w$")
DEFAULT_RANGE = "12w"
_DUPLICATE_LIMIT = 50


# ---------------------------------------------------------------------------
# Wire shapes (route-local)
# ---------------------------------------------------------------------------


class LatencyPoint(BaseModel):
    week: date
    committed_logs: int
    commit_ms_p50: float | None
    commit_ms_p90: float | None
    staged_ms_p50: float | None
    staged_ms_p90: float | None


class HitRatePoint(BaseModel):
    week: date
    items: int  # items in committed logs
    hits: int
    rate: float | None
    items_incl_discarded: int  # companion: committed + discarded logs
    hits_incl_discarded: int
    rate_incl_discarded: float | None


class AdherencePoint(BaseModel):
    week: date
    days_logged: int


class SecondaryPoint(BaseModel):
    week: date
    committed_logs: int
    items: int
    edited: int
    edit_rate: float | None
    disambiguated: int
    disambiguation_rate: float | None
    tier3: int
    short_circuit_logs: int
    short_circuit_share: float | None
    discarded_logs: int
    capture_failures: int


class DuplicatePair(BaseModel):
    source: str
    food_a: str
    name_a: str
    food_b: str
    name_b: str


class MetricsResponse(BaseModel):
    range: str
    timezone: str
    weeks: list[date]
    baseline_days_per_week: int
    latency: list[LatencyPoint]
    hit_rate: list[HitRatePoint]
    adherence: list[AdherencePoint]
    secondary: list[SecondaryPoint]
    duplicate_suspects: list[DuplicatePair]


# ---------------------------------------------------------------------------
# SQL — one grouped query per source table
# ---------------------------------------------------------------------------

# commit latency spans two clocks: captured_at is the CLIENT's (the phone
# supplies it on POST /api/logs) and committed_at is the DB server's, so a
# client running ahead yields a negative sample. GREATEST(.., 0) floors it —
# an unsynced clock cannot drag the p50 below zero.
#
# §9's capture-failure counter is one row per INCIDENT. A server-detected
# failure already owns a status='capture_failure' row, and the client then
# reports the same incident on its next log as capture_fallback +
# capture_error='empty_transcript' (Today.tsx, for both server reasons) — that
# echo is dropped here. Every other client string (upload_failed, and the
# recorder classes permission_denied / recorder_error / too_short /
# silent_blob, all detected before any upload) has no server row and counts.
_LOGS_SQL = """
SELECT date_trunc('week', captured_at AT TIME ZONE %(tz)s)::date AS week,
       count(*) FILTER (WHERE status = 'committed')                       AS committed_logs,
       count(*) FILTER (WHERE status = 'discarded')                       AS discarded_logs,
       count(*) FILTER (WHERE status = 'capture_failure'
                           OR (status <> 'capture_failure'
                               AND (capture_fallback OR capture_error IS NOT NULL)
                               AND capture_error
                                   IS DISTINCT FROM 'empty_transcript'))  AS capture_failures,
       count(*) FILTER (WHERE status = 'committed' AND short_circuit)     AS short_circuit_logs,
       percentile_cont(0.5) WITHIN GROUP
         (ORDER BY EXTRACT(EPOCH FROM GREATEST(committed_at - captured_at, interval '0')) * 1000)
         FILTER (WHERE status = 'committed' AND committed_at IS NOT NULL) AS commit_ms_p50,
       percentile_cont(0.9) WITHIN GROUP
         (ORDER BY EXTRACT(EPOCH FROM GREATEST(committed_at - captured_at, interval '0')) * 1000)
         FILTER (WHERE status = 'committed' AND committed_at IS NOT NULL) AS commit_ms_p90,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY staged_ms)
         FILTER (WHERE status = 'committed' AND staged_ms IS NOT NULL)    AS staged_ms_p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY staged_ms)
         FILTER (WHERE status = 'committed' AND staged_ms IS NOT NULL)    AS staged_ms_p90
FROM logs
WHERE %(start)s::timestamptz IS NULL OR captured_at >= %(start)s
GROUP BY 1
"""

_ITEMS_SQL = """
SELECT date_trunc('week', l.captured_at AT TIME ZONE %(tz)s)::date AS week,
       count(*) FILTER (WHERE l.status = 'committed')                                  AS items,
       count(*) FILTER (WHERE l.status = 'committed'
                          AND (ir.tier <= 2 OR l.short_circuit))                       AS hits,
       count(*) FILTER (WHERE l.status IN ('committed', 'discarded'))                  AS items_incl_discarded,
       count(*) FILTER (WHERE l.status IN ('committed', 'discarded')
                          AND (ir.tier <= 2 OR l.short_circuit))                       AS hits_incl_discarded,
       count(*) FILTER (WHERE l.status = 'committed' AND ir.was_edited)                AS edited,
       count(*) FILTER (WHERE l.status = 'committed' AND ir.was_disambiguated)         AS disambiguated,
       count(*) FILTER (WHERE l.status = 'committed' AND ir.tier = 3)                  AS tier3
FROM item_resolutions ir
JOIN logs l ON l.id = ir.log_id
WHERE ir.status <> 'removed'
  AND (%(start)s::timestamptz IS NULL OR l.captured_at >= %(start)s)
GROUP BY 1
"""

_ADHERENCE_SQL = """
SELECT date_trunc('week', d::timestamp)::date AS week, count(*) AS days_logged
FROM (SELECT DISTINCT (logged_at AT TIME ZONE %(tz)s)::date AS d
      FROM entries
      WHERE %(start)s::timestamptz IS NULL OR logged_at >= %(start)s) x
GROUP BY 1
"""

# §4 invariant telemetry: same-source pairs with near-identical per_100g and a
# shared name token (≥ 3 chars). a.id < b.id keeps each pair once.
_DUPLICATES_SQL = """
SELECT a.source, a.id::text AS food_a, a.display_name AS name_a,
       b.id::text AS food_b, b.display_name AS name_b
FROM foods a
JOIN foods b ON b.source = a.source AND a.id < b.id
WHERE a.per_100g IS NOT NULL AND b.per_100g IS NOT NULL
  AND abs((a.per_100g->>'kcal')::numeric      - (b.per_100g->>'kcal')::numeric)      <= 5
  AND abs((a.per_100g->>'protein_g')::numeric - (b.per_100g->>'protein_g')::numeric) <= 1
  AND abs((a.per_100g->>'carb_g')::numeric    - (b.per_100g->>'carb_g')::numeric)    <= 1
  AND abs((a.per_100g->>'fat_g')::numeric     - (b.per_100g->>'fat_g')::numeric)     <= 1
  AND EXISTS (
        SELECT 1
        FROM unnest(regexp_split_to_array(lower(a.display_name), '[^a-z0-9]+')) AS t
        WHERE length(t) >= 3
          AND t = ANY (regexp_split_to_array(lower(b.display_name), '[^a-z0-9]+'))
      )
ORDER BY a.display_name, b.display_name
LIMIT %(limit)s
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def parse_range(value: str) -> int | None:
    """'12w' → 12; 'all' → None. Anything else is a 422."""
    if value == "all":
        return None
    m = _RANGE_RE.match(value)
    if not m or int(m.group(1)) < 1:
        raise HTTPException(status_code=422, detail="range must be '<n>w' (1–999) or 'all'")
    return int(m.group(1))


def _rate(num: int, den: int) -> float | None:
    return None if den == 0 else num / den


async def _timezone() -> ZoneInfo:
    row = await db.fetchone("SELECT value FROM settings WHERE key = 'timezone'")
    try:
        return ZoneInfo(row["value"]) if row else ZoneInfo("UTC")
    except (KeyError, ValueError, OSError, TypeError):  # bad/unset setting → UTC
        return ZoneInfo("UTC")


def _week_list(first: date, last: date) -> list[date]:
    weeks = []
    w = first
    while w <= last:
        weeks.append(w)
        w += timedelta(days=7)
    return weeks


def _f(v) -> float | None:
    return None if v is None else float(v)


# ---------------------------------------------------------------------------
# GET /api/metrics
# ---------------------------------------------------------------------------


@router.get("/api/metrics", response_model=MetricsResponse)
async def get_metrics(range: str = Query(DEFAULT_RANGE)) -> MetricsResponse:
    weeks_back = parse_range(range)
    tz = await _timezone()
    today = datetime.now(timezone.utc).astimezone(tz).date()
    this_monday = today - timedelta(days=today.weekday())

    start: datetime | None = None
    if weeks_back is not None:
        first_monday = this_monday - timedelta(weeks=weeks_back - 1)
        start = datetime.combine(first_monday, time.min, tzinfo=tz)

    params = {"tz": tz.key, "start": start}
    logs = {r["week"]: r for r in await db.fetchall(_LOGS_SQL, params)}
    items = {r["week"]: r for r in await db.fetchall(_ITEMS_SQL, params)}
    adherence = {r["week"]: r for r in await db.fetchall(_ADHERENCE_SQL, params)}
    dup_rows = await db.fetchall(_DUPLICATES_SQL, {"limit": _DUPLICATE_LIMIT})
    brow = await db.fetchone(
        "SELECT value FROM settings WHERE key = 'cronometer_baseline_days_per_week'"
    )
    baseline = int(brow["value"] or 0) if brow else 0

    if weeks_back is not None:
        first = this_monday - timedelta(weeks=weeks_back - 1)
    else:
        seen = [*logs, *items, *adherence]
        first = min(seen) if seen else this_monday
    weeks = _week_list(first, this_monday)

    latency, hit_rate, adh, secondary = [], [], [], []
    for w in weeks:
        lg = logs.get(w) or {}
        it = items.get(w) or {}
        committed = int(lg.get("committed_logs") or 0)
        n_items = int(it.get("items") or 0)
        hits = int(it.get("hits") or 0)
        n_incl = int(it.get("items_incl_discarded") or 0)
        hits_incl = int(it.get("hits_incl_discarded") or 0)
        edited = int(it.get("edited") or 0)
        disamb = int(it.get("disambiguated") or 0)
        sc = int(lg.get("short_circuit_logs") or 0)
        latency.append(
            LatencyPoint(
                week=w,
                committed_logs=committed,
                commit_ms_p50=_f(lg.get("commit_ms_p50")),
                commit_ms_p90=_f(lg.get("commit_ms_p90")),
                staged_ms_p50=_f(lg.get("staged_ms_p50")),
                staged_ms_p90=_f(lg.get("staged_ms_p90")),
            )
        )
        hit_rate.append(
            HitRatePoint(
                week=w,
                items=n_items,
                hits=hits,
                rate=_rate(hits, n_items),
                items_incl_discarded=n_incl,
                hits_incl_discarded=hits_incl,
                rate_incl_discarded=_rate(hits_incl, n_incl),
            )
        )
        adh.append(AdherencePoint(week=w, days_logged=int((adherence.get(w) or {}).get("days_logged") or 0)))
        secondary.append(
            SecondaryPoint(
                week=w,
                committed_logs=committed,
                items=n_items,
                edited=edited,
                edit_rate=_rate(edited, n_items),
                disambiguated=disamb,
                disambiguation_rate=_rate(disamb, n_items),
                tier3=int(it.get("tier3") or 0),
                short_circuit_logs=sc,
                short_circuit_share=_rate(sc, committed),
                discarded_logs=int(lg.get("discarded_logs") or 0),
                capture_failures=int(lg.get("capture_failures") or 0),
            )
        )

    return MetricsResponse(
        range=range,
        timezone=tz.key,
        weeks=weeks,
        baseline_days_per_week=baseline,
        latency=latency,
        hit_rate=hit_rate,
        adherence=adh,
        secondary=secondary,
        duplicate_suspects=[DuplicatePair(**r) for r in dup_rows],
    )
