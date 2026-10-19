import type { Metadata } from "next";
import { notFound } from "next/navigation";
import HistoryDay from "@/components/HistoryDay";

/** YYYY-MM-DD and a real calendar date (rejects 2026-02-30). */
function validDate(s: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
  const [y, m, d] = s.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  return dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d;
}

/** The tab names the day ("Saturday, Oct 10"; "Thu, Oct 10, 2024" outside
 *  this year, as the day view's title). Bare: the root layout's
 *  "%s · Tally" template adds the suffix (spelled out, it read "Diary · Tally
 *  · Tally"). HistoryApi's longDate can't be imported here — that module is
 *  "use client" — so the same format is inline, in UTC because the date is
 *  a calendar day, not an instant. */
export async function generateMetadata({
  params,
}: {
  params: Promise<{ date: string }>;
}): Promise<Metadata> {
  const { date } = await params;
  if (!validDate(date)) return { title: "Diary" };
  const d = new Date(`${date}T00:00:00Z`);
  const year = d.getUTCFullYear() !== new Date().getUTCFullYear();
  return {
    title: d.toLocaleDateString("en-US", {
      weekday: year ? "short" : "long",
      month: "short",
      day: "numeric",
      ...(year ? { year: "numeric" } : {}),
      timeZone: "UTC",
    }),
  };
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
