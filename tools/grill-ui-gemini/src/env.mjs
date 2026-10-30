// Secrets read at runtime. Nothing here is ever printed or written to a run's output.
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
export const TOOL_DIR = resolve(here, "..");
export const REPO_DIR = resolve(TOOL_DIR, "../..");
export const MAIN_CHECKOUT = "/home/constantineau/Agent_C4";

// Where a .env may hold a secret: this checkout's root, then the main checkout (worktrees
// have none of their own). C4_ENV_FILE overrides both.
const ENV_FILES = [process.env.C4_ENV_FILE, join(REPO_DIR, ".env"), join(MAIN_CHECKOUT, ".env")].filter(Boolean);

/** One `NAME=value` from a .env text (last one wins, quotes stripped). */
export function readDotenv(text, name) {
  let value;
  for (const line of text.split(/\r?\n/)) {
    const m = line.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/);
    if (m && m[1] === name) value = m[2].replace(/^(['"])(.*)\1$/, "$2");
  }
  return value || undefined;
}

/** `name` exported, else the first .env that has it. */
export function secret(name, env = process.env, files = ENV_FILES) {
  if (env[name]) return env[name];
  for (const f of files) {
    try {
      if (!existsSync(f)) continue;
      const v = readDotenv(readFileSync(f, "utf8"), name);
      if (v) return v;
    } catch {
      // unreadable .env: try the next one
    }
  }
  return undefined;
}

/** The Gemini key: C4_GEMINI_API_KEY, exported or in a .env. */
export const geminiKey = (env, files) => secret("C4_GEMINI_API_KEY", env, files);

/**
 * The dev Lab's team password (read-only use: it only buys a GET token). C4_LAB_PASSWORD,
 * else LAB_PASSWORD in a .env, else what the running dev container was started with, else
 * compose.dev.yml's default.
 */
export function labPassword(env = process.env, files = ENV_FILES, container = "sr33-dev-lab-1") {
  const v = secret("C4_LAB_PASSWORD", env, files) ?? secret("LAB_PASSWORD", {}, files);
  if (v) return v;
  try {
    const out = execFileSync("docker", ["exec", container, "printenv", "LAB_PASSWORD"], { stdio: ["ignore", "pipe", "ignore"] }).toString().trim();
    if (out) return out;
  } catch {
    // no docker or no container: the compose default
  }
  return "lab-dev";
}
