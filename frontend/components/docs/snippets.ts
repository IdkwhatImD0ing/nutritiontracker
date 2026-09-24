import { SITE_URL } from "@/lib/site";

/** Code samples the docs pages print, built from SITE_URL so a fork's docs
 *  show its own domain. Plain strings: CodeBlock and CodeTabs render them
 *  verbatim, and Copy copies exactly what is on screen. Example payloads are
 *  shaped exactly like the backend's response models (routes/diary.py,
 *  routes/logs.py, routes/commit.py); the values in them are illustrative. */

export const MCP_URL = `${SITE_URL}/api/mcp`;
const KEY = "tally_live_...";

// ---------------------------------------------------------------------------
// MCP client setup
// ---------------------------------------------------------------------------

export const CLAUDE_CODE_ADD = `claude mcp add --transport http tally ${MCP_URL} --header "Authorization: Bearer ${KEY}"`;

export const MCP_JSON = JSON.stringify(
  {
    mcpServers: {
      tally: { type: "http", url: MCP_URL, headers: { Authorization: `Bearer ${KEY}` } },
    },
  },
  null,
  2,
);

// For clients that can only launch a local (stdio) process, such as Claude
// Desktop's config file: mcp-remote is that process and forwards to the
// endpoint. The header has no space and lives in env because some clients
// split args on spaces (mcp-remote's README gives this workaround).
export const MCP_REMOTE_JSON = JSON.stringify(
  {
    mcpServers: {
      tally: {
        command: "npx",
        args: ["-y", "mcp-remote", MCP_URL, "--header", "Authorization:${TALLY_AUTH}"],
        env: { TALLY_AUTH: `Bearer ${KEY}` },
      },
    },
  },
  null,
  2,
);

// Checked against @modelcontextprotocol/sdk 1.30 (Client, StreamableHTTPClientTransport
// with requestInit, the 60 s DEFAULT_REQUEST_TIMEOUT_MSEC).
export const MCP_TS = `import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const transport = new StreamableHTTPClientTransport(new URL("${MCP_URL}"), {
  requestInit: { headers: { Authorization: \`Bearer \${process.env.TALLY_API_KEY}\` } },
});
const client = new Client({ name: "my-agent", version: "1.0.0" });
await client.connect(transport);

const { tools } = await client.listTools();
console.log(tools.map((t) => t.name));

const today = await client.callTool({ name: "get_day", arguments: {} });
console.log(today.content);

// log_food can take up to about two minutes and the SDK gives up after 60 s:
// await client.callTool({ name: "log_food", arguments: { text: "two eggs" } }, undefined, { timeout: 120_000 });

await client.close();
`;

// Checked against the Python SDK 2.x client (mcp.client.Client accepts a
// transport; streamable_http_client takes an httpx2 client for headers).
export const MCP_PY = `# pip install "mcp>=2.2,<3"
import asyncio
import os

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

URL = "${MCP_URL}"
HEADERS = {"Authorization": f"Bearer {os.environ['TALLY_API_KEY']}"}


async def main() -> None:
    # log_food can take up to about two minutes, so allow long reads
    timeout = httpx2.Timeout(30, read=150)
    async with httpx2.AsyncClient(headers=HEADERS, timeout=timeout) as http:
        async with Client(streamable_http_client(URL, http_client=http)) as client:
            listed = await client.list_tools()
            print([t.name for t in listed.tools])
            today = await client.call_tool("get_day", {})
            print(today.content)


asyncio.run(main())
`;

// ---------------------------------------------------------------------------
// REST: read today's diary
// ---------------------------------------------------------------------------

export const DIARY_CURL = `curl ${SITE_URL}/api/diary -H "Authorization: Bearer $TALLY_API_KEY"`;

export const DIARY_PY = `import os

import httpx

res = httpx.get(
    "${SITE_URL}/api/diary",
    headers={"Authorization": f"Bearer {os.environ['TALLY_API_KEY']}"},
)
res.raise_for_status()
day = res.json()
print(day["totals"]["kcal"], "of", day["totals"]["targets"]["kcal"], "kcal")
`;

export const DIARY_FETCH = `const res = await fetch("${SITE_URL}/api/diary", {
  headers: { Authorization: \`Bearer \${process.env.TALLY_API_KEY}\` },
});
if (!res.ok) throw new Error(\`\${res.status} \${JSON.stringify(await res.json())}\`);
const day = await res.json();
console.log(day.totals.kcal, "of", day.totals.targets.kcal, "kcal");
`;

// DiaryResponse (routes/diary.py): every meal key is always present.
export const DIARY_RESPONSE = `{
  "date": "2026-09-23",
  "meals": {
    "breakfast": [
      {
        "id": "0f6b2c8e-4d1a-4c3b-9e7f-2a5d8c1b6e40",
        "meal": "breakfast",
        "food": {
          "id": "2d4f8a61-9c3e-4b7a-8f10-6e2b5c9d1a37",
          "name": "Egg, whole, cooked, hard-boiled",
          "brand": null
        },
        "serving": { "id": "93437", "description": "1 large" },
        "qty": 2.0,
        "macros": { "kcal": 155.0, "protein_g": 12.58, "carb_g": 1.12, "fat_g": 10.61, "fiber_g": 0.0 },
        "logged_at": "2026-09-23T15:15:00Z",
        "grams": 100.0,
        "source": "fdc",
        "web_source": null,
        "estimated": "none"
      }
    ],
    "lunch": [],
    "dinner": [],
    "snack": []
  },
  "totals": {
    "kcal": 155.0,
    "protein_g": 12.58,
    "carb_g": 1.12,
    "fat_g": 10.61,
    "targets": { "kcal": 2200.0, "protein_g": 160.0, "carb_g": 220.0, "fat_g": 70.0 }
  }
}`;

