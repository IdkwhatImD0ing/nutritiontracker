import BackBar from "@/components/BackBar";

/** A day URL that is not a real date (page.tsx's validDate). It renders in
 *  the history layout, so the tab bar is still there, with the same back
 *  control as a real day. */
export default function DayNotFound() {
  return (
    <main className="shell">
      <header className="brand">
        <BackBar parent="/history" label="History" />
        <h1>Day not found</h1>
      </header>
      <p className="hhint">That isn&rsquo;t a date in your diary.</p>
    </main>
  );
}
