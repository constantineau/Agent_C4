// The reviewer: Gemini through the official SDK (@google/genai, Interactions API), or a canned
// fake for dry runs. Plus the price list and the cost meter that enforces the cap.
// Copied from Tramp's tools/playtest-gemini/src/models.mjs; one call per screen, several images.
import { FINDINGS_SCHEMA } from "./findings.mjs";

// USD per 1M tokens, paid Standard tier, from https://ai.google.dev/gemini-api/docs/pricing
// as read on 2026-10-16. Output includes thinking. Some prices double on 2026-12-01
// (gemini-4-argon) and 2027-01-01 (3.8/3.6 flash): update this table then.
export const PRICES = {
  "gemini-3.1-flash-lite": { input: 0.25, output: 1.5 },
  "gemini-3.5-flash-lite": { input: 0.3, output: 2.5 },
  "gemini-3-flash-preview": { input: 0.5, output: 3.0 },
  "gemini-3.8-flash": { input: 0.75, output: 3.75 },
  "gemini-4-argon": { input: 2.0, output: 10.0 },
  "gemini-3.1-pro-preview": { input: 2.0, output: 12.0 },
  fake: { input: 0.3, output: 2.5 }, // priced like the default so dry runs show a realistic estimate
};

// --model accepts these names too.
export const ALIASES = {
  cheap: "gemini-3.5-flash-lite",
  mid: "gemini-3.8-flash",
  strong: "gemini-4-argon",
};
export const DEFAULT_MODEL = ALIASES.strong; // as Tramp: the strong model by default, --model cheap to try things

export function resolveModel(name) {
  const id = ALIASES[name] ?? name ?? DEFAULT_MODEL;
  return id;
}

/** Tokens -> USD for a model (unknown models are priced like the strongest, to be safe). */
export function costUsd(model, inputTokens, outputTokens) {
  const p = PRICES[model] ?? PRICES["gemini-4-argon"];
  return (inputTokens * p.input + outputTokens * p.output) / 1e6;
}

// Image tokens per media resolution for Gemini 3/4 (docs/media-resolution, 2026-10-16).
export const IMAGE_TOKENS = { low: 280, medium: 560, high: 1120, ultra_high: 2240 };

/** Rough token count for text: ~4 characters a token. */
export const textTokens = (s) => Math.ceil((s?.length ?? 0) / 4);

/** Adds up what a run spends and says whether the next call still fits under the cap. */
export class CostMeter {
  constructor(model, capUsd) {
    this.model = model;
    this.capUsd = capUsd;
    this.inputTokens = 0;
    this.outputTokens = 0;
    this.calls = 0;
  }
  add(usage) {
    this.inputTokens += usage.input ?? 0;
    this.outputTokens += usage.output ?? 0;
    this.calls += 1;
  }
  get usd() {
    return costUsd(this.model, this.inputTokens, this.outputTokens);
  }
  /** Would a call of about this size push the run over the cap? */
  fits(estInput, estOutput) {
    return this.usd + costUsd(this.model, estInput, estOutput) <= this.capUsd;
  }
  /**
   * Most output tokens (thinking included) a call with about `estInput` input tokens may
   * use and still leave `reserveUsd` under the cap; 0 when fewer than `minOut` fit.
   * `headroom` multiplies the input: 1.25 for a ~4-characters-a-token guess, less for a
   * count from the API.
   */
  outputAllowance(estInput, { minOut = 1, maxOut = Infinity, reserveUsd = 0, headroom = 1.25 } = {}) {
    const p = PRICES[this.model] ?? PRICES["gemini-4-argon"];
    const left = this.capUsd - this.usd - reserveUsd - (estInput * headroom * p.input) / 1e6;
    const n = Math.floor((left * 1e6) / p.output);
    return n >= minOut ? Math.min(n, maxOut) : 0;
  }
  summary() {
    return {
      model: this.model,
      calls: this.calls,
      input_tokens: this.inputTokens,
      output_tokens: this.outputTokens,
      usd: Number(this.usd.toFixed(4)),
      cap_usd: this.capUsd,
    };
  }
}

/**
 * One model call inside the budget: checks the cap first and caps the reply's tokens to
 * what is left, so no call is made, and none can overspend, once the money is gone.
 * Returns null (and calls nothing) when it doesn't fit.
 */
export async function budgetedCall(meter, model, kind, args, { estInput, minOut, maxOut, reserveUsd = 0 }) {
  // The real input size from the API (free), not the guess: a guess that runs short let a
  // $0.02 cap reach $0.027. If counting fails, no paid call is made: a guess can't bound the
  // input. Only the fake model, which costs nothing, goes on the guess.
  let input = estInput;
  let headroom = 2;
  if (model.countInput) {
    try {
      input = await model.countInput(args);
      headroom = 1.05;
    } catch {
      return null;
    }
  }
  const maxOutputTokens = meter.outputAllowance(input, { minOut, maxOut, reserveUsd, headroom });
  if (!maxOutputTokens) return null;
  let res;
  try {
    res = await model[kind]({ ...args, maxOutputTokens });
  } catch (e) {
    // The call may have been billed with its reply lost: count it at its most, so the report
    // that follows still fits the cap.
    meter.add({ input, output: maxOutputTokens });
    throw e;
  }
  meter.add(res.usage);
  return res;
}

