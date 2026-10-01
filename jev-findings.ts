#!/usr/bin/env bun
// jev-findings.ts — can Jev grade a reviewer's FINDING (short text) the way the reviewer did?
// Reads every labelled finding across finished duet runs, hides the label, asks Jev for
// severity three ways, and reports agreement. Read-only. Usage: bun ~/.duet/jev-findings.ts [--dry]
import { readFileSync, existsSync, readdirSync, mkdirSync, writeFileSync } from "fs";
import { join } from "path";

const DUET = process.env.DUET_HOME ?? join(process.env.HOME!, ".duet");
const OUT = join(DUET, "jev-findings");
const MODEL = "jev-1.13.0";
const DRY = process.argv.includes("--dry");
const LEVELS = ["low", "medium", "high", "critical"] as const;

const envPath = join(DUET, ".env");
if (existsSync(envPath)) for (const line of readFileSync(envPath, "utf8").split("\n")) {
  const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$/);
  if (m && !process.env[m[1]]) process.env[m[1]] = m[2].replace(/^["']|["']$/g, "");
}
const KEY = process.env.TYPESAFE_API_KEY;
if (!KEY && !DRY) { console.error(`TYPESAFE_API_KEY not set in ${envPath}`); process.exit(2); }

const RUBRIC = `critical: data loss, a security hole, or a defect that reaches the user on the main path.
high: the ticket's stated behaviour is not delivered, or is delivered wrongly; a stated acceptance criterion is unmet; a bug in an edge case a user can reach; an assertion that cannot fail, or a branch carrying a stated criterion with no test at all; evidence the ticket owes that was never produced.
medium: a real weakness with no user-visible consequence — a stale comment, a leaky abstraction, an inefficiency, a convention broken without harm.
low: preference, naming, formatting, tidiness.`;

const CRITERIA = {
  low: "Preference, naming, formatting, tidiness. No functional consequence.",
  medium: "A real weakness with no user-visible consequence: a stale comment, a leaky abstraction, an inefficiency, a convention broken without harm.",
  high: "The ticket's stated behaviour is not delivered or is delivered wrongly; a stated acceptance criterion is unmet or has no test that could fail; a bug in an edge case a user can reach; evidence the ticket owes was never produced.",
  critical: "Data loss, a security hole, or a defect that reaches the user on the main path.",
};

const QUESTIONS = {
  // ordered scale
  severity_score: { type: "score", instructions: "Applying `rubric`, how severe is the defect described in `finding`?",
    criteria: LEVELS.map(l => `${l}: ${CRITERIA[l]}`) },
  // categorical, same labels
  severity_choice: { type: "choice", instructions: "Applying `rubric`, which severity label does the defect described in `finding` deserve?",
    criteria: CRITERIA },
  // the binary the duet gate actually uses
  blocks: { type: "noul", instructions: "Applying `rubric`, is the defect described in `finding` critical or high — serious enough to send the ticket back to the builder?",
    criteria: { true: "It is critical or high: behaviour not delivered, a criterion unmet or untestable, a reachable bug, a security hole, or data loss.",
                false: "It is medium or low: a weakness with no user-visible consequence, or a matter of preference." } },
};

async function ask(state: unknown): Promise<any> {
  let last = "";
  for (let attempt = 0; attempt < 4; attempt++) {
    if (attempt) await Bun.sleep(500 * 2 ** attempt);
    let r: Response;
    try {
      r = await fetch("https://api.typesafe.ai/v1/systemone", {
        method: "POST", headers: { Authorization: `Bearer ${KEY}`, "Content-Type": "application/json" },
        body: JSON.stringify({ state, model: MODEL, questions: QUESTIONS }), signal: AbortSignal.timeout(30_000),
      });
    } catch (e: any) { last = String(e?.message ?? e); continue; }
    if (r.ok) return r.json();
    last = `HTTP ${r.status}: ${(await r.text()).slice(0, 300)}`;
    if (!(r.status === 429 || r.status >= 500)) break;
  }
  throw new Error(last);
}

function ticketText(run: any, num: string): string {
  const dir = join(run.inputsDir ?? join(DUET, "runs", run.id, "inputs"), "issues");
  const f = existsSync(dir) ? readdirSync(dir).find(n => n.startsWith(num + "-") && n.endsWith(".md")) : undefined;
  return f ? readFileSync(join(dir, f), "utf8") : "";
}

// ---- collect every labelled finding ----
type Sample = { run: string; ticket: string; review: string; id: string; label: string; finding: any; ticketText: string };
const samples: Sample[] = []; const seen = new Set<string>();
for (const rid of readdirSync(join(DUET, "runs")).sort()) {
  const sj = join(DUET, "runs", rid, "state.json"); if (!existsSync(sj)) continue;
  const run = JSON.parse(readFileSync(sj, "utf8")); if (run.phase !== "done") continue;
  for (const num of Object.keys(run.tickets).sort()) {
    const jobs = join(DUET, "runs", rid, "jobs", num); if (!existsSync(jobs)) continue;
    const tt = ticketText(run, num);
    for (const rv of readdirSync(jobs).filter(d => /^review-\d+$/.test(d)).sort()) {
      const rp = join(jobs, rv, "reply.json"); if (!existsSync(rp)) continue;
      let reply: any; try { reply = JSON.parse(readFileSync(rp, "utf8")); } catch { continue; }
      for (const f of reply.findings ?? []) {
        if (!LEVELS.includes(f.severity)) continue;
        const { severity, id, ...rest } = f;                       // hide the label
        const key = `${rid}|${num}|${severity}|${rest.evidence}`;   // same finding re-confirmed on re-review counts once
        if (seen.has(key)) continue; seen.add(key);
        samples.push({ run: rid, ticket: num, review: rv, id: id ?? "?", label: severity, finding: rest, ticketText: tt });
      }
    }
  }
}
const byLabel = (xs: Sample[]) => Object.fromEntries(LEVELS.map(l => [l, xs.filter(s => s.label === l).length]));
console.log(`${samples.length} labelled findings:`, byLabel(samples));
if (DRY) process.exit(0);

// ---- ask ----
mkdirSync(OUT, { recursive: true });
const rows: any[] = []; let i = 0;
await Promise.all(Array.from({ length: 4 }, async () => { while (i < samples.length) {
  const s = samples[i++];
  let res: any; try { res = await ask({ ticket: s.ticketText, rubric: RUBRIC, finding: s.finding }); }
  catch (e: any) { console.error(`${s.run}/${s.ticket}/${s.id}: ${e.message}`); continue; }
  const a = res.answers;
  const row = { ...s, ticketText: undefined,
    score: a.severity_score?.score, score_conf: a.severity_score?.confidence, score_probs: a.severity_score?.probabilities,
    choice: a.severity_choice?.choice, choice_conf: a.severity_choice?.confidence, blocks: a.blocks?.noul, usage: res.usage };
  rows.push(row);
  const scoreLabel = LEVELS[Math.max(0, Math.min(3, Math.round(row.score)))];
  console.log(`${s.label.padEnd(8)} → choice=${(row.choice ?? "?").padEnd(8)}(${row.choice_conf?.toFixed(2)}) score=${row.score?.toFixed(2)}→${scoreLabel.padEnd(8)} blocks=${row.blocks?.toFixed(2)}  ${s.run.slice(11)}/${s.ticket}/${s.id}`);
}}));
writeFileSync(join(OUT, "results.json"), JSON.stringify(rows, null, 2));

// ---- scorecard ----
const isBlock = (l: string) => l === "critical" || l === "high";
const n = rows.length;
const choiceExact = rows.filter(r => r.choice === r.label).length;
const scoreExact = rows.filter(r => LEVELS[Math.max(0, Math.min(3, Math.round(r.score)))] === r.label).length;
const scoreOffByOne = rows.filter(r => Math.abs(LEVELS.indexOf(r.label) - Math.round(r.score)) <= 1).length;
console.log(`\n=== ${n} findings ===`);
console.log(`4-way exact agreement:   choice ${choiceExact}/${n} (${(100 * choiceExact / n).toFixed(0)}%)   score ${scoreExact}/${n} (${(100 * scoreExact / n).toFixed(0)}%)   score within one level ${scoreOffByOne}/${n}`);
for (const [name, pred] of [["noul blocks>0.5", (r: any) => r.blocks > 0.5], ["choice∈{crit,high}", (r: any) => isBlock(r.choice)]] as const) {
  const tp = rows.filter(r => isBlock(r.label) && pred(r)).length, fn = rows.filter(r => isBlock(r.label) && !pred(r)).length;
  const fp = rows.filter(r => !isBlock(r.label) && pred(r)).length, tn = rows.filter(r => !isBlock(r.label) && !pred(r)).length;
  console.log(`blocking via ${name.padEnd(20)} recall ${tp}/${tp + fn}  false-alarm ${fp}/${fp + tn}  accuracy ${(100 * (tp + tn) / n).toFixed(0)}%`);
}
console.log(`\nmean 'blocks' noul by reviewer label (should climb):`);
for (const l of LEVELS) { const xs = rows.filter(r => r.label === l).map(r => r.blocks); if (xs.length) console.log(`  ${l.padEnd(8)} ${(xs.reduce((a, b) => a + b, 0) / xs.length).toFixed(2)}   (n=${xs.length})`); }
console.log(`\nconfusion (rows = reviewer, cols = jev choice):`);
console.log("          " + LEVELS.map(l => l.padStart(9)).join(""));
for (const l of LEVELS) console.log(l.padEnd(10) + LEVELS.map(c => String(rows.filter(r => r.label === l && r.choice === c).length).padStart(9)).join(""));
const tok = rows.reduce((a, r) => a + (r.usage?.input_tokens ?? 0), 0);
console.log(`\n${tok} tokens ≈ $${(tok * 0.042 / 1e6).toFixed(4)}.  Detail: ${OUT}/results.json`);
