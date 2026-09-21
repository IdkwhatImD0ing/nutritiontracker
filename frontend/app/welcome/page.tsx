import type { Metadata } from "next";
import Link from "next/link";
import "./landing.css";

/** The public front door. `proxy.ts` sends every signed-out visitor here
 *  instead of straight to the sign-in form, so someone arriving from the
 *  repo or a link learns what Tally is before being asked for an email.
 *  Signed-in visitors never see it — `/` renders Today for them. */

export const metadata: Metadata = {
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
    body: "Vision names what is on the plate and estimates the amounts. Every estimate is flagged and has to be confirmed.",
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

export default function Welcome() {
  return (
    <main className="land">
      <header className="land-nav">
        <span className="land-mark">Tally</span>
        <nav className="land-nav-links">
          <a href="https://github.com/IdkwhatImD0ing/nutritiontracker">Source</a>
          <Link href="/auth/sign-in">Sign in</Link>
        </nav>
      </header>

      <section className="land-hero">
        <h1>
          Say what you ate.
          <span className="land-hero-dim"> That is the whole thing.</span>
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
        </div>
        <p className="land-fine">One log a day on a demo account. No card, no trial timer.</p>
      </section>

      <section className="land-demo" aria-label="What a log looks like">
        <div className="land-said">
          <span className="land-said-label">You say</span>
          <p>“two eggs and a fairlife”</p>
        </div>
        <div className="land-rows">
          <div className="land-row">
            <div className="land-row-main">
              <span className="land-row-name">Eggs, scrambled</span>
              <span className="land-row-sub">
                2 large · <span className="land-p">12 g protein</span>
              </span>
            </div>
            <span className="land-row-kcal num">156</span>
          </div>
          <div className="land-row">
            <div className="land-row-main">
              <span className="land-row-name">Fairlife 2% milk</span>
              <span className="land-row-sub">
                1 bottle · <span className="land-p">13 g protein</span>
              </span>
            </div>
            <span className="land-row-kcal num">120</span>
          </div>
        </div>
        <p className="land-demo-note">
          Numbers from USDA FoodData Central — the food, the portion and the meal already
          chosen. One tap files it.
        </p>
      </section>

      <section className="land-claims">
        <article>
          <h2>It gets faster every week</h2>
          <p>
            Every commit teaches it the phrases you use, the foods they mean and the portions
            you take. A meal you have logged before resolves from your own history with{" "}
            <strong>no AI call at all</strong> — one database round-trip. Only a food you have
            never logged touches the network, once.
          </p>
        </article>
        <article>
          <h2>It does not make numbers up</h2>
          <p>
            Text and voice never estimate. If a food cannot be found, the app says so instead
            of guessing. The one exception is a photo, where amounts have to be estimated —
            and there every estimate is flagged and refused at commit until you confirm it.
          </p>
        </article>
        <article>
          <h2>Your diary is yours</h2>
          <p>
            Accounts are kept apart by Postgres row-level security, not by application code —
            the app runs as a database role that cannot bypass it. Photos are never stored.
          </p>
        </article>
      </section>

      <section className="land-lanes" aria-label="Four ways to log">
        <h2 className="land-lanes-head">Four ways in, one list out</h2>
        <div className="land-lane-grid">
          {LANES.map(({ name, body, Icon }) => (
            <div className="land-lane" key={name}>
              <Icon />
              <h3>{name}</h3>
              <p>{body}</p>
            </div>
          ))}
        </div>
      </section>

      <section className="land-open">
        <h2>Open source</h2>
        <p>
          Tally is MIT-licensed and built to be run by one person on free tiers. The design
          document, the phased build plan and the ledger of every review finding are all in
          the repo.
        </p>
        <div className="land-cta">
          <a
            className="land-btn ghost"
            href="https://github.com/IdkwhatImD0ing/nutritiontracker"
          >
            Read the source
          </a>
          <Link href="/auth/sign-up" className="land-btn">
            Try a demo
          </Link>
        </div>
      </section>

      <footer className="land-foot">
        <span>Tally</span>
        <span>
          Nutrition data from{" "}
          <a href="https://fdc.nal.usda.gov/">USDA FoodData Central</a>
        </span>
        <span className="land-foot-legal">
          <Link href="/privacy">Privacy</Link>
          <Link href="/terms">Terms</Link>
        </span>
      </footer>
    </main>
  );
}
