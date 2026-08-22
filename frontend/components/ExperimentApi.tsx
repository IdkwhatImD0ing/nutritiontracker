"use client";

/**
 * §9 metrics for /experiment — `GET /api/metrics?range=<n>w|all`
 * (backend/routes/metrics.py): four parallel weekly series keyed by ISO-week
 * Monday (local per settings.timezone, oldest first, every week present —
 * gaps are zero counts / null rates) plus the §4 duplicate-suspect pairs as
 * a snapshot. Hand-typed against that route until typegen reruns; the
 * parser is tolerant so a missing series renders as "no data", not a crash.
 */

import { ApiError } from "@/lib/api";
import { apiRequest } from "./DiaryApi";

export type LatencyPoint = {
  week: string;
  committed_logs: number;
  commit_ms_p50: number | null;
  commit_ms_p90: number | null;
  staged_ms_p50: number | null;
  staged_ms_p90: number | null;
};

export type HitRatePoint = {
  week: string;
  items: number;
  hits: number;
  rate: number | null; // committed logs only
  items_incl_discarded: number;
  hits_incl_discarded: number;
  rate_incl_discarded: number | null; // the honest companion (§9)
};

export type AdherencePoint = { week: string; days_logged: number };

export type SecondaryPoint = {
  week: string;
  committed_logs: number;
  items: number;
  edited: number;
  edit_rate: number | null;
  disambiguated: number;
  disambiguation_rate: number | null;
  tier3: number;
  short_circuit_logs: number;
  short_circuit_share: number | null;
  discarded_logs: number;
  capture_failures: number;
};

export type DuplicatePair = {
  source: string;
  food_a: string;
  name_a: string;
  food_b: string;
  name_b: string;
};

export type MetricsResponse = {
  range: string;
  timezone: string;
  weeks: string[];
  baseline_days_per_week: number;
  latency: LatencyPoint[];
  hit_rate: HitRatePoint[];
  adherence: AdherencePoint[];
  secondary: SecondaryPoint[];
  duplicate_suspects: DuplicatePair[];
};

export class MetricsUnavailable extends Error {
  constructor() {
    super("GET /api/metrics is not wired yet");
  }
}

const num = (v: unknown): number | null =>
  typeof v === "number" && Number.isFinite(v) ? v : null;
const count = (v: unknown): number => num(v) ?? 0;
const str = (v: unknown): string => (typeof v === "string" ? v : "");

type Row = Record<string, unknown>;
const rows = (v: unknown): Row[] =>
  Array.isArray(v) ? v.filter((r): r is Row => !!r && typeof r === "object") : [];

export function normalizeMetrics(raw: unknown, range: string): MetricsResponse {
  const r = (raw ?? {}) as Row;
  const byWeek = <T extends { week: string }>(list: Row[], map: (w: Row) => T) =>
    list
      .filter((w) => typeof w.week === "string")
      .map(map)
      .sort((a, b) => (a.week < b.week ? -1 : 1));
  const latency = byWeek(rows(r.latency), (w) => ({
    week: str(w.week),
    committed_logs: count(w.committed_logs),
    commit_ms_p50: num(w.commit_ms_p50),
    commit_ms_p90: num(w.commit_ms_p90),
    staged_ms_p50: num(w.staged_ms_p50),
    staged_ms_p90: num(w.staged_ms_p90),
  }));
  const hit_rate = byWeek(rows(r.hit_rate), (w) => ({
    week: str(w.week),
    items: count(w.items),
    hits: count(w.hits),
    rate: num(w.rate),
    items_incl_discarded: count(w.items_incl_discarded),
    hits_incl_discarded: count(w.hits_incl_discarded),
    rate_incl_discarded: num(w.rate_incl_discarded),
  }));
  const adherence = byWeek(rows(r.adherence), (w) => ({
    week: str(w.week),
    days_logged: count(w.days_logged),
  }));
  const secondary = byWeek(rows(r.secondary), (w) => ({
    week: str(w.week),
    committed_logs: count(w.committed_logs),
    items: count(w.items),
    edited: count(w.edited),
    edit_rate: num(w.edit_rate),
    disambiguated: count(w.disambiguated),
    disambiguation_rate: num(w.disambiguation_rate),
    tier3: count(w.tier3),
    short_circuit_logs: count(w.short_circuit_logs),
    short_circuit_share: num(w.short_circuit_share),
    discarded_logs: count(w.discarded_logs),
    capture_failures: count(w.capture_failures),
  }));
  const weeksRaw = Array.isArray(r.weeks) ? r.weeks.filter((w) => typeof w === "string") : [];
  const weeks = (weeksRaw.length ? weeksRaw : latency.map((p) => p.week)) as string[];
  return {
    range: str(r.range) || range,
    timezone: str(r.timezone),
    weeks: [...weeks].sort(),
    baseline_days_per_week: count(r.baseline_days_per_week),
    latency,
    hit_rate,
    adherence,
    secondary,
    duplicate_suspects: rows(r.duplicate_suspects).map((d) => ({
      source: str(d.source),
      food_a: str(d.food_a),
      name_a: str(d.name_a),
      food_b: str(d.food_b),
      name_b: str(d.name_b),
    })),
  };
}

export async function fetchMetrics(range = "12w"): Promise<MetricsResponse> {
  const qs = new URLSearchParams({ range });
  try {
    const raw = await apiRequest<unknown>(`/api/metrics?${qs.toString()}`);
    return normalizeMetrics(raw, range);
  } catch (e) {
    if (e instanceof ApiError && (e.status === 404 || e.status === 405)) {
      throw new MetricsUnavailable();
    }
    throw e;
  }
}
