import type { Metadata } from "next";
import Link from "next/link";
import TallyMark from "@/components/TallyMark";
import { GITHUB_URL, SITE_URL } from "@/lib/site";
import "./landing.css";

/** The public front door. `proxy.ts` sends every signed-out visitor here
 *  instead of straight to the sign-in form, so someone arriving from the
 *  repo or a link learns what Tally is before being asked for an email.
 *  Signed-in visitors never see it — `/` renders Today for them.
 *
 *  It is also the installed app's signed-out screen (a cold launch, or where
 *  Sign out lands). There, landing.css's display-mode: standalone block
 *  trims it to the pitch and the preview, with the two ways in pinned at the
 *  foot (Bill, 2026-10-13); the website's parts stay with the browser.
 *
 *  The structure is FireTrace's landing (header, mono eyebrow, display
 *  headline, framed preview, numbered claims); the brand is Tally's. The
 *  preview is a static picture of the staging sheet in plain elements, not
 *  the real component, so it has nothing to tab into and needs no client JS. */

export const metadata: Metadata = {
  // `/welcome` is only the rewrite target behind `/` (proxy.ts); old links to it
  // still resolve, so point search engines at the one real address.
  alternates: { canonical: "/" },
  title: "Say what you ate",
  description:
    "Tally is a nutrition logger you talk to. Say or type what you ate, check the pre-filled list, tap once. Real database numbers, and it gets faster every week.",
};

const LANES = [
  {
    name: "Voice",
    body: "Say “two eggs and a fairlife”. Both rows come back filled in — food, portion and meal.",
    Icon: () => (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <rect x="9" y="3" width="6" height="11" rx="3" />
        <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
      </svg>
    ),
  },
  {
    name: "Photo",
    body: "Vision names what is on the plate and estimates the amounts. Every estimate is flagged so you can check it before you log it.",
    Icon: () => (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M4 8a2 2 0 0 1 2-2h2l1.5-2h5L16 6h2a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z" />
        <circle cx="12" cy="13" r="3.5" />
      </svg>
    ),
  },
  {
    name: "Barcode",
    body: "Scan the label. The numbers come from the manufacturer’s own record, not an estimate.",
    Icon: () => (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M4 7V5h2M20 7V5h-2M4 17v2h2M20 17v2h-2" />
        <path d="M8 8v8M11 8v8M14 8v8M17 8v8" />
      </svg>
    ),
  },
  {
    name: "Search",
    body: "The boring escape hatch, on purpose. Your own foods first, then the USDA database.",
    Icon: () => (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="11" cy="11" r="6.5" />
        <path d="M16 16l4 4" />
      </svg>
    ),
  },
];

/** The worked example in the preview. Totals and the button label are
 *  derived from it so the picture cannot add up wrong. */
const PREVIEW_ROWS = [
  { name: "Eggs, scrambled", brand: null, portion: "2 large", protein: 12, kcal: 156 },
  { name: "2% milk", brand: "fairlife", portion: "1 bottle", protein: 13, kcal: 120 },
];
const PREVIEW_PROTEIN = PREVIEW_ROWS.reduce((n, r) => n + r.protein, 0);
const PREVIEW_KCAL = PREVIEW_ROWS.reduce((n, r) => n + r.kcal, 0);

const STEPS = [
  {
    k: "You say it",
    body: "Typed or spoken, in your own words. One sentence can hold a whole meal.",
  },
  {
    k: "Tally fills it in",
    body: "Each food is matched to a database record, USDA FoodData Central here, with the portion and meal already chosen.",
  },
  {
    k: "You tap once",
    body: "Nothing is saved until you tap Log. Fix a portion or swap a food first if you need to.",
  },
];

const CLAIMS = [
  {
    title: "It gets faster every week",
    body: (
      <>
        Every meal you log teaches it the phrases you use, the foods they mean and the portions you
        take. A meal you have logged before resolves from your own history with{" "}
        <strong>no AI call at all</strong> — one database round-trip. Only a food you have never
        logged touches the network, once.
      </>
    ),
  },
  {
    title: "It does not make numbers up",
    body: (
      <>
        The calories and macros come from a food database or a brand’s own published label. If
        a food cannot be found, the app says so instead of guessing. For a dish with no label
        anywhere, like a deli platter or a photo of your plate, the amounts are estimated. Every
        estimated row is flagged in the list and in your diary, so you can check it before you
        log it or fix it after.
      </>
    ),
  },
  {
    title: "Your diary is yours",
    body: (
      <>
        Accounts are kept apart by Postgres row-level security, not by application code — the
        app runs as a database role that cannot bypass it. Photos are never stored.
      </>
    ),
  },
];

