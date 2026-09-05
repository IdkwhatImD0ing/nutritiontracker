import type { Metadata } from "next";
import History from "@/components/History";

// Bare leaf: the root layout's "%s · Tally" template adds the suffix, so
// spelling it out here rendered "History · Tally · Tally".
export const metadata: Metadata = { title: "History" };

export default function HistoryPage() {
  return <History />;
}
