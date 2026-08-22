import type { Metadata } from "next";
import History from "@/components/History";

export const metadata: Metadata = { title: "History · Usual" };

export default function HistoryPage() {
  return <History />;
}
