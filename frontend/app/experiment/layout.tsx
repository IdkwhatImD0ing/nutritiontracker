import type { ReactNode } from "react";
import TabBar from "@/components/TabBar";

/** Same shape as app/history/layout.tsx. Experiment is a page pushed from
 *  Settings, not a tab (§7.4), but it still gets the bar, so leaving it never
 *  depends on the back control alone. */
export default function ExperimentLayout({ children }: { children: ReactNode }) {
  return (
    <>
      {children}
      <TabBar />
    </>
  );
}