// ---------------------------------------------------------------------------
// REST: log food (stage, then commit)
// ---------------------------------------------------------------------------

export const LOG_PY = `import os
import uuid
from datetime import datetime, timezone

import httpx

tally = httpx.Client(
    base_url="${SITE_URL}",
    headers={"Authorization": f"Bearer {os.environ['TALLY_API_KEY']}"},
    timeout=130,  # a log looks every food up before it answers
)

# 1. Stage. Tally finds the foods and portions; nothing is in the diary yet.
staged = tally.post("/api/logs", json={
    "id": str(uuid.uuid4()),  # idempotency key: a retry with the same id never logs twice
    "text": "two eggs and a fairlife",
    "captured_at": datetime.now(timezone.utc).isoformat(),
    "tz": "America/Los_Angeles",
}).raise_for_status().json()

if not staged["items"]:  # nothing staged; agent.reply is usually a question for you
    raise SystemExit(staged["agent"]["reply"] if staged["agent"] else "nothing staged")

# 2. Commit. Send back every row to keep; a row left out is removed.
day = tally.post(
    f"/api/logs/{staged['log_id']}/commit", json={"items": staged["items"]}
).raise_for_status().json()
print(day["kcal"], "kcal today", day["errors"])
`;

export const LOG_FETCH = `const base = "${SITE_URL}";
const headers = {
  Authorization: \`Bearer \${process.env.TALLY_API_KEY}\`,
  "Content-Type": "application/json",
};
async function post(path, body) {
  const res = await fetch(base + path, { method: "POST", headers, body: JSON.stringify(body) });
  if (!res.ok) throw new Error(\`\${res.status} \${JSON.stringify(await res.json())}\`);
  return res.json();
}

// 1. Stage. Tally finds the foods and portions; nothing is in the diary yet.
const staged = await post("/api/logs", {
  id: crypto.randomUUID(), // idempotency key: a retry with the same id never logs twice
  text: "two eggs and a fairlife",
  captured_at: new Date().toISOString(),
  tz: "America/Los_Angeles",
});
if (staged.items.length === 0) throw new Error(staged.agent?.reply ?? "nothing staged");

// 2. Commit. Send back every row to keep; a row left out is removed.
const day = await post(\`/api/logs/\${staged.log_id}/commit\`, { items: staged.items });
console.log(day.kcal, "kcal today", day.errors);
`;

// LogCreateResponse (routes/logs.py), trimmed: each item also carries tier,
// alternates, servings and other fields that are often null.
export const LOG_RESPONSE = `{
  "log_id": "7c0e5a0e-2f4b-4f7e-9d0a-5b8f1f3c2a91",
  "meal": "breakfast",
  "items": [
    {
      "item_index": 0,
      "phrase": "eggs",
      "status": "resolved",
      "food": {
        "id": "2d4f8a61-9c3e-4b7a-8f10-6e2b5c9d1a37",
        "name": "Egg, whole, cooked, hard-boiled",
        "brand": null
      },
      "serving": { "id": "93437", "description": "1 large" },
      "qty": 2.0,
      "macros": { "kcal": 155.0, "protein_g": 12.58, "carb_g": 1.12, "fat_g": 10.61, "fiber_g": 0.0 },
      "meal": "breakfast",
      "confidence": "high",
      "estimated": "none"
    },
    {
      "item_index": 1,
      "phrase": "fairlife",
      "status": "resolved",
      "food": {
        "id": "9a3e7b52-1f6c-4d8e-b0a4-73c5e2d91f68",
        "name": "Ultra-filtered 2% milk",
        "brand": "fairlife"
      },
      "serving": { "id": "label", "description": "1 cup (240 ml)" },
      "qty": 1.0,
      "macros": { "kcal": 120.0, "protein_g": 13.0, "carb_g": 6.0, "fat_g": 4.5, "fiber_g": null },
      "meal": "breakfast",
      "confidence": "high",
      "estimated": "none"
    }
  ],
  "logged_at": null,
  "saved_meal": null,
  "note": null,
  "agent": {
    "reply": "Two eggs and a fairlife, staged for breakfast.",
    "weight": null,
    "options": null,
    "discarded": null,
    "updated": null
  }
}`;

// CommitResponse (routes/commit.py): the day's totals after the commit.
export const COMMIT_RESPONSE = `{
  "kcal": 275.0,
  "protein_g": 25.58,
  "carb_g": 7.12,
  "fat_g": 15.11,
  "targets": { "kcal": 2200.0, "protein_g": 160.0, "carb_g": 220.0, "fat_g": 70.0 },
  "errors": [],
  "saved_meal": null,
  "totals_day": "2026-09-23"
}`;