/** A sample of the MCP server's tools; names and scopes are the ones the
 *  docs list. `write` tools need a read-and-write key. */
const TOOLS = [
  { name: "get_day", scope: "read", body: "A day's entries by meal, with totals against your targets." },
  { name: "log_food", scope: "write", body: "Turns a sentence into staged rows. Nothing is saved yet." },
  { name: "commit_log", scope: "write", body: "Saves the staged rows to your diary." },
];

const KEY = "tally_live_...";

function Preview() {
  return (
    <div className="land-frame">
      <div className="land-sheet">
        <div className="land-sheet-head">
          <span className="land-sheet-title">Review</span>
          <span className="land-meal">breakfast</span>
        </div>
        <div className="land-said">
          <p className="land-bubble">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <rect x="9" y="3" width="6" height="11" rx="3" />
              <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
            </svg>
            two eggs and a fairlife
          </p>
        </div>
        <div className="land-rows">
          {PREVIEW_ROWS.map((r) => (
            <div className="land-row" key={r.name}>
              <div className="land-row-info">
                <div className="land-row-name">
                  {r.name}
                  <span className="land-badge">USDA</span>
                </div>
                <div className="land-row-sub">
                  {r.brand ? <span>{r.brand}</span> : null}
                  <span className="land-portion">{r.portion}</span>
                  <span className="land-p num">{r.protein} g protein</span>
                </div>
              </div>
              <div className="land-row-kcal num">
                <span className="n">{r.kcal}</span>
                <span className="u">kcal</span>
              </div>
            </div>
          ))}
        </div>
        <div className="land-sheet-foot">
          <div className="land-totals num">
            <span className="k">Total</span>
            <span>
              <strong className="land-p">{PREVIEW_PROTEIN} g</strong> protein
            </span>
            <span>
              <strong>{PREVIEW_KCAL}</strong> kcal
            </span>
          </div>
          {/* looks like the app's commit button; deliberately not a <button> */}
          <div className="land-commit">Log {PREVIEW_ROWS.length} items</div>
        </div>
      </div>

      <ol className="land-steps">
        {STEPS.map((s) => (
          <li key={s.k}>
            <span className="land-step-k">{s.k}</span>
            <p>{s.body}</p>
          </li>
        ))}
      </ol>
    </div>
  );
}

