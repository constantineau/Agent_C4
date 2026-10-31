import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { nearestFrame } from "../src/capture.mjs";
import { REPO_DIR, geminiKey, labPassword, readDotenv } from "../src/env.mjs";
import { FindingsError, numberFindings, parseFindings, reportMd } from "../src/findings.mjs";
import { CostMeter, FakeModel, GeminiModel, budgetedCall, costUsd, resolveModel } from "../src/models.mjs";
import { caption } from "../src/prompts.mjs";
import { SCREENS, SIZES, pickScreens, sliceSource, sourceBlock } from "../src/screens.mjs";

test("model tiers and prices are Tramp's", () => {
  assert.equal(resolveModel("cheap"), "gemini-3.5-flash-lite");
  assert.equal(resolveModel("mid"), "gemini-3.8-flash");
  assert.equal(resolveModel("strong"), "gemini-4-argon");
  assert.equal(resolveModel(undefined), "gemini-4-argon");
  assert.equal(costUsd("gemini-3.5-flash-lite", 1e6, 1e6), 2.8);
  assert.equal(costUsd("gemini-unknown", 1e6, 0), 2.0); // unknown: priced like the strongest
});

test(".env reading: exported first, then the first file that has it", () => {
  assert.equal(readDotenv("A=1\nC4_GEMINI_API_KEY='abc'\n", "C4_GEMINI_API_KEY"), "abc");
  const dir = mkdtempSync(join(tmpdir(), "grill-"));
  const f = join(dir, ".env");
  writeFileSync(f, "C4_GEMINI_API_KEY=fromfile\nLAB_PASSWORD=pw\n");
  assert.equal(geminiKey({}, [join(dir, "missing"), f]), "fromfile");
  assert.equal(geminiKey({ C4_GEMINI_API_KEY: "exported" }, [f]), "exported");
  assert.equal(geminiKey({}, [join(dir, "missing")]), undefined);
  assert.equal(labPassword({}, [f], "no-such-container"), "pw");
  assert.equal(labPassword({ C4_LAB_PASSWORD: "x" }, [f], "no-such-container"), "x");
});

test("a cap of 0 makes no call; a call's output is capped to what's left", async () => {
  const calls = [];
  const stub = { countInput: async () => 2000, grill: async (a) => (calls.push(a), { text: "{}", usage: { input: 2000, output: 10 } }) };
  assert.equal(await budgetedCall(new CostMeter("gemini-4-argon", 0), stub, "grill", {}, { estInput: 2000, minOut: 800, maxOut: 8000 }), null);
  assert.equal(calls.length, 0);
  // argon $2/$10 per 1M: 2000 x1.05 in = $0.0042; $0.02 cap leaves $0.0158 = 1580 out
  await budgetedCall(new CostMeter("gemini-4-argon", 0.02), stub, "grill", {}, { estInput: 2000, minOut: 800, maxOut: 8000 });
  assert.equal(calls[0].maxOutputTokens, 1580);
});

test("Gemini gets every image with its caption, JSON schema and a token cap", async () => {
  const g = new GeminiModel({ apiKey: "x", model: "gemini-4-argon" });
  let req;
  g._client = { interactions: { create: async (r) => ((req = r), { output_text: '{"findings":[]}', usage: { total_input_tokens: 5, total_output_tokens: 1, total_thought_tokens: 2 } }) } };
  const res = await g.grill({ system: "s", text: "t", images: [{ b64: "AA", caption: "[a]" }, { b64: "BB", caption: "[b]" }], maxOutputTokens: 900 });
  assert.deepEqual(res.usage, { input: 5, output: 3 });
  assert.deepEqual(req.input.map((p) => p.type), ["text", "text", "image", "text", "image"]);
  assert.equal(req.input[3].text, "[b]");
  assert.equal(req.generation_config.max_output_tokens, 900);
  assert.equal(req.response_format.mime_type, "application/json");
  assert.equal(req.store, false);
  let counted;
  g._client = { models: { countTokens: async (r) => ((counted = r), { totalTokens: 42 }) } };
  assert.equal(await g.countInput({ system: "s", text: "t", images: [{ b64: "AA", caption: "[a]" }] }), 42);
  assert.deepEqual(counted.contents[0].parts.at(-1).mediaResolution, { level: "MEDIA_RESOLUTION_HIGH" });
});

