import type { Metadata } from "next";
import History from "@/components/History";

export const metadata: Metadata = { title: "History · Tally" };

export default function HistoryPage() {
  return <History />;
}
