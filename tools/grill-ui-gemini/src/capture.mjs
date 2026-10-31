// Screenshots of every screen x state x iPad size, plus a manifest. The Lab is the standing
// dev stack (read-only: every non-GET request from the page is blocked); the console is the
// Jul 18 Race Rewind replay, served by tools/replay/server.py on a free port.
import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { chromium } from "playwright";
import { REPO_DIR } from "./env.mjs";
import { SIZES } from "./screens.mjs";

const IPAD_UA = "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1";

/** A TCP port nothing is listening on right now. */
export function freePort() {
  return new Promise((resolve, reject) => {
    const srv = createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

/** Start the replay server on a free port; resolves once /replay/index answers. */
export async function startReplay(timeline, { port } = {}) {
  const p = port ?? (await freePort());
  const child = spawn("python3", [join(REPO_DIR, "tools/replay/server.py"), "--timeline", timeline, "--port", String(p)], { stdio: ["ignore", "pipe", "pipe"] });
  let log = "";
  child.stdout.on("data", (d) => (log += d));
  child.stderr.on("data", (d) => (log += d));
  const url = `http://127.0.0.1:${p}`;
  // Loading ~100 MB of frames takes a few seconds.
  for (let i = 0; i < 120; i++) {
    if (child.exitCode !== null) throw new Error(`replay server exited: ${log.trim().split("\n").slice(-3).join(" | ")}`);
    try {
      const r = await fetch(`${url}/replay/index`);
      if (r.ok) return { url, stop: () => child.kill("SIGTERM"), log: () => log };
    } catch {
      // not up yet
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  child.kill("SIGTERM");
  throw new Error("replay server did not come up in 60 s");
}

/** A GET-only Bearer token for the Lab (POST /api/auth is a login, not a write). */
export async function labToken(labUrl, password) {
  const r = await fetch(`${labUrl}/api/auth`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password }) });
  if (!r.ok) throw new Error(`Lab sign-in failed: HTTP ${r.status}`);
  return (await r.json()).token;
}

/** Nearest frame index for an ISO time. */
export function nearestFrame(times, iso) {
  const want = Date.parse(iso);
  let best = 0;
  let bestD = Infinity;
  times.forEach((t, i) => {
    const d = Math.abs(Date.parse(t) - want);
    if (d < bestD) {
      bestD = d;
      best = i;
    }
  });
  return { index: best, t: times[best], off_s: Math.round(bestD / 1000) };
}

// The console polls forever, so it never goes network-idle: it only gets the pause.
async function settle(page, ms = 800, idle = true) {
  if (idle) {
    try {
      await page.waitForLoadState("networkidle", { timeout: 6000 });
    } catch {
      // map tiles keep loading; a pause is enough
    }
  }
  await page.waitForTimeout(ms);
}

async function runSteps(page, steps = [], idle = true) {
  for (const s of steps) {
    if (s.click) await page.locator(s.click).first().click({ timeout: 5000 });
    if (s.check) await page.locator(s.check).first().check({ timeout: 5000 });
    if (s.selectIndex) await page.locator(s.selectIndex).first().selectOption({ index: s.index }, { timeout: 5000 });
    await settle(page, 800, idle);
  }
}

async function newIpad(browser, size) {
  const ctx = await browser.newContext({ viewport: SIZES[size], deviceScaleFactor: 1, isMobile: true, hasTouch: true, userAgent: IPAD_UA });
  return ctx;
}

/** Guard a page: block anything that isn't a read, answer mocks, collect page errors. */
async function guard(page, { mock = {}, blocked, errors }) {
  page.on("pageerror", (e) => errors.push(e.message.slice(0, 300)));
  await page.route("**/*", (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (mock[url.pathname] && req.method() === "GET") return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(mock[url.pathname]) });
    if (req.method() !== "GET" && req.method() !== "HEAD") {
      const what = `${req.method()} ${url.pathname}`;
      if (!blocked.includes(what)) blocked.push(what);
      return route.abort();
    }
    return route.continue();
  });
}

/**
 * Capture every screen. Returns the manifest rows; writes PNGs to `dir/shots/`.
 * `say` logs progress; failures of one shot are recorded and the rest go on.
 */
export async function capture({ screens, sizes, dir, labUrl, labPassword, replayUrl, say = () => {} }) {
  const browser = await chromium.launch({ headless: true, args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"] });
  const rows = [];
  let token;
  let times;
  try {
    for (const screen of screens) {
      if (screen.app === "lab" && !screen.signedOut && !token) token = await labToken(labUrl, labPassword);
      if (screen.app === "console" && !times) times = (await (await fetch(`${replayUrl}/replay/index`)).json()).t;
      for (const state of screen.states) {
        for (const size of sizes) {
          const file = `shots/${screen.id}__${state.id}__${size}.png`;
          const row = { screen: screen.id, state: state.id, size, file, note: state.note ?? "", blocked: [], page_errors: [] };
          const ctx = await newIpad(browser, size);
          try {
            const page = await ctx.newPage();
            await guard(page, { mock: state.mock, blocked: row.blocked, errors: row.page_errors });
            if (screen.app === "lab") {
              if (!screen.signedOut) await ctx.addInitScript((t) => sessionStorage.setItem("c4lab.token", t), token);
              row.url = `${labUrl}/${screen.hash}`;
              await page.goto(row.url, { waitUntil: "domcontentloaded", timeout: 30000 });
            } else {
              const f = nearestFrame(times, state.at);
              Object.assign(row, { at: state.at, frame: f.index, frame_t: f.t, frame_off_s: f.off_s });
              row.url = `${replayUrl}/dashboard/`;
              await page.goto(row.url, { waitUntil: "domcontentloaded", timeout: 30000 });
              await settle(page, 1000, false);
              // The console runs top-level here (no review page around it), so the frame is
              // posted to the window itself; the shim only checks the message type.
              await page.evaluate(async (i) => {
                const frame = await (await fetch(`/replay/frame?i=${i}`)).json();
                window.postMessage({ type: "replay:frame", frame }, "*");
              }, f.index);
              await page.waitForTimeout(2500);
            }
            const idle = screen.app !== "console";
            await settle(page, 800, idle);
            await runSteps(page, state.steps, idle);
            writeFileSync(join(dir, file), await page.screenshot({ type: "png" }));
            row.scroll_height = await page.evaluate(() => document.documentElement.scrollHeight);
            row.ok = true;
            say(`  shot ${file}${row.blocked.length ? ` (blocked ${row.blocked.length} writes)` : ""}`);
          } catch (e) {
            row.ok = false;
            row.error = e.message.split("\n")[0].slice(0, 300);
            say(`  FAILED ${file}: ${row.error}`);
          } finally {
            await ctx.close();
          }
          rows.push(row);
        }
      }
    }
  } finally {
    await browser.close();
  }
  return rows;
}
