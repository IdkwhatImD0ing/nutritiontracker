"use client";

import { useEffect, useState } from "react";
import { getSettings, targetsEmpty, type Settings } from "./SettingsApi";
import WizardGoal from "./WizardGoal";
import "@/app/library/m3.css";

const SKIP_KEY = "usual.wizard.skipped.v1";

/** §7.4 first run: after sign-in, while settings.targets is empty, the goal
 *  wizard takes over the screen. No props — mount it once under the
 *  signed-in shell (Today is the natural host); it renders nothing when
 *  targets exist, the settings read fails, or he chose "skip for now"
 *  (Settings keeps a re-run button). */
export default function WizardGate() {
  const [settings, setSettings] = useState<Settings | null>(null);

  useEffect(() => {
    let cancelled = false;
    try {
      if (localStorage.getItem(SKIP_KEY)) return;
    } catch {
      /* storage unavailable — just check the server */
    }
    getSettings()
      .then((s) => {
        if (!cancelled && targetsEmpty(s)) setSettings(s);
      })
      .catch(() => {
        /* no nag on a failed read; the tracker's zero-state still works */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!settings) return null;

  return (
    <WizardGoal
      initial={settings.body}
      onDone={() => {
        setSettings(null);
        // The tracker read its targets from /api/diary on mount (X5b's
        // state, not ours) — a reload is the one honest way to refresh it
        // from here, and this path runs once per install.
        window.location.reload();
      }}
      onSkip={() => {
        try {
          localStorage.setItem(SKIP_KEY, "1");
        } catch {
          /* fine — it will ask again next open */
        }
        setSettings(null);
      }}
    />
  );
}
