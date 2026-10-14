import Link from "next/link";

/** Any unmatched URL, in the root layout's chrome rather than Next's bare
 *  black page. No tab bar: signed-out visitors land here too (a bad /docs
 *  link), and `/` is Today for a signed-in visitor, the landing page for
 *  anyone else. */
export default function NotFound() {
  return (
    <main className="shell">
      <header className="brand">
        <h1>Page not found</h1>
      </header>
      <p className="hhint">There&rsquo;s nothing at this address.</p>
      <Link href="/" className="commit-btn">
        Open Tally
      </Link>
    </main>
  );
}
