#!/usr/bin/env node
// Capture the manifest's `screenshots` (the ones Chrome shows in its richer
// install dialog) against a locally running production build:
//
//   pnpm --dir frontend build && pnpm --dir frontend start --port 3100
//   node scripts/gen-screenshots.mjs --url http://localhost:3100/
//
// Chrome's `--screenshot --window-size=W,H` is not enough on its own: it lays
// the page out at the default desktop width and then crops the image to W, so
// the narrow capture came back as a sliced-up desktop layout with the mobile
// breakpoint never applied. Emulation.setDeviceMetricsOverride over CDP is what
// actually resizes the layout viewport, so this drives Chrome directly. Node
// built-ins only (fetch + global WebSocket, Node 22+); no new dependencies.

import { spawn } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";

const CHROMES = [
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "google-chrome",
  "chromium",
];

const SHOTS = [
  // Chrome wants every screenshot sharing a form factor to share an aspect
  // ratio, and rejects anything past 3840px or an extreme ratio.
  { name: "wide.png", width: 1280, height: 800, mobile: false, scale: 1 },
  { name: "narrow.png", width: 412, height: 915, mobile: true, scale: 2 },
];

const arg = (flag, fallback) => {
  const i = process.argv.indexOf(flag);
  return i === -1 ? fallback : process.argv[i + 1];
};

const url = arg("--url", "http://localhost:3100/");
const outDir = arg("--out", "frontend/public/screenshots");
const chromePath = arg("--chrome", process.env.CHROME_PATH ?? CHROMES[0]);
const settle = Number(arg("--settle", "1500")); // let webfonts paint

function launch() {
  const child = spawn(chromePath, [
    "--headless=new",
    "--disable-gpu",
    "--hide-scrollbars",
    "--force-color-profile=srgb",
    "--remote-debugging-port=0",
    "--user-data-dir=" + join(process.env.TEMP ?? "/tmp", "tally-shot-profile"),
    "about:blank",
  ]);
  return new Promise((resolve, reject) => {
    let buf = "";
    const onErr = (d) => {
      buf += d;
      const m = buf.match(/DevTools listening on (ws:\/\/\S+)/);
      if (m) {
        child.stderr.off("data", onErr);
        resolve({ child, wsUrl: m[1] });
      }
    };
    child.stderr.on("data", onErr);
    child.on("error", reject);
    setTimeout(() => reject(new Error("Chrome did not report a DevTools port")), 20000);
  });
}

function connect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let id = 0;
  const pending = new Map();
  const waiters = [];
  ws.addEventListener("message", (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
    } else if (msg.method) {
      for (let i = waiters.length - 1; i >= 0; i--) {
        if (waiters[i].method === msg.method) {
          waiters[i].resolve(msg.params);
          waiters.splice(i, 1);
        }
      }
    }
  });
  const send = (method, params = {}, sessionId) =>
    new Promise((resolve, reject) => {
      const n = ++id;
      pending.set(n, { resolve, reject });
      ws.send(JSON.stringify({ id: n, method, params, sessionId }));
    });
  const once = (method, ms = 30000) =>
    new Promise((resolve, reject) => {
      waiters.push({ method, resolve });
      setTimeout(() => reject(new Error(`timeout waiting for ${method}`)), ms);
    });
  const ready = new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve);
    ws.addEventListener("error", () => reject(new Error("CDP connect failed")));
  });
  return { ws, send, once, ready };
}

const { child, wsUrl } = await launch();
const cdp = connect(wsUrl);
await cdp.ready;

await mkdir(outDir, { recursive: true });

for (const shot of SHOTS) {
  const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await cdp.send("Target.attachToTarget", {
    targetId,
    flatten: true,
  });

  await cdp.send("Page.enable", {}, sessionId);
  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width: shot.width,
    height: shot.height,
    deviceScaleFactor: shot.scale,
    mobile: shot.mobile,
  }, sessionId);

  const loaded = cdp.once("Page.loadEventFired");
  await cdp.send("Page.navigate", { url }, sessionId);
  await loaded;
  await new Promise((r) => setTimeout(r, settle));

  const { data } = await cdp.send("Page.captureScreenshot", { format: "png" }, sessionId);
  const path = join(outDir, shot.name);
  await mkdir(dirname(path), { recursive: true });
  await writeFile(path, Buffer.from(data, "base64"));
  const px = `${shot.width * shot.scale}x${shot.height * shot.scale}`;
  console.log(`  ${path}  ${px}  ${Buffer.from(data, "base64").length.toLocaleString()} bytes`);

  await cdp.send("Target.closeTarget", { targetId });
}

cdp.ws.close();
child.kill();
