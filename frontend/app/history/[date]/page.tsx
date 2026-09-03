import type { Metadata } from "next";
import { notFound } from "next/navigation";
import HistoryDay from "@/components/HistoryDay";

export const metadata: Metadata = { title: "Diary · Tally" };

/** YYYY-MM-DD and a real calendar date (rejects 2026-02-30). */
function validDate(s: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
  const [y, m, d] = s.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  return dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d;
}

export default async function HistoryDayPage({
  params,
}: {
  params: Promise<{ date: string }>;
}) {
  const { date } = await params;
  if (!validDate(date)) notFound();
  return <HistoryDay date={date} />;
}
