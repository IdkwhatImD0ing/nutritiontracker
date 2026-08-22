import type { ReactNode } from "react";
import TabBar from "@/components/TabBar";

/** Same shape as app/library/layout.tsx: the §7.4 tab bar mounts once per
 *  section; m3.css (imported by TabBar) pads .shell for it. */
export default function HistoryLayout({ children }: { children: ReactNode }) {
  return (
    <>
      {children}
      <TabBar />
    </>
  );
}