/** Gemini usage block -> { input, output } (thinking is billed as output). */
export function usageOf(interaction) {
  const u = interaction?.usage ?? {};
  return {
    input: u.total_input_tokens ?? 0,
    output: (u.total_output_tokens ?? 0) + (u.total_thought_tokens ?? 0),
  };
}

const mediaLevel = (r) => ({ level: `MEDIA_RESOLUTION_${r.toUpperCase()}` });

/**
 * Real Gemini. `grill()` sends one screen (its screenshots + source) and asks for the findings
 * as JSON. Stateless (store: false): each screen is its own call, so cost per screen is flat.
 */
export class GeminiModel {
  constructor({ apiKey, model, thinking = "low", resolution = "high" }) {
    this.model = model;
    this.thinking = thinking;
    this.resolution = resolution;
    this.apiKey = apiKey;
  }
  async client() {
    if (!this._client) {
      const { GoogleGenAI } = await import("@google/genai");
      this._client = new GoogleGenAI({ apiKey: this.apiKey });
    }
    return this._client;
  }
  /**
   * The request's input tokens, counted by the API (models.countTokens, free). The Developer
   * API won't take a system instruction there, so it is counted as text, the schema too.
   */
  async countInput({ system, text, images = [] }) {
    const ai = await this.client();
    const parts = [{ text: system }, { text }, { text: JSON.stringify(FINDINGS_SCHEMA) }];
    for (const img of images) {
      if (img.caption) parts.push({ text: img.caption });
      parts.push({ inlineData: { data: img.b64, mimeType: "image/png" }, mediaResolution: mediaLevel(this.resolution) });
    }
    const res = await ai.models.countTokens({ model: this.model, contents: [{ role: "user", parts }] });
    if (!Number.isFinite(res?.totalTokens)) throw new Error("no token count");
    return res.totalTokens;
  }
  async grill({ system, text, images = [], maxOutputTokens }) {
    const ai = await this.client();
    const input = [{ type: "text", text }];
    for (const img of images) {
      // Each image is named just before it, so findings can say which size/state they mean.
      if (img.caption) input.push({ type: "text", text: img.caption });
      input.push({ type: "image", data: img.b64, mime_type: "image/png", resolution: this.resolution });
    }
    const req = {
      model: this.model,
      system_instruction: system,
      input,
      store: false,
      generation_config: {
        thinking_level: this.thinking,
        temperature: 1.0,
        // Thinking counts towards it: the API stops there, so the cost cap holds.
        ...(maxOutputTokens ? { max_output_tokens: maxOutputTokens } : {}),
      },
      response_format: { type: "text", mime_type: "application/json", schema: FINDINGS_SCHEMA },
    };
    let last;
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const res = await ai.interactions.create(req);
        return { text: res.output_text ?? "", usage: usageOf(res) };
      } catch (e) {
        last = e;
        // Retry only a 429: the request was refused unprocessed, so it cost nothing. A 5xx or a
        // lost reply may have been generated and billed, and a retry could pass the cap.
        const status = e?.status ?? e?.code;
        if (Number(status) !== 429) break;
        await new Promise((r) => setTimeout(r, 2000 * (attempt + 1)));
      }
    }
    throw new Error(`Gemini call failed: ${last?.message ?? last}`);
  }
}

/**
 * Canned fake for --dry-run: one finding per screen that names the screen's first image, and
 * one security-flagged finding on the first screen it sees, so the whole pipeline (parsing,
 * the security split, the report) runs without a key. Usage is estimated like a real call.
 */
export class FakeModel {
  constructor({ resolution = "high" } = {}) {
    this.model = "fake";
    this.resolution = resolution;
    this.calls = 0;
  }
  usage(text, images, out) {
    return { input: textTokens(text) + images.length * IMAGE_TOKENS[this.resolution] + 800, output: textTokens(out) };
  }
  async countInput({ text, images = [] }) {
    return this.usage(text, images, "").input;
  }
  async grill({ text, images = [], screen }) {
    const first = images[0] ?? {};
    const findings = [
      {
        title: `(dry run) ${screen?.title ?? "screen"}: smallest text may be unreadable in sun`,
        screen: screen?.id ?? "unknown",
        size: first.size ?? "any",
        state: first.state ?? "",
        severity: "minor",
        evidence: `fake finding on ${first.caption ?? "the first screenshot"}`,
        suggested_fix: "none: this is the dry-run fake",
        security: false,
        file: screen?.sources?.[0]?.file ?? "",
        line: 0,
      },
    ];
    if (this.calls++ === 0) {
      findings.push({ ...findings[0], title: "(dry run) security-flagged example", severity: "major", security: true });
    }
    const out = JSON.stringify({ findings });
    return { text: out, usage: this.usage(text, images, out) };
  }
}