export default function Welcome() {
  return (
    <div className="land">
      <header className="land-nav">
        <div className="land-wrap land-nav-in">
          <Link href="/" className="land-mark">
            <TallyMark className="land-mark-icon" />
            <span>Tally</span>
          </Link>
          <nav className="land-nav-links" aria-label="Site">
            <Link href="/docs">Docs</Link>
            <a href={GITHUB_URL}>GitHub</a>
            <Link href="/auth/sign-in" className="land-pill">
              Sign in
            </Link>
          </nav>
        </div>
      </header>

      <main className="land-wrap land-main">
        <section className="land-hero">
          <p className="land-eyebrow">
            <span className="land-dot" aria-hidden="true" />
            Voice-first nutrition logging
          </p>
          <h1>
            Say what you ate.
            <span className="land-hero-accent"> That is the whole thing.</span>
          </h1>
          <p className="land-lede">
            Most food apps make you search, scroll, pick a serving and set a meal — for every
            item, every time. Tally does that once and then remembers.
          </p>
          <div className="land-cta">
            <Link href="/auth/sign-up" className="land-btn">
              Try a demo
            </Link>
            <Link href="/auth/sign-in" className="land-btn ghost">
              Sign in
            </Link>
            <Link href="/docs" className="land-textlink">
              Read the docs
            </Link>
          </div>
          <p className="land-fine">One log a day on a demo account. No card, no trial timer.</p>
        </section>

        <section className="land-preview" aria-labelledby="land-preview-label">
          <p className="land-eyebrow" id="land-preview-label">
            Preview · what one sentence becomes
          </p>
          <Preview />
        </section>

        <section className="land-claims" aria-label="Why Tally">
          {CLAIMS.map((c, i) => (
            <article key={c.title}>
              <span className="land-num" aria-hidden="true">
                0{i + 1}
              </span>
              <h2>{c.title}</h2>
              <p>{c.body}</p>
            </article>
          ))}
        </section>

        <section className="land-lanes" aria-labelledby="land-lanes-head">
          <p className="land-eyebrow">Four ways to log</p>
          <h2 className="land-h2" id="land-lanes-head">
            Four ways in, one list out
          </h2>
          <div className="land-lane-grid">
            {LANES.map(({ name, body, Icon }) => (
              <div className="land-lane" key={name}>
                <span className="land-lane-icon">
                  <Icon />
                </span>
                <h3>{name}</h3>
                <p>{body}</p>
              </div>
            ))}
          </div>
        </section>

        <section className="land-dev" aria-labelledby="land-dev-head">
          <div className="land-dev-copy">
            <p className="land-eyebrow">For developers</p>
            <h2 className="land-h2" id="land-dev-head">
              Your diary, from any agent
            </h2>
            <p>
              Create an API key in Settings and your diary is open to a REST API and an MCP
              server, so Claude, Cursor or an agent you wrote can read your day or log a meal
              for you. Each key is read-only or read and write, and you can revoke it at any
              time. Keys act as your account, so a demo account&apos;s one log a day still
              applies.
            </p>
            <div className="land-cta">
              <Link href="/docs" className="land-btn">
                Read the docs
              </Link>
            </div>
          </div>

          <div className="land-dev-side">
            <div className="land-term">
              <div className="land-term-bar" aria-hidden="true">
                <i />
                <i />
                <i />
                <span>terminal</span>
              </div>
              {/* focusable so a keyboard can scroll the long lines */}
              <pre className="land-code" tabIndex={0}>
                <code>
                  <span className="c"># Add Tally to Claude Code over MCP</span>
                  {"\n"}
                  <span className="pr">$ </span>claude mcp add --transport http tally{" "}
                  <span className="u">{SITE_URL}/api/mcp</span>{" "}
                  <span className="f">--header</span>{" "}
                  <span className="s">&quot;Authorization: Bearer {KEY}&quot;</span>
                  {"\n\n"}
                  <span className="c"># Or call the REST API</span>
                  {"\n"}
                  <span className="pr">$ </span>curl <span className="u">{SITE_URL}/api/diary</span>{" "}
                  <span className="f">-H</span>{" "}
                  <span className="s">&quot;Authorization: Bearer {KEY}&quot;</span>
                </code>
              </pre>
            </div>

            <div className="land-tools">
              <div className="land-tools-head">
                <span className="land-eyebrow">A few of the tools</span>
                <Link href="/docs/mcp" className="land-textlink">
                  All tools
                </Link>
              </div>
              <ul>
                {TOOLS.map((t) => (
                  <li key={t.name}>
                    <code>{t.name}</code>
                    <span className={`land-scope ${t.scope}`}>{t.scope}</span>
                    <p>{t.body}</p>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </section>

        <section className="land-open" aria-labelledby="land-open-head">
          <div>
            <p className="land-eyebrow">MIT licensed</p>
            <h2 className="land-h2" id="land-open-head">
              Open source
            </h2>
            <p>
              Tally is MIT-licensed and built to be run by one person on free tiers. The design
              document, the phased build plan and the ledger of every review finding are all in
              the repo.
            </p>
          </div>
          <div className="land-cta">
            <a className="land-btn ghost" href={GITHUB_URL}>
              Read the source
            </a>
            <Link href="/auth/sign-up" className="land-btn">
              Try a demo
            </Link>
          </div>
        </section>
      </main>

      <footer className="land-foot">
        <div className="land-wrap land-foot-in">
          <Link href="/" className="land-mark small">
            <TallyMark className="land-mark-icon" />
            <span>Tally</span>
          </Link>
          <nav className="land-foot-links" aria-label="Footer">
            <Link href="/docs" className="land-web">
              Docs
            </Link>
            <Link href="/privacy">Privacy</Link>
            <Link href="/terms">Terms</Link>
            <a href={GITHUB_URL} className="land-web">
              GitHub
            </a>
          </nav>
          <p className="land-foot-credit">
            Nutrition data from <a href="https://fdc.nal.usda.gov/">USDA FoodData Central</a>
          </p>
        </div>
      </footer>
    </div>
  );
}
