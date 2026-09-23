import type { Metadata } from "next";
import Link from "next/link";
import { CodeBlock } from "@/components/docs/CodeBlock";
import { DocsPage, Endpoint } from "@/components/docs/DocsPage";
import { COMMIT_RESPONSE, DIARY_RESPONSE, LOG_RESPONSE } from "@/components/docs/snippets";
import { SITE_URL } from "@/lib/site";

/** A curated map of the FastAPI routes under backend/routes/, grouped the
 *  way a caller thinks about them. Field names come from the route modules'
 *  pydantic models; the full, generated contract is /api/openapi.json. Keep
 *  this page in step when a route's shape changes. */

export const metadata: Metadata = {
  title: "REST API",
  description:
    "Tally's REST endpoints by area: diary and history, logging, entries, weights, foods, saved meals, settings.",
};

const TOC = [
  { id: "conventions", label: "Conventions" },
  { id: "diary", label: "Diary & history" },
  { id: "logging", label: "Logging" },
  { id: "entries", label: "Entries" },
  { id: "weights", label: "Weights" },
  { id: "foods", label: "Foods" },
  { id: "meals", label: "Saved meals" },
  { id: "settings", label: "Settings" },
  { id: "me", label: "Me" },
];

const LOG_REQUEST = `{
  "id": "7c0e5a0e-2f4b-4f7e-9d0a-5b8f1f3c2a91",
  "text": "two eggs and a fairlife",
  "captured_at": "2026-09-23T08:15:00-07:00",
  "tz": "America/Los_Angeles"
}`;

const WEIGHT_REQUEST = `{ "weight": 180.4, "unit": "lb" }`;

const WEIGHT_RESPONSE = `{
  "day": "2026-09-23",
  "weight_kg": 81.83,
  "unit": "lb",
  "source": "manual",
  "log_id": null
}`;

