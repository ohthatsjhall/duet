#!/usr/bin/env bun
// jev-replay.ts — replay a Jev pre-review battery over FINISHED duet runs and compare
// each answer with what the round-1 reviewer actually decided. Read-only on runs and
// repos; results go to ~/.duet/jev-replay/. Usage:
//   bun ~/.duet/jev-replay.ts [--dry] [run-id-substring ...]
import { readFileSync, existsSync, readdirSync, mkdirSync, writeFileSync } from "fs";
import { join } from "path";

const DUET = process.env.DUET_HOME ?? join(process.env.HOME!, ".duet");
const OUT = join(DUET, "jev-replay");
const MODEL = "jev-1.13.0";          // pinned: thresholds do not survive a model swap
const FIRE = 0.7;                    // sde_cascade's gate; any per-question noul above this fires
const MAX_DIFF_BYTES = 100_000;      // ~25k tokens; API cap is 32k for state + longest question
const DRY = process.argv.includes("--dry");
const only = process.argv.slice(2).filter(a => !a.startsWith("--"));

// ---- key: ~/.duet/.env, one KEY=value per line; real env wins ----
const envPath = join(DUET, ".env");
if (existsSync(envPath)) for (const line of readFileSync(envPath, "utf8").split("\n")) {
  const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$/);
  if (m && !process.env[m[1]]) process.env[m[1]] = m[2].replace(/^["']|["']$/g, "");
}
const KEY = process.env.TYPESAFE_API_KEY;
if (!KEY && !DRY) { console.error(`TYPESAFE_API_KEY not set. Put it in ${envPath}`); process.exit(2); }

// ---- the battery. Every noul is phrased so TRUE = something is wrong (escalate). ----
const RUBRIC = `critical: data loss, a security hole, or a defect that reaches the user on the main path.
high: the ticket's stated behaviour is not delivered, or is delivered wrongly; a stated acceptance criterion is unmet; a bug in an edge case a user can reach; an assertion that cannot fail, or a branch carrying a stated criterion with no test at all; evidence the ticket owes that was never produced.
medium: a real weakness with no user-visible consequence — a stale comment, a leaky abstraction, an inefficiency, a convention broken without harm.
low: preference, naming, formatting, tidiness.`;

const noul = (instructions: string, yes: string, no: string) =>
  ({ type: "noul", instructions, criteria: { true: yes, false: no } });

const QUESTIONS = {
  unmet_criterion: noul(
    "Does the `diff` leave a stated acceptance criterion in `ticket` unmet, or deliver it wrongly?",
    "At least one acceptance criterion the ticket states is not delivered, or is delivered incorrectly.",
    "Every acceptance criterion the ticket states is delivered as described."),
  untested_criterion: noul(
    "Is there a stated acceptance criterion in `ticket` whose behaviour has no test in the `diff` that would fail if that behaviour broke?",
    "Some stated criterion has no test, or only a test that would stay green if the behaviour were removed.",
    "Each stated criterion has a test that would fail if its behaviour broke."),
  vacuous_assertion: noul(
    "Does the `diff` add a test assertion that cannot fail, or that merely confirms whatever the implementation happens to do rather than a required behaviour?",
    "An added assertion is tautological, always true, or coupled to implementation detail instead of required behaviour.",
    "Added assertions express acceptance criteria or valid regressions and could fail."),
  reachable_bug: noul(
    "Does the `diff` introduce a defect in a code path a user can reach?",
    "There is a concrete input or state under which the new code misbehaves for a user.",
    "No user-reachable defect is introduced."),
  out_of_scope: noul(
    "Does the `diff` change behaviour that `ticket` does not ask for?",
    "It modifies behaviour outside the ticket's stated scope.",
    "Every behavioural change traces to something the ticket asks for."),
  unrequested_file: noul(
    "Does the `diff` add a file — documentation, verification notes, scratch output — that neither `ticket` nor ordinary repository conventions call for?",
    "A file was added that no ticket requirement or repository convention asks for.",
    "Every added file is one the ticket or the repository's conventions call for."),
  // holistic, kept for contrast with the per-question gate (sde_cascade's __overall__ head)
  should_revise: noul(
    "Applying `rubric`, would a careful independent reviewer send this `diff` back for at least one critical or high finding?",
    "There is at least one critical or high finding under the rubric.",
    "There is no critical or high finding; any issues are medium or low."),
  // severity as the reviewer would grade it — tests the 'second opinion' idea
  worst_severity: {
    type: "score",
    instructions: "Applying `rubric`, what is the severity of the WORST defect in the `diff`? If there is no defect at all, choose the lowest level.",
    criteria: [
      "No defect, or only low: preference, naming, formatting, tidiness.",
      "Medium: a real weakness with no user-visible consequence — a stale comment, a leaky abstraction, an inefficiency, a convention broken without harm.",
      "High: the ticket's stated behaviour is not delivered or delivered wrongly; a stated acceptance criterion is unmet or untested; a user-reachable edge-case bug; an assertion that cannot fail.",
      "Critical: data loss, a security hole, or a defect that reaches the user on the main path.",
    ],
  },
};
const GATE = ["unmet_criterion", "untested_criterion", "vacuous_assertion", "reachable_bug", "out_of_scope", "unrequested_file"];

// ---- api: plain fetch, no SDK ----
async function ask(state: unknown): Promise<any> {
  let last = "";
  for (let attempt = 0; attempt < 4; attempt++) {
    if (attempt) await Bun.sleep(500 * 2 ** attempt);
    let r: Response;
    try {
      r = await fetch("https://api.typesafe.ai/v1/systemone", {
        method: "POST",
        headers: { Authorization: `Bearer ${KEY}`, "Content-Type": "application/json" },
        body: JSON.stringify({ state, model: MODEL, questions: QUESTIONS }),
        signal: AbortSignal.timeout(30_000),
      });
    } catch (e: any) { last = String(e?.message ?? e); continue; }
    if (r.ok) return r.json();
    last = `HTTP ${r.status}: ${(await r.text()).slice(0, 400)}`;
    if (!(r.status === 429 || r.status >= 500)) break;   // 4xx other than 429: don't retry
  }
  throw new Error(last);
}

// ---- duet run readers, all read-only ----
const git = (args: string[]) => Bun.spawnSync(["git", ...args], { stdout: "pipe", stderr: "pipe" });
const NOISE = /(\.lock$|pnpm-lock\.yaml$|package-lock\.json$|\.snap$|openapi[^/]*\.(json|ya?ml)$|\.pbxproj$|\.xcodeproj\/)/;

function diffFor(repo: string, base: string, head: string): string | null {
  const names = git(["-C", repo, "diff", "--name-only", `${base}..${head}`]);
  if (names.exitCode !== 0) return null;
  const files = names.stdout.toString().split("\n").filter(f => f && !NOISE.test(f));
  const d = git(["-C", repo, "diff", `${base}..${head}`, "--", ...files]);
  return d.exitCode === 0 ? d.stdout.toString() : null;
}

function ticketText(run: any, num: string): string | null {
  const dir = join(run.inputsDir ?? join(DUET, "runs", run.id, "inputs"), "issues");
  if (!existsSync(dir)) return null;
  const f = readdirSync(dir).find(n => n.startsWith(num + "-") && n.endsWith(".md"));
  return f ? readFileSync(join(dir, f), "utf8") : null;
}

// what the FIRST reviewer of this ticket saw and said (review-1 unless an earlier round died before review)
function round1(rid: string, num: string) {
  const jobs = join(DUET, "runs", rid, "jobs", num);
  if (!existsSync(jobs)) return null;
  const first = readdirSync(jobs).filter(d => /^review-\d+$/.test(d)).sort((a, b) => +a.slice(7) - +b.slice(7))
    .find(d => existsSync(join(jobs, d, "prompt.md")) && existsSync(join(jobs, d, "reply.json")));
  if (!first) return null;
  const j = join(jobs, first);
  const p = join(j, "prompt.md"), rp = join(j, "reply.json");
  const prompt = readFileSync(p, "utf8");
  const base = prompt.match(/^Base commit: ([0-9a-f]{40})/m)?.[1];
  const head = prompt.match(/^Head commit: ([0-9a-f]{40})/m)?.[1];
  let reply: any; try { reply = JSON.parse(readFileSync(rp, "utf8")); } catch { return null; }
  if (!base || !head) return null;
  const sevs: string[] = (reply.findings ?? []).map((f: any) => f.severity);
  return { review: first, base, head, verdict: reply.verdict, sevs, blocking: sevs.some(s => s === "critical" || s === "high") };
}

// ---- main ----
mkdirSync(OUT, { recursive: true });
const rows: any[] = [];
const jobs: (() => Promise<void>)[] = [];
for (const rid of readdirSync(join(DUET, "runs")).sort()) {
  const sj = join(DUET, "runs", rid, "state.json");
  if (!existsSync(sj)) continue;
  if (only.length && !only.some(o => rid.includes(o))) continue;
  const run = JSON.parse(readFileSync(sj, "utf8"));
  if (run.phase !== "done") { console.error(`skip ${rid}: phase=${run.phase}`); continue; }
  if (!run.repo || !existsSync(run.repo)) { console.error(`skip ${rid}: repo missing`); continue; }
  for (const num of Object.keys(run.tickets).sort()) jobs.push(async () => {
    const r1 = round1(rid, num); if (!r1) return;
    const ticket = ticketText(run, num); if (!ticket) { console.error(`${rid}/${num}: no ticket text`); return; }
    let diff = diffFor(run.repo, r1.base, r1.head); if (!diff) { console.error(`${rid}/${num}: diff unavailable`); return; }
    const truncated = diff.length > MAX_DIFF_BYTES;
    if (truncated) diff = diff.slice(0, MAX_DIFF_BYTES) + "\n[... diff truncated for size ...]";
    const truthMark = r1.blocking ? "SENT-BACK" : "approved ";
    if (DRY) { console.log(`dry  ${truthMark}  ${rid}/${num}  diff=${(diff.length / 1024) | 0}KB ticket=${(ticket.length / 1024) | 0}KB${truncated ? " [truncated]" : ""}`); return; }

    const t0 = Date.now();
    let res: any;
    try { res = await ask({ ticket, rubric: RUBRIC, diff }); } catch (e: any) { console.error(`${rid}/${num}: ${e.message}`); return; }
    const a = res.answers ?? {};
    const nouls: Record<string, number> = {};
    for (const k of Object.keys(QUESTIONS)) if (a[k]?.type === "noul") nouls[k] = a[k].noul;
    const fired = GATE.filter(k => nouls[k] > FIRE);
    const sev = a.worst_severity ?? {};
    const row = {
      run: rid, ticket: num, repo: run.repo.split("/").slice(-2).join("/"),
      truth: { review: r1.review, verdict: r1.verdict, blocking: r1.blocking, sevs: r1.sevs },
      nouls, fired, holistic: nouls.should_revise,
      severity: { score: sev.score, confidence: sev.confidence, probabilities: sev.probabilities },
      usage: res.usage, model: res.model, ms: Date.now() - t0, truncated,
    };
    rows.push(row);
    writeFileSync(join(OUT, `${rid}__${num}.json`), JSON.stringify({ ...row, answers: a }, null, 2));
    console.log(`${truthMark}  ${rid}/${num}  gate=${fired.length ? "FIRE(" + fired.join(",") + ")" : "pass"}  holistic=${nouls.should_revise?.toFixed(2)}  sev=${sev.score?.toFixed(2)}±${sev.confidence?.toFixed(2)}  ${row.ms}ms${truncated ? " [truncated]" : ""}`);
  });
}

let i = 0;   // 4 in flight; well under the 1,200 req/min limit
await Promise.all(Array.from({ length: 4 }, async () => { while (i < jobs.length) await jobs[i++](); }));
if (DRY) { console.log(`\n${jobs.length} tickets would be sent.`); process.exit(0); }

// ---- summary: does Jev agree with the reviewer? ----
const pos = rows.filter(r => r.truth.blocking), neg = rows.filter(r => !r.truth.blocking);
const mean = (xs: number[]) => (xs = xs.filter(Number.isFinite)).length ? xs.reduce((a, b) => a + b, 0) / xs.length : NaN;
console.log(`\n=== ${rows.length} tickets: ${pos.length} sent back by the reviewer (crit/high), ${neg.length} approved ===`);
console.log(`per-question gate @${FIRE}:      caught ${pos.filter(r => r.fired.length).length}/${pos.length} send-backs, false-fired on ${neg.filter(r => r.fired.length).length}/${neg.length} approved`);
console.log(`holistic should_revise @${FIRE}: caught ${pos.filter(r => r.holistic > FIRE).length}/${pos.length} send-backs, false-fired on ${neg.filter(r => r.holistic > FIRE).length}/${neg.length} approved`);
console.log(`\nmean noul, sent-back vs approved (a bigger gap = a more useful question):`);
for (const k of [...GATE, "should_revise"]) {
  const mp = mean(pos.map(r => r.nouls[k])), mn = mean(neg.map(r => r.nouls[k]));
  console.log(`  ${k.padEnd(20)} sent-back ${mp.toFixed(2)}   approved ${mn.toFixed(2)}   gap ${(mp - mn).toFixed(2)}`);
}
console.log(`severity score (0 low · 1 medium · 2 high · 3 critical): sent-back ${mean(pos.map(r => r.severity.score)).toFixed(2)}   approved ${mean(neg.map(r => r.severity.score)).toFixed(2)}`);
const tok = rows.reduce((a, r) => a + (r.usage?.input_tokens ?? 0), 0);
console.log(`\n${tok} input tokens ≈ $${(tok * 0.042 / 1e6).toFixed(4)}.  Per-ticket detail in ${OUT}/`);
writeFileSync(join(OUT, "summary.json"), JSON.stringify(rows, null, 2));
