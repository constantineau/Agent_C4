#!/usr/bin/env node
// Gemini grills the Lab and the race console as a hostile UI/UX reviewer. See README.md.
import { appendFileSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { parseArgs } from "node:util";
import { capture, startReplay } from "./capture.mjs";
import { REPO_DIR, geminiKey, labPassword } from "./env.mjs";
import { FindingsError, numberFindings, parseFindings, reportMd } from "./findings.mjs";
import { CostMeter, DEFAULT_MODEL, FakeModel, GeminiModel, IMAGE_TOKENS, PRICES, budgetedCall, resolveModel, textTokens } from "./models.mjs";
import { SYSTEM, caption, screenPrompt } from "./prompts.mjs";
import { SCREENS, SIZES, pickScreens, sourceBlock } from "./screens.mjs";

const DEFAULT_TIMELINE = "/home/constantineau/backups/replay-jul18/timeline-fullrace";

const HELP = `Usage: run.sh [options]

  --only LIST          screens whose id contains any of these (comma-separated), e.g. lab-polar,console
                       (default all: ${SCREENS.map((s) => s.id).join(", ")})
  --sizes LIST         iPad sizes, comma-separated (default ${Object.keys(SIZES).join(",")})
  --model NAME         cheap | mid | strong (${DEFAULT_MODEL}, default) | any Gemini model id
  --max-usd X          hard cost cap for the run (default 1.0)
  --thinking LEVEL     minimal|low|medium|high (default medium: low gave the cheap model 0 findings)
  --resolution LEVEL   screenshot detail sent to Gemini: low|medium|high (default high)
  --source-chars N     most front-end source characters sent per screen (default 120000)
  --lab-url URL        the Lab (default http://localhost:8103, the standing dev stack)
  --timeline DIR       Race Rewind timeline for the console (default ${DEFAULT_TIMELINE});
                       a replay server is started on a free port and stopped at the end
  --replay-url URL     use an already-running replay server instead of starting one
  --shots DIR          reuse an earlier run's screenshots (its manifest.json) instead of capturing
  --capture-only       take the screenshots and stop (no model call)
  --out DIR            where runs go (default /mnt/data/c4/ui-grill)
  --dry-run            canned fake reviewer: no API key, no cost (screenshots are still real)
`;

function args() {
  const { values } = parseArgs({
    options: {
      only: { type: "string" },
      sizes: { type: "string", default: Object.keys(SIZES).join(",") },
      model: { type: "string" },
      "max-usd": { type: "string", default: "1.0" },
      thinking: { type: "string", default: "medium" },
      resolution: { type: "string", default: "high" },
      "source-chars": { type: "string", default: "120000" },
      "lab-url": { type: "string", default: "http://localhost:8103" },
      timeline: { type: "string", default: DEFAULT_TIMELINE },
      "replay-url": { type: "string" },
      shots: { type: "string" },
      "capture-only": { type: "boolean", default: false },
      out: { type: "string", default: "/mnt/data/c4/ui-grill" },
      "dry-run": { type: "boolean", default: false },
      help: { type: "boolean", default: false },
    },
  });
  return values;
}

const stamp = () => new Date().toISOString().replace(/[-:]/g, "").replace(/\..*$/, "").replace("T", "-");

async function main() {
  const o = args();
  if (o.help) {
    process.stdout.write(HELP);
    return;
  }
  if (!IMAGE_TOKENS[o.resolution]) throw new Error(`--resolution ${o.resolution}?`);
  const sizes = o.sizes.split(",").map((s) => s.trim()).filter(Boolean);
  for (const s of sizes) if (!SIZES[s]) throw new Error(`--sizes ${s}? (have ${Object.keys(SIZES).join(", ")})`);
  const screens = pickScreens(o.only);
  const capUsd = Number(o["max-usd"]);
  const sourceChars = Number(o["source-chars"]);

  let model;
  if (o["dry-run"]) model = new FakeModel({ resolution: o.resolution });
  else if (!o["capture-only"]) {
    const apiKey = geminiKey();
    if (!apiKey) throw new Error("no C4_GEMINI_API_KEY (export it or put it in the repo's .env); or use --dry-run");
    const id = resolveModel(o.model);
    if (!PRICES[id]) console.warn(`! ${id} is not in the price list; the cost is estimated at the strongest model's prices`);
    model = new GeminiModel({ apiKey, model: id, thinking: o.thinking, resolution: o.resolution });
  }
  const meter = new CostMeter(model?.model ?? "none", capUsd);

  // Two runs started in the same second must not share a folder: a short random tail.
  mkdirSync(o.out, { recursive: true });
  const dir = mkdtempSync(join(o.out, `${stamp()}${o["dry-run"] ? "-dry" : ""}-`));
  mkdirSync(join(dir, "shots"), { recursive: true });
  const startedAt = new Date().toISOString();
  console.log(`ui grill -> ${dir}`);

  // 1. Screenshots (or an earlier run's).
  let manifest;
  let shotsDir = dir;
  if (o.shots) {
    shotsDir = resolve(o.shots);
    manifest = JSON.parse(readFileSync(join(shotsDir, "manifest.json"), "utf8"));
    manifest.shots = manifest.shots.filter((r) => screens.some((s) => s.id === r.screen) && sizes.includes(r.size));
    console.log(`  reusing ${manifest.shots.length} screenshots from ${shotsDir}`);
  } else {
    let replay;
    const needConsole = screens.some((s) => s.app === "console");
    try {
      if (needConsole && !o["replay-url"]) {
        replay = await startReplay(o.timeline);
        console.log(`  replay server ${replay.url} (${o.timeline})`);
      }
      const shots = await capture({
        screens,
        sizes,
        dir,
        labUrl: o["lab-url"].replace(/\/$/, ""),
        labPassword: screens.some((s) => s.app === "lab") ? labPassword() : undefined,
        replayUrl: (o["replay-url"] ?? replay?.url ?? "").replace(/\/$/, ""),
        say: console.log,
      });
      manifest = { started_at: startedAt, lab_url: o["lab-url"], timeline: needConsole ? o["replay-url"] ?? o.timeline : null, sizes, shots };
    } finally {
      replay?.stop();
    }
  }
  writeFileSync(join(dir, "manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`);
  const okShots = manifest.shots.filter((r) => r.ok);
  console.log(`  ${okShots.length} of ${manifest.shots.length} screenshots ok`);
  if (o["capture-only"]) return;

  // 2. One reviewer call per screen.
  const logFile = join(dir, "log.jsonl");
  const log = (row) => appendFileSync(logFile, `${JSON.stringify(row)}\n`);
  const findings = [];
  const errors = [];
  let grilled = 0;
  for (const screen of screens) {
    const rows = okShots.filter((r) => r.screen === screen.id);
    if (!rows.length) {
      errors.push({ screen: screen.id, error: "no screenshots" });
      continue;
    }
    const images = rows.map((r) => ({ b64: readFileSync(join(shotsDir, r.file)).toString("base64"), caption: caption(r), size: r.size, state: r.state }));
    const text = screenPrompt(screen, rows, sourceBlock(REPO_DIR, screen, sourceChars));
    const estInput = textTokens(SYSTEM) + textTokens(text) + images.length * (IMAGE_TOKENS[o.resolution] + 30);
    let res;
    try {
      res = await budgetedCall(meter, model, "grill", { system: SYSTEM, text, images, screen }, { estInput, minOut: 800, maxOut: 8000 });
    } catch (e) {
      errors.push({ screen: screen.id, error: e.message });
      log({ screen: screen.id, error: e.message });
      console.log(`  ${screen.id}: FAILED ${e.message}`);
      continue;
    }
    if (!res) {
      errors.push({ screen: screen.id, error: `cost cap reached ($${meter.usd.toFixed(3)} of $${capUsd})` });
      console.log(`  ${screen.id}: skipped, cost cap reached`);
      continue;
    }
    grilled += 1;
    let got = [];
    try {
      got = parseFindings(res.text, screen.id);
    } catch (e) {
      if (!(e instanceof FindingsError)) throw e;
      errors.push({ screen: screen.id, error: `unreadable reply: ${e.message}` });
    }
    findings.push(...got);
    log({ screen: screen.id, images: rows.length, usage: res.usage, usd: Number(meter.usd.toFixed(4)), findings: got.length, reply: res.text });
    console.log(`  ${screen.id}: ${rows.length} shots, ${got.length} findings, $${meter.usd.toFixed(4)} so far`);
  }

  // 3. Outputs.
  const numbered = numberFindings(findings);
  const summary = {
    started_at: startedAt,
    dry_run: o["dry-run"],
    screens: screens.map((s) => s.id),
    screens_grilled: grilled,
    shots: okShots.length,
    shots_failed: manifest.shots.filter((r) => !r.ok).map((r) => ({ file: r.file, error: r.error })),
    blocked_writes: [...new Set(manifest.shots.flatMap((r) => r.blocked ?? []))],
    findings: numbered.length,
    security_findings: numbered.filter((f) => f.security).length,
    by_severity: Object.fromEntries(["blocker", "major", "minor"].map((s) => [s, numbered.filter((f) => f.severity === s).length])),
    errors,
    cost: meter.summary(),
  };
  writeFileSync(join(dir, "findings.json"), `${JSON.stringify(numbered, null, 2)}\n`);
  writeFileSync(join(dir, "summary.json"), `${JSON.stringify(summary, null, 2)}\n`);
  writeFileSync(join(dir, "report.md"), reportMd({ findings: numbered, summary, errors }));
  const c = meter.summary();
  console.log(`  findings: ${numbered.length} (${summary.by_severity.blocker} blocker, ${summary.by_severity.major} major, ${summary.by_severity.minor} minor; ${summary.security_findings} security)`);
  console.log(`  cost: ${c.calls} calls, ${c.input_tokens} in + ${c.output_tokens} out tokens = $${c.usd.toFixed(4)}${o["dry-run"] ? " (estimate; dry run spent nothing)" : ""}`);
  console.log(`  report: ${join(dir, "report.md")}`);
}

main().catch((e) => {
  console.error(`ui grill failed: ${e.message}`);
  process.exit(1);
});
