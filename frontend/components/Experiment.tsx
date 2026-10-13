"use client";

import { useEffect, useState } from "react";
import "@/app/experiment/experiment.css";
import BackBar from "./BackBar";
import {
  fetchMetrics,
  MetricsUnavailable,
  type MetricsResponse,
} from "./ExperimentApi";
import ExperimentCharts from "./ExperimentCharts";

const RANGE = "12w";

/** §7.4 /experiment — internal, linked from Settings, not the tab bar. "This
 *  page is the deliverable of the experiment; the dashboard is merely the
 *  product." (§9) */
export default function Experiment() {
  const [data, setData] = useState<MetricsResponse | null>(null);
  const [state, setState] = useState<"loading" | "ok" | "unwired" | "error">(
    "loading",
  );

  useEffect(() => {
    let cancelled = false;
    fetchMetrics(RANGE)
      .then((d) => {
        if (cancelled) return;
        setData(d);
        setState("ok");
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setState(e instanceof MetricsUnavailable ? "unwired" : "error");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main className="shell">
      <header className="brand">
        <BackBar parent="/settings" label="Settings" />
        <h1>Experiment</h1>
        <span className="date">last {RANGE.replace("w", " weeks")}</span>
      </header>
      <p className="exp-intro">
        Does the app get faster and stickier as it learns? §9: latency, alias
        hit rate, and adherence against the Cronometer baseline — weekly,
        over committed logs.
      </p>

      {state === "loading" ? (
        <p className="hhint">
          <span className="spinner" />
          Loading…
        </p>
      ) : null}
      {state === "unwired" ? (
        <div className="exp-notice">
          <strong>No metrics route yet.</strong> This page reads{" "}
          <code>GET /api/metrics?range={RANGE}</code> (§5); the series render
          as soon as it lands.
        </div>
      ) : null}
      {state === "error" ? (
        <p className="hist-error">Couldn&rsquo;t load the metrics.</p>
      ) : null}
      {state === "ok" && data ? (
        data.weeks.length === 0 ? (
          <p className="hhint">No committed logs in this range yet.</p>
        ) : (
          <ExperimentCharts data={data} />
        )
      ) : null}
    </main>
  );
}
