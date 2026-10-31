// What a reviewer reply must look like, how a reply becomes checked findings, and the
// report written from them. Security findings are kept apart: the repo is public.

export const SEVERITIES = ["blocker", "major", "minor"];

export const FINDINGS_SCHEMA = {
  type: "object",
  properties: {
    findings: {
      type: "array",
      items: {
        type: "object",
        properties: {
          title: { type: "string", description: "One line, plain words: what is wrong." },
          screen: { type: "string", description: "The screen id you were given." },
          size: { type: "string", description: "Which iPad size(s) it shows on, e.g. 1024x1366, or 'all'." },
          state: { type: "string", description: "Which state/moment it shows in (a caption's state), or 'all'." },
          severity: { type: "string", enum: SEVERITIES, description: "blocker = wrong call or unreadable on deck; major = slows or misleads a crew member; minor = polish." },
          evidence: { type: "string", description: "What exactly is on the screenshot that shows it (the text, number, colour, position)." },
          suggested_fix: { type: "string", description: "A concrete change." },
          security: { type: "boolean", description: "true if this is about secrets, auth, tokens, exposed data or anything an attacker could use." },
          file: { type: "string", description: "Source file it comes from, if you can tell, else empty." },
          line: { type: "integer", description: "Line number in that file, if you can tell, else 0." },
        },
        required: ["title", "screen", "size", "severity", "evidence", "suggested_fix", "security"],
      },
    },
  },
  required: ["findings"],
};

export class FindingsError extends Error {}

/** The first JSON object in a reply (models sometimes wrap it in ``` fences). */
export function extractJson(text) {
  if (typeof text !== "string" || !text.trim()) throw new FindingsError("empty reply");
  const fenced = text.match(/```(?:json)?\s*([\s\S]*?)```/);
  const body = fenced ? fenced[1] : text;
  const start = body.indexOf("{");
  const end = body.lastIndexOf("}");
  if (start < 0 || end < start) throw new FindingsError("no JSON object in reply");
  try {
    return JSON.parse(body.slice(start, end + 1));
  } catch (e) {
    throw new FindingsError(`bad JSON: ${e.message}`);
  }
}

const str = (v) => (typeof v === "string" ? v.trim() : v == null ? "" : String(v).trim());

/**
 * A reply -> checked findings for one screen. Drops entries with no title or evidence (a
 * finding nobody can verify is noise), forces the screen id, defaults a bad severity to minor,
 * and treats anything that smells of secrets as security even if the model said false.
 */
export function parseFindings(text, screenId) {
  const obj = extractJson(text);
  const list = Array.isArray(obj) ? obj : obj.findings;
  if (!Array.isArray(list)) throw new FindingsError("no findings array");
  const out = [];
  for (const f of list) {
    if (!f || typeof f !== "object") continue;
    const title = str(f.title);
    const evidence = str(f.evidence);
    if (!title || !evidence) continue;
    const severity = SEVERITIES.includes(str(f.severity).toLowerCase()) ? str(f.severity).toLowerCase() : "minor";
    const line = Number.isInteger(f.line) ? f.line : /^\d+$/.test(str(f.line)) ? Number(str(f.line)) : 0;
    const finding = {
      title,
      screen: screenId,
      size: str(f.size) || "all",
      state: str(f.state) || "all",
      severity,
      evidence,
      suggested_fix: str(f.suggested_fix),
      security: f.security === true || f.security === "true" || looksSecurity(`${title} ${evidence} ${str(f.suggested_fix)}`),
      file: str(f.file),
      line: line > 0 ? line : 0,
    };
    out.push(finding);
  }
  return out;
}

// Words that mean "don't put this on a public tracker" whatever the model's flag says.
const SECURITY_WORDS = /\b(password|passwd|secret|api[ -_]?key|(?:auth|access|api|bearer|session|lab|github|gh) ?tokens?|bearer|credential|auth(?:entication|orization)?|session ?storage|local ?storage|xss|injection|csrf|cors|leak|innerhtml|unauthenticated|0\.0\.0\.0)\b/i;
export const looksSecurity = (text) => SECURITY_WORDS.test(text ?? "");

const RANK = { blocker: 0, major: 1, minor: 2 };
export const bySeverity = (a, b) => RANK[a.severity] - RANK[b.severity] || a.screen.localeCompare(b.screen);

/** Number the findings (G1, G2…) in severity order so the verify step can name them. */
export function numberFindings(findings) {
  return findings.slice().sort(bySeverity).map((f, i) => ({ id: `G${i + 1}`, ...f }));
}

/** The human report: public findings by severity, then only a count of security ones. */
export function reportMd({ findings, summary, errors = [] }) {
  const pub = findings.filter((f) => !f.security);
  const sec = findings.filter((f) => f.security);
  const lines = [
    `# UI grill: ${summary.started_at}`,
    "",
    `${summary.dry_run ? "DRY RUN (canned fake reviewer, no Gemini). " : ""}Model ${summary.cost.model}, ${summary.screens_grilled} screens, ${summary.shots} screenshots, $${summary.cost.usd.toFixed(4)} (cap $${summary.cost.cap_usd}).`,
    `${findings.length} findings: ${SEVERITIES.map((s) => `${findings.filter((f) => f.severity === s).length} ${s}`).join(", ")}. ${sec.length} flagged security (kept out of this file's body: see findings.json, never post them publicly).`,
    "",
    "These are unverified model output. Check each against its screenshot and the code before filing.",
    "",
  ];
  for (const sev of SEVERITIES) {
    const group = pub.filter((f) => f.severity === sev);
    if (!group.length) continue;
    lines.push(`## ${sev[0].toUpperCase()}${sev.slice(1)} (${group.length})`, "");
    for (const f of group) {
      lines.push(`### ${f.id}. ${f.title}`);
      lines.push(`- Screen: \`${f.screen}\`, size ${f.size}, state ${f.state}`);
      lines.push(`- Evidence: ${f.evidence}`);
      if (f.suggested_fix) lines.push(`- Fix: ${f.suggested_fix}`);
      if (f.file) lines.push(`- Code: \`${f.file}${f.line ? `:${f.line}` : ""}\``);
      lines.push("");
    }
  }
  if (sec.length) lines.push(`## Security (${sec.length})`, "", `Ids: ${sec.map((f) => f.id).join(", ")}. Details in findings.json only. They go to the private c4-ops file, never GitHub.`, "");
  if (errors.length) lines.push("## Screens that failed", "", ...errors.map((e) => `- \`${e.screen}\`: ${e.error}`), "");
  return `${lines.join("\n")}\n`;
}
