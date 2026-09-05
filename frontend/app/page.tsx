import type { Metadata } from "next";
import TabBar from "@/components/TabBar";
import Today from "@/components/Today";
import WizardGate from "@/components/WizardGate";

// Only ever seen by a signed-in visitor: proxy.ts rewrites `/` to the landing
// route for everyone else, and that route brings its own title.
//
// The suffix is spelled out because the root layout's "%s · Tally" template
// does not reach this file — a template skips the segment that defines it, and
// app/page.tsx shares the root segment with app/layout.tsx. Child segments
// (/history, /library, /settings) do get it, so those stay bare.
export const metadata: Metadata = { title: "Today · Tally" };

/** §7.4 Today: the capture bar + staging + diary, the three-tab bar, and the
 *  first-run goal wizard (renders nothing once settings.targets exist). */
export default function Home() {
  return (
    <>
      <Today />
      <WizardGate />
      <TabBar />
    </>
  );
}
