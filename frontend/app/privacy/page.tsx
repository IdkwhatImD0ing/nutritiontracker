import type { Metadata } from "next";
import Link from "next/link";
import "../legal.css";

/** Public, and it has to stay that way: proxy.ts excludes /privacy from the
 *  signed-out redirect so an unfurler, a reviewer or an OAuth provider can
 *  actually read it. Keep this page truthful about the processors in §3 —
 *  it is a description of the system, not boilerplate. */

export const metadata: Metadata = {
  title: "Privacy",
  description:
    "What Tally stores, where it is processed, and how to get it deleted.",
};

export default function Privacy() {
  return (
    <main className="legal">
      <header className="legal-nav">
        <Link href="/" className="legal-mark">
          Tally
        </Link>
        <nav>
          <Link href="/terms">Terms</Link>
        </nav>
      </header>

      <div className="legal-head">
        <h1>Privacy</h1>
        <p className="legal-stamp">Last updated 20 September 2026</p>
      </div>

      <div className="legal-body">
        <section>
          <h2>Who runs this</h2>
          <p>
            Tally is a personal, open-source project run by one person, Bill Zhang. It is not
            a company. Questions, or a deletion request, go to{" "}
            <a href="mailto:billzhangsc@gmail.com">billzhangsc@gmail.com</a>.
          </p>
        </section>

        <section>
          <h2>What is stored</h2>
          <ul>
            <li>
              <strong>Your account</strong> — the email address you sign up with, and a hashed
              password. Passwords are never stored in a readable form.
            </li>
            <li>
              <strong>Your diary</strong> — the foods you log, portions, meals, timestamps and
              the macro numbers captured at the moment you commit them.
            </li>
            <li>
              <strong>What you said</strong> — the raw text or voice transcript of every
              utterance you submit, kept so the app can learn your phrasing and so mistakes can
              be traced. Photos you submit are processed for recognition.
            </li>
            <li>
              <strong>What it learned</strong> — the mapping from your phrases to specific
              foods, and the portion you usually take.
            </li>
            <li>
              <strong>Weigh-ins and profile</strong> — any weight you log, plus the height, age,
              sex, activity level and goals you enter to generate targets.
            </li>
          </ul>
        </section>

        <section>
          <h2>Where it is processed</h2>
          <p>
            Tally is a small app standing on other people’s services. These are all of them:
          </p>
          <ul>
            <li>
              <strong>Neon</strong> — the Postgres database that holds everything above, and the
              managed authentication that issues your session.
            </li>
            <li>
              <strong>Vercel</strong> — hosting for the web app and the API.
            </li>
            <li>
              <strong>Deepgram</strong> — converts your voice recordings to text. OpenAI’s
              transcription is the fallback when it fails.
            </li>
            <li>
              <strong>OpenAI</strong> — splits your sentence into food items, reads photos you
              submit, and runs the agent that searches for a food it has not seen before.
            </li>
            <li>
              <strong>USDA FoodData Central</strong> and <strong>Open Food Facts</strong> — the
              food databases. A lookup sends the name or barcode of a food, never your diary.
            </li>
          </ul>
          <p>
            Application traces are kept on a server run by the same person who runs Tally, not a
            third-party analytics product. There is no advertising, no tracking pixels and no
            analytics SDK in the app.
          </p>
        </section>

        <section>
          <h2>Who can see your rows</h2>
          <p>
            Accounts are separated inside the database itself, by Postgres row-level security
            rather than by application code — the database role the app runs as cannot read
            another account’s rows even if the app asks it to. The one shared table is the
            cache of public food records, which holds nutrition data and nothing personal.
          </p>
          <p>
            Your data is not sold, rented, or shared with anyone beyond the services listed
            above.
          </p>
        </section>

        <section>
          <h2>Demo accounts</h2>
          <p>
            Anyone can sign up for a demo account, capped at one log a day. A demo is for trying
            the app, not for keeping a diary, and its rows are stored the same way and deleted
            on the same request.
          </p>
        </section>

        <section>
          <h2>Cookies</h2>
          <p>
            One session cookie, set when you sign in, so the app knows it is you. Nothing else.
          </p>
        </section>

        <section>
          <h2>Getting your data out, or deleted</h2>
          <p>
            There is no self-serve export or delete button yet. Email{" "}
            <a href="mailto:billzhangsc@gmail.com">billzhangsc@gmail.com</a> from the address on
            the account and everything tied to it will be exported or removed. Because Tally is
            MIT-licensed, the other option is to run your own copy and keep the data on
            infrastructure you control.
          </p>
        </section>

        <section>
          <h2>Changes</h2>
          <p>
            If this policy changes, the date at the top changes with it. The full history of
            this page is public in the{" "}
            <a href="https://github.com/IdkwhatImD0ing/nutritiontracker">repository</a>.
          </p>
        </section>
      </div>

      <footer className="legal-foot">
        <span>Tally</span>
        <span>
          <Link href="/terms">Terms of Service</Link>
        </span>
      </footer>
    </main>
  );
}