test("findings: checked, screen forced, weak ones dropped, secrets forced private", () => {
  const reply = "```json\n" + JSON.stringify({
    findings: [
      { title: "TWS too small", screen: "wrong", size: "820x1180", severity: "BLOCKER", evidence: "'30 kts' is 14px grey", suggested_fix: "28px", security: false, file: "a.js", line: "12" },
      { title: "no evidence", evidence: "" },
      { title: "Odd", evidence: "x", severity: "meh", security: false },
      { title: "Team password shown in the URL", evidence: "url bar", severity: "major", security: false },
    ],
  }) + "\n```";
  const f = parseFindings(reply, "console");
  assert.equal(f.length, 3);
  assert.deepEqual([f[0].screen, f[0].severity, f[0].line, f[0].state], ["console", "blocker", 12, "all"]);
  assert.equal(f[1].severity, "minor");
  assert.equal(f[2].security, true);
  // a CSS colour token is not a secret (the first real run flagged one)
  assert.equal(parseFindings(JSON.stringify({ findings: [{ title: "Low contrast", evidence: "the muted colour token #4b5c6b", security: false }] }), "c")[0].security, false);
  assert.throws(() => parseFindings("no json here", "x"), FindingsError);
  assert.throws(() => parseFindings('{"nope":1}', "x"), FindingsError);
});

test("the report never carries a security finding's text", () => {
  const findings = numberFindings([
    { title: "minor thing", screen: "a", size: "all", state: "all", severity: "minor", evidence: "e", suggested_fix: "", security: false, file: "", line: 0 },
    { title: "SECRET-TITLE", screen: "a", size: "all", state: "all", severity: "blocker", evidence: "SECRET-EVIDENCE", suggested_fix: "", security: true, file: "", line: 0 },
  ]);
  assert.deepEqual(findings.map((f) => f.id), ["G1", "G2"]);
  assert.equal(findings[0].security, true); // blocker sorts first
  const md = reportMd({ findings, summary: { started_at: "t", dry_run: true, screens_grilled: 1, shots: 2, cost: { model: "fake", usd: 0, cap_usd: 1 } } });
  assert.ok(!md.includes("SECRET"));
  assert.match(md, /minor thing/);
  assert.match(md, /Ids: G1/);
});

test("the fake reviewer runs the whole pipeline with one security example", async () => {
  const fake = new FakeModel();
  const screen = SCREENS.find((s) => s.id === "console");
  const a = parseFindings((await fake.grill({ text: "x", images: [{ b64: "", caption: "c", size: "1024x1366", state: "early-race" }], screen })).text, "console");
  const b = parseFindings((await fake.grill({ text: "x", images: [], screen })).text, "console");
  assert.equal(a.filter((f) => f.security).length, 1);
  assert.equal(b.filter((f) => f.security).length, 0);
});

test("every screen's source markers exist in the real files", () => {
  for (const s of SCREENS) {
    for (const spec of s.sources) {
      const got = sliceSource(REPO_DIR, spec);
      assert.ok(got.text, `${s.id}: ${spec.file} missing`);
      assert.equal(got.note, "", `${s.id}: ${got.note}`);
    }
  }
});

test("source slices are line-numbered and cut at the budget", () => {
  const dir = mkdtempSync(join(tmpdir(), "grill-"));
  mkdirSync(join(dir, "w"));
  writeFileSync(join(dir, "w/a.js"), "zero\n/* -- A */\none\ntwo\n/* -- B */\nthree\n");
  const s = sliceSource(dir, { file: "w/a.js", from: "/* -- A", to: "/* -- B" });
  assert.equal(s.text, "2| /* -- A */\n3| one\n4| two");
  assert.match(sliceSource(dir, { file: "w/a.js", from: "nope" }).note, /not found/);
  const block = sourceBlock(dir, { sources: [{ file: "w/a.js" }, { file: "w/a.js" }] }, 600);
  assert.match(block, /left out/);
});

test("screens: filters, sizes, the Jul 18 moments", () => {
  assert.deepEqual(pickScreens("polar,console").map((s) => s.id), ["lab-polar", "console"]);
  assert.throws(() => pickScreens("nothing"), /no screen matches/);
  for (const k of ["1024x1366", "1366x1024", "820x1180"]) assert.ok(SIZES[k]);
  const at = SCREENS.find((s) => s.id === "console").states.map((s) => s.at);
  for (const t of ["2026-07-18T22:08:31Z", "2026-07-18T22:59:01Z", "2026-07-18T23:56:10Z"]) assert.ok(at.includes(t));
});

test("nearest replay frame and captions", () => {
  const f = nearestFrame(["2026-07-18T22:08:00Z", "2026-07-18T22:08:30Z", "2026-07-18T22:09:00Z"], "2026-07-18T22:08:31Z");
  assert.deepEqual(f, { index: 1, t: "2026-07-18T22:08:30Z", off_s: 1 });
  const c = caption({ screen: "console", state: "gps-kick", size: "820x1180", frame_t: "T", note: "n", scroll_height: 2000 });
  assert.match(c, /state=gps-kick size=820x1180, race time T \(n\), page continues below/);
});