export default function RestDoc() {
  return (
    <DocsPage href="/docs/rest" toc={TOC}>
      <h1>REST API</h1>
      <p className="docs-lede">
        The endpoints the Tally app itself uses, grouped by what they are for. Each row shows the
        method, the path and the scope a key needs.
      </p>
      <p>
        This page covers the routes you are likely to call. The machine-readable contract for
        every route, generated from the backend&apos;s models, is{" "}
        <a href="/api/openapi.json">
          <code>/api/openapi.json</code>
        </a>{" "}
        (OpenAPI 3.1). It also lists a few routes the app uses internally that are not covered here:
        the streaming chat, photo upload, aliases and telemetry.
      </p>

      <h2 id="conventions">Conventions</h2>
      <ul>
        <li>
          Base URL <code>{SITE_URL}</code>; every path starts with <code>/api</code>. JSON in,
          JSON out.
        </li>
        <li>
          Auth: <code>Authorization: Bearer tally_live_…</code> on every request.{" "}
          <code>read</code> covers GET; everything else needs <code>write</code> (
          <Link href="/docs/api-keys#scopes">scopes</Link>).
        </li>
        <li>
          Days are your local days, in the timezone from your settings (UTC if unset). Dates are{" "}
          <code>YYYY-MM-DD</code>, timestamps ISO 8601, ids UUIDs. Only log creation takes a{" "}
          <code>tz</code>; every read uses the saved timezone.
        </li>
        <li>
          Macros are <code>kcal</code>, <code>protein_g</code>, <code>carb_g</code>,{" "}
          <code>fat_g</code>, and <code>fiber_g</code> when the source reports it. A{" "}
          <code>macros</code> object may carry other nutrient keys, always numbers.
        </li>
        <li>
          Another account&apos;s ids read as <code>404</code>, exactly like ids that do not exist.
          Errors are JSON with a <code>detail</code> field (
          <Link href="/docs/limits#errors">error shapes</Link>).
        </li>
      </ul>

      <h2 id="diary">Diary &amp; history</h2>
      <ul className="docs-eps">
        <Endpoint method="GET" path="/api/diary?date=YYYY-MM-DD" scope="read">
          <p>
            One day&apos;s entries grouped under <code>breakfast</code>, <code>lunch</code>,{" "}
            <code>dinner</code> and <code>snack</code> (always all four), plus{" "}
            <code>totals</code> against your <code>targets</code>. No <code>date</code> means
            today.
          </p>
        </Endpoint>
        <Endpoint method="GET" path="/api/history?range=3d|w|m|y" scope="read">
          <p>
            Totals over a window ending today: the last 3, 7 or 30 days, or 52 weeks (default{" "}
            <code>w</code>, the last 7 days). For <code>3d</code>, <code>w</code> and{" "}
            <code>m</code>, <code>days</code> has one row per day (an unlogged day is zeros with{" "}
            <code>logged: false</code>); for <code>y</code>, <code>weeks</code> has weekly
            averages over the logged days. Also <code>targets</code> and your logging{" "}
            <code>streak</code>. There is no custom start date; use the diary for single days.
          </p>
        </Endpoint>
      </ul>
      <CodeBlock code={DIARY_RESPONSE} lang="json" title="200 OK · GET /api/diary" />

      <h2 id="logging">Logging</h2>
      <p>
        Logging is two steps. <strong>Stage</strong>: send what you ate in words; Tally names the
        foods, finds portions and the meal, looks the foods up and stores the rows as a staged
        log. <strong>Commit</strong>: send back the rows to keep and they go into the diary.
        Nothing reaches the diary before the commit, and a staged log you do not want can be
        discarded.
      </p>
      <ul className="docs-eps">
        <Endpoint method="POST" path="/api/logs" scope="write">
          <p>
            Stage a log from text. Body: <code>id</code> (a UUID you generate: resending the same
            id returns the stored result instead of logging twice), <code>text</code>,{" "}
            <code>captured_at</code> (ISO timestamp), <code>tz</code> (IANA name). Optional{" "}
            <code>history</code> carries earlier chat turns so a follow-up can refer back. The
            call waits for the lookups, up to about two minutes. On a demo account it spends the
            day&apos;s log.
          </p>
          <p>
            Returns <code>log_id</code>, <code>meal</code>, <code>items</code> (the staged rows),{" "}
            <code>logged_at</code> (set when words like &ldquo;yesterday&rdquo; moved the day),{" "}
            <code>saved_meal</code>, <code>note</code>, and <code>agent</code> with the
            agent&apos;s <code>reply</code>, any <code>options</code> it offers, and a{" "}
            <code>weight</code> if you mentioned one.
          </p>
        </Endpoint>
        <Endpoint method="GET" path="/api/logs/{log_id}" scope="read">
          <p>
            The log&apos;s rows as they stand now. A row can carry <code>stale: true</code> when
            its lookup stalled; retry it with <code>/api/resolve/tier3</code>.
          </p>
        </Endpoint>
        <Endpoint method="POST" path="/api/logs/{log_id}/commit" scope="write">
          <p>
            Body: <code>items</code>, the rows to keep, in the staged shape (edit{" "}
            <code>food</code>, <code>serving</code>, <code>qty</code> or <code>meal</code> first
            if you like), and optional <code>save_intent: &#123;name&#125;</code> to also save them
            as a saved meal. Only <code>resolved</code> rows are written; others you send stay on
            the log; rows you leave out are removed. Macros are recomputed from the food database,
            never taken from your payload. Returns the day&apos;s totals, per-row{" "}
            <code>errors</code>, <code>saved_meal</code> and <code>totals_day</code> (null when
            the rows landed on more than one day). Committing a log that is already committed
            writes nothing and answers 200 again, so a retry is safe.
          </p>
        </Endpoint>
        <Endpoint method="POST" path="/api/logs/{log_id}/commit-item" scope="write">
          <p>
            Body: <code>item_index</code>, <code>item</code>. Adds one row that resolved after the
            log was committed. The log must already be committed; returns the day&apos;s totals.
          </p>
        </Endpoint>
        <Endpoint method="DELETE" path="/api/logs/{log_id}" scope="write">
          <p>Discard a staged log. Discarding twice is harmless; a committed log is 409.</p>
        </Endpoint>
        <Endpoint method="DELETE" path="/api/logs/{log_id}/items/{item_index}" scope="write">
          <p>Remove one staged row before commit. A row already committed is 409.</p>
        </Endpoint>
        <Endpoint method="POST" path="/api/logs/from-food" scope="write">
          <p>
            Stage exactly one row from a food you already have an id for, with no model involved.
            Body: <code>id</code>, <code>food_id</code>, <code>input_mode</code> (
            <code>&quot;search&quot;</code> or <code>&quot;barcode&quot;</code>),{" "}
            <code>captured_at</code>, and optional <code>serving_id</code>, <code>qty</code>,{" "}
            <code>meal_hint</code>. Counts as the day&apos;s log on a demo account.
          </p>
        </Endpoint>
      </ul>
      <h3 id="fix-a-row">Fix a staged row</h3>
      <ul className="docs-eps">
        <Endpoint method="POST" path="/api/resolve" scope="write">
          <p>
            Body: <code>log_id</code>, <code>item_index</code>, <code>phrase</code>, optional{" "}
            <code>qty</code> and <code>unit</code>. Re-resolves a row from new words, or adds one
            at a new index. Returns the row.
          </p>
        </Endpoint>
        <Endpoint method="POST" path="/api/resolve/answer" scope="write">
          <p>
            Body: <code>log_id</code>, <code>item_index</code>, <code>answer</code>. Answers a
            row&apos;s <code>question</code> with one of its options, an alternate&apos;s{" "}
            <code>food_id</code>, or free <code>text</code>.
          </p>
        </Endpoint>
        <Endpoint method="POST" path="/api/resolve/tier3" scope="write">
          <p>
            Body: <code>log_id</code>, <code>item_index</code>. Runs the lookup again for a row
            still <code>searching</code>.
          </p>
        </Endpoint>
      </ul>
      <p>
        <code>/api/resolve</code>, <code>/api/resolve/tier3</code> and a free-text answer can
        each run a fresh lookup, so give them a long timeout (
        <Link href="/docs/limits#time">request time</Link>). On a demo account they share a
        budget of 10 a day (<Link href="/docs/limits#demo">limits</Link>).
      </p>
      <CodeBlock code={LOG_REQUEST} lang="json" title="POST /api/logs · request" />
      <CodeBlock code={LOG_RESPONSE} lang="json" title="200 OK · POST /api/logs (trimmed)" />
      <CodeBlock
        code={COMMIT_RESPONSE}
        lang="json"
        title="200 OK · POST /api/logs/{log_id}/commit"
      />
      <p>
        Each staged row has a <code>status</code>: <code>resolved</code> (ready to commit),{" "}
        <code>searching</code> (lookup not finished), <code>needs_input</code> (see its{" "}
        <code>question</code>) or <code>not_found</code>. Tally never makes up numbers for a food
        it cannot find: it says <code>not_found</code>.
      </p>

      <h2 id="entries">Entries</h2>
      <p>Committed diary rows. Their ids are the <code>id</code> fields in the diary response.</p>
      <ul className="docs-eps">
        <Endpoint method="PATCH" path="/api/entries/{entry_id}" scope="write">
          <p>
            Body: any of <code>qty</code>, <code>serving_id</code>, <code>food_id</code>. Returns{" "}
            <code>&#123;entry, totals&#125;</code>. An entry&apos;s meal and time cannot be
            changed. <code>422</code> when nothing changes or <code>qty</code> is not positive.
          </p>
        </Endpoint>
        <Endpoint method="DELETE" path="/api/entries/{entry_id}" scope="write">
          <p>Deletes the entry for good and returns the day&apos;s new totals.</p>
        </Endpoint>
      </ul>

      <h2 id="weights">Weights</h2>
      <ul className="docs-eps">
        <Endpoint method="GET" path="/api/weights?days=90" scope="read">
          <p>
            <code>latest</code> weigh-in plus <code>entries</code> from the last{" "}
            <code>days</code> days (1 to 3660, default 90), newest first.
          </p>
        </Endpoint>
        <Endpoint method="PUT" path="/api/weights" scope="write">
          <p>
            Body: <code>weight</code>, <code>unit</code> (<code>&quot;kg&quot;</code> or{" "}
            <code>&quot;lb&quot;</code>), optional <code>day</code> (default today). One weigh-in
            per day: a second one replaces the first. Stored in kg; 30 to 300 kg is accepted.
          </p>
        </Endpoint>
      </ul>
      <CodeBlock code={WEIGHT_REQUEST} lang="json" title="PUT /api/weights · request" />
      <CodeBlock code={WEIGHT_RESPONSE} lang="json" title="200 OK · PUT /api/weights" />

      <h2 id="foods">Foods</h2>
      <ul className="docs-eps">
        <Endpoint method="GET" path="/api/foods/search?q=&source=all|local|fdc" scope="read">
          <p>
            Returns <code>local</code> hits from Tally&apos;s food cache (shared by all accounts,
            up to 10) and, unless <code>source=local</code>, <code>generic</code> and{" "}
            <code>branded</code> hits from USDA FoodData Central. If USDA fails,{" "}
            <code>fdc_error</code> says so and local hits still come back.
          </p>
        </Endpoint>
        <Endpoint method="GET" path="/api/foods/recent" scope="read">
          <p>Foods you have logged, newest first, up to 200.</p>
        </Endpoint>
        <Endpoint method="GET" path="/api/foods/{food_id}" scope="read">
          <p>
            One food: its <code>servings</code> (with macros per serving), every nutrient the
            source reported, a source badge, and the raw provider <code>payload</code>.
          </p>
        </Endpoint>
        <Endpoint method="GET" path="/api/foods/barcode?code=" scope="read">
          <p>
            Look a barcode up: the cache first, then USDA, then Open Food Facts. A miss is{" "}
            <code>&#123;&quot;found&quot;: false&#125;</code>, not a 404. A new hit is added to the
            shared food cache.
          </p>
        </Endpoint>
        <Endpoint method="POST" path="/api/foods/select" scope="write">
          <p>
            Body: <code>source</code> (<code>&quot;fdc&quot;</code>), <code>source_id</code>, and
            optionally <code>log_id</code> + <code>item_index</code>. Adds a USDA search hit to
            the cache and returns its <code>food_id</code>; with a log row given, also sets that
            row to the food.
          </p>
        </Endpoint>
      </ul>

      <h2 id="meals">Saved meals</h2>
      <p>
        A saved meal is a name, trigger phrases and a fixed list of foods. Saying a trigger phrase
        in a log stages the whole list.
      </p>
      <ul className="docs-eps">
        <Endpoint method="GET" path="/api/meals?include_inactive=false" scope="read">
          <p>Your saved meals; deleted ones too with <code>include_inactive=true</code>.</p>
        </Endpoint>
        <Endpoint method="POST" path="/api/meals" scope="write">
          <p>
            Body: <code>name</code>, <code>trigger_phrases</code> (default: the name),{" "}
            <code>items</code> (<code>food_id</code>, <code>serving_id</code>, <code>qty</code>).
            Answers <code>201</code>; a second active meal with the same name is <code>409</code>.
          </p>
        </Endpoint>
        <Endpoint method="PATCH" path="/api/meals/{meal_id}" scope="write">
          <p>
            Any of <code>name</code>, <code>trigger_phrases</code>, <code>items</code>,{" "}
            <code>is_active</code>. Lists you send replace the stored ones.
          </p>
        </Endpoint>
        <Endpoint method="DELETE" path="/api/meals/{meal_id}" scope="write">
          <p>Deactivates the meal. The row is kept, so past diary entries keep their context.</p>
        </Endpoint>
        <Endpoint method="POST" path="/api/meals/from-log" scope="write">
          <p>
            Body: <code>log_id</code>, <code>name</code>, optional <code>trigger_phrases</code>.
            Saves a committed log&apos;s entries as a meal, or updates the meal of that name.
          </p>
        </Endpoint>
      </ul>

      <h2 id="settings">Settings</h2>
      <ul className="docs-eps">
        <Endpoint method="GET" path="/api/settings" scope="read">
          <p>
            <code>body</code> (height, weight, age and the rest of the profile),{" "}
            <code>targets</code> (<code>null</code> until goals are set), <code>meal_windows</code>
            , <code>profile_notes</code>, <code>timezone</code>.
          </p>
        </Endpoint>
        <Endpoint method="PUT" path="/api/settings" scope="write">
          <p>
            Partial update: only the fields you send are written. <code>timezone</code> must be an
            IANA name; <code>targets.kcal</code> must be 800 to 10000.
          </p>
        </Endpoint>
        <Endpoint method="POST" path="/api/settings/goals/compute" scope="write">
          <p>
            Proposes targets from height, weight, age, sex and activity level. Saves nothing; PUT
            the <code>targets</code> you want. It is a POST, so it needs a write key.
          </p>
        </Endpoint>
      </ul>

      <h2 id="me">Me</h2>
      <ul className="docs-eps">
        <Endpoint method="GET" path="/api/me" scope="read">
          <p>
            <code>email</code>, <code>is_owner</code>, and <code>demo</code>:{" "}
            <code>&#123;limit, used_today, resets_at&#125;</code> for a demo account,{" "}
            <code>null</code> for the owner.
          </p>
        </Endpoint>
        <Endpoint method="GET" path="/api/health" scope="public">
          <p>
            <code>&#123;&quot;ok&quot;: true, &quot;service&quot;: &quot;tally&quot;&#125;</code>.
            Needs no key, like <code>/api/openapi.json</code>.
          </p>
        </Endpoint>
      </ul>
      <p>
        Managing API keys (<code>/api/keys</code>) needs a signed-in app session:{" "}
        <Link href="/docs/api-keys#manage">API keys</Link>.
      </p>
    </DocsPage>
  );
}
