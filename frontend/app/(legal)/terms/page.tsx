import type { Metadata } from "next";

/** Public, like /privacy — proxy.ts excludes it from the signed-out redirect.
 *  The two clauses that actually matter for a nutrition app are “not medical
 *  advice” and the accuracy disclaimer; keep them near the top. */

export const metadata: Metadata = {
  title: "Terms",
  description: "The terms for using Tally, including what it is not.",
};

export default function Terms() {
  return (
    <>
      <div className="legal-head">
        <h1>Terms of Service</h1>
        <p className="legal-stamp">Last updated 20 September 2026</p>
      </div>

      <div className="legal-body">
        <section>
          <h2>The short version</h2>
          <p>
            Tally is a free, open-source nutrition logger run as a personal project by Bill
            Zhang. Using it means accepting what is below. If you do not, do not use it — or
            fork it and run your own, which the licence explicitly allows.
          </p>
        </section>

        <section>
          <h2>This is not medical advice</h2>
          <p>
            Tally is a logging tool. Calorie targets, macro splits and anything else it
            calculates are estimates produced by standard formulas from numbers you typed in.
            They are <strong>not</strong> medical, nutritional or clinical advice, and nothing
            here is a substitute for a doctor or a registered dietitian.
          </p>
          <p>
            Do not use Tally to manage a medical condition, and talk to a qualified professional
            before making significant changes to how you eat — particularly if you are pregnant,
            have an eating disorder or a history of one, or have diabetes, kidney disease or any
            other condition affected by diet.
          </p>
        </section>

        <section>
          <h2>The numbers can be wrong</h2>
          <p>
            Nutrition values come from public databases — USDA FoodData Central, Open Food Facts,
            and nutrition pages published by restaurant chains. Those sources contain errors, go
            out of date, and often describe a food that is close to yours rather than yours.
          </p>
          <p>
            Portions estimated from a photo are guesses by a model. They are flagged as estimates
            in the app and have to be confirmed before they commit, and you should treat them as
            a starting point rather than a measurement. Check anything you intend to rely on.
          </p>
        </section>

        <section>
          <h2>Your account</h2>
          <p>
            Keep your password to yourself; you are responsible for what happens under your
            account. Sign up with an address you actually control, since that address is how a
            deletion or export request is verified.
          </p>
          <p>
            Demo accounts exist so people can try the app. They are capped at one log a day, with
            a small daily allowance for re-running a model on an edited row, and both reset at
            midnight UTC. Working around those caps, including by making extra accounts, is not
            allowed.
          </p>
        </section>

        <section>
          <h2>Using it reasonably</h2>
          <p>Do not:</p>
          <ul>
            <li>try to reach another account’s data, or probe the app for ways to;</li>
            <li>automate bulk requests, scrape the service, or resell access to it;</li>
            <li>upload content you have no right to upload;</li>
            <li>use it to break a law, or to deliberately break the service for other people.</li>
          </ul>
          <p>
            Accounts doing any of the above can be suspended or deleted without notice.
          </p>
        </section>

        <section>
          <h2>No promises about uptime</h2>
          <p>
            Tally runs on free tiers and is maintained in one person’s spare time. It may be
            slow, unavailable, changed, or shut down entirely, at any time and without notice.
            Keep your own copy of anything you cannot afford to lose.
          </p>
        </section>

        <section>
          <h2>No warranty, and the limit of liability</h2>
          <p>
            Tally is provided “as is”, without warranty of any kind, express or implied,
            including fitness for a particular purpose. To the maximum extent the law allows,
            the operator is not liable for any indirect, incidental or consequential damages, or
            for lost data or lost profits, arising from your use of it.
          </p>
        </section>

        <section>
          <h2>Open source</h2>
          <p>
            The source is{" "}
            <a href="https://github.com/IdkwhatImD0ing/nutritiontracker">MIT-licensed</a>. Those
            terms govern the code. These terms govern the hosted service at this domain. The
            nutrition data is not the operator’s to license: USDA FoodData Central is public
            domain, and chain-restaurant values are transcribed from each chain’s own published
            page and stored with a link back to it.
          </p>
        </section>

        <section>
          <h2>Governing law and changes</h2>
          <p>
            These terms are governed by the laws of the State of California, USA. If they change,
            the date at the top changes with them, and the full history is public in the
            repository. Questions go to{" "}
            <a href="mailto:billzhangsc@gmail.com">billzhangsc@gmail.com</a>.
          </p>
        </section>
      </div>
    </>
  );
}
