"use client";

import { useEffect, useState } from "react";
import "@/app/experiment/experiment.css";
import BackBar from "./BackBar";
import ErrorNotice from "./ErrorNotice";
import {
  fetchMetrics,
  MetricsUnavailable,
  type MetricsResponse,
} from "./ExperimentApi";
import ExperimentCharts from "./ExperimentCharts";
import Skeleton from "./Skeleton";

const RANGE = "12w";

/** §7.4 /experiment — internal, linked from Settings, not the tab bar. "This
 *  page is the deliverable of the experiment; the dashboard is merely the
 *  product." (§9) */
export default function Experiment() {
  const [data, setData] = useState<MetricsResponse | null>(null);
  const [state, setState] = useState<"loading" | "ok" | "unwired" | "error">(
    "loading",
  );
  // `attempt` re-runs the read: the failure notice carries a Retry.
  const [attempt, setAttempt] = useState(0);

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
  }, [attempt]);

  return (
    <main className="shell">
      <header className="brand">
        <BackBar parent="/settings" label="Settings" />
        <h1>Experiment</h1>
        <span className="date">last {RANGE.replace("w", " weeks")}</span>
      </header>
      <p className="exp-intro">
        Does Tally get faster and more used as it learns? Logging speed, how
        often a phrase is recognised without a lookup, and days logged per week
        against the Cronometer baseline — weekly, over logged meals.
      </p>

      {/* the three Primary charts, roughly to size */}
      {state === "loading" ? <Skeleton rows={3} height={240} /> : null}
      {state === "unwired" ? (
        <div className="exp-notice">
          Charts aren&rsquo;t available on this server yet.
        </div>
      ) : null}
      {state === "error" ? (
        <ErrorNotice
          message="Couldn’t load the metrics."
          onRetry={() => {
            setState("loading");
            setAttempt((n) => n + 1);
          }}
        />
      ) : null}
      {state === "ok" && data ? (
        data.weeks.length === 0 ? (
          <p className="hhint">No logged meals in this range yet.</p>
        ) : (
          <ExperimentCharts data={data} />
        )
      ) : null}
    </main>
  );
}
