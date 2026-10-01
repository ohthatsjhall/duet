# duet

A two-agent build supervisor. One agent builds, the other reviews, ordinary code
owns the loop. Runs a Pocock-style spec + tickets to a reviewed, integrated branch
with no human relaying between Claude Code and Codex.

## Layout

    ~/.duet/duet.ts          the supervisor (bun, single file, no dependencies)
    ~/.duet/config.json      global config: lastBuilder (alternation), default models, timeout
    ~/.duet/runs/<run-id>/   one directory per run
        state.json           authoritative state (tickets, decisions, phase, SHAs)
        events.jsonl         append-only log, feed it to /retro
        inputs/              frozen spec, tickets, CONTEXT.md — what the agents were given
        jobs/<ticket>/       every agent call: prompt.md, schema.json, command.json, stdout, reply.json, check logs
        decisions/D-NN.md    questions only the owner can answer
        integration/         worktree on the integration branch duet/<slug>-xxxx (until teardown)
        wt/<ticket>/         per-ticket worktrees (removed after merge)
        artifacts/<ticket>/  what the ticket produced but did not commit, kept before its
                             worktree is deleted (see `artifacts` in the repo config)
    ~/.local/bin/duet        shim
    ~/.agents/skills/duet    the /duet skill, symlinked into ~/.claude/skills and ~/.codex/skills

## Flow

    grill-with-docs → to-spec → to-tickets            (you + Claude, one context window)
    duet start --spec <spec.md>                        (the supervisor, hands off)
      per ticket, blockers first, N in parallel:
        builder: implement (tdd) → check → simplify → commit
        supervisor: runs the check command itself
        reviewer: fresh read-only session, code-review (standards + spec), JSON verdict
        REVISE → builder fixes → check → fresh review   (max 2 rounds)
        APPROVED → serialised merge into the integration branch → check again
        no convergence → record findings, merge the green ticket, carry them forward
        no safe reversible product choice → decision file, other tickets continue
      when all merged:
        builder: reconcile carried critical/high findings on the combined tree
        builder: ponytail-review (if installed) → simplify over the whole branch → check
        reviewer: authoritative whole-branch review → fix rounds (max 2)
        optional: push, draft PR
    duet teardown <run-id>                              (after a successful run)
      verify recorded heads, clean worktrees and merged ticket branches
      retain the integration branch and compact audit record
    you: merge the retained integration branch

## Roles

Fixed per run. Default alternates the builder from the previous run (config.json
`lastBuilder`), so both models build and both review over time. Override for one
run with `--builder` and `--reviewer`. The selected roles and model identifiers
are printed by `duet plan` and frozen into the run state.

A ticket can override the run with a `**Builder:** codex` line; the other agent
then reviews that ticket. The final whole-branch review still goes to the run's
reviewer, who in a mixed run will have built some tickets, so use overrides when
the per-ticket split matters more than a fully independent final pass.

## Per-repo config (.duet.json, optional)

    {
      "check": "swift build && swift test",
      "checkFinal": "",
      "writableDirs": ["$HOME/.cache/shared-build"],
      "codexSandbox": "danger-full-access",
      "checkAfterMerge": true,
      "maxRounds": 3,
      "ticketReviewPolicy": "carry-to-final",
      "parallel": 1,
      "timeoutMinutes": 45,
      "push": false,
      "pr": false,
      "extraInstructions": "appended to every agent prompt within its assigned role"
    }

`checkFinal` is appended to `check` (`check && checkFinal`) only in the final
phase, after every ticket has merged — the place for gates too slow to repeat on
every build, merge and fix round, such as a release-configuration build. Override
per run with `--check-final "<cmd>"`.

`worktreeFiles` lists gitignored files (local `.env` files, mostly) copied from the main checkout
into every worktree, so a builder can boot the app it must verify in a browser. A listed file that is
not gitignored is skipped, because the supervisor commits whatever a builder leaves behind.

UI tickets owe browser evidence. The builder follows `impeccable` for the interface, verifies the
changed flow with `agent-browser`, and saves screenshots under `.duet-evidence/` (gitignore it, and
list it in `artifacts`). The read-only reviewer judges those screenshots; a UI change without them is
HIGH. Claude builders start fresh tickets with the `/implement` slash command, because `implement`
is a user-only skill that Claude will not call through its Skill tool.

`writableDirs` lists paths outside the worktree the builder must write for the
check to run: shared build caches, toolchain state, simulator state. `~` and
`$HOME` are expanded. Without them a sandboxed builder cannot run the very
command it is judged by. `codexSandbox` overrides the Codex builder's sandbox
(default `workspace-write`); a check that needs OS services rather than only
files, an iOS simulator for instance, wants `danger-full-access`.

`jevDisputeThreshold` (default 0.8) enables a second opinion on review severity when
`TYPESAFE_API_KEY` is set in the environment or in `~/.duet/.env`. After every review,
each medium/low finding is sent to TypeSafe's Jev model with the ticket and the
severity rubric; a finding Jev rates above the threshold as blocking is recorded as
*disputed* in `jobs/<ticket>/review-N/jev.json`, `events.jsonl` and the ticket's
comments. Nothing else changes: no verdict moves, no round is added. Measured on
291 labelled findings, Jev never under-rated a high and above 0.8 flagged no lows,
so a dispute means "look again", never "this was fine". Without a key it is off.

Without `check`, duet detects one from the repo (Package.swift, bun.lock,
package.json, Cargo.toml, go.mod, pyproject.toml, Gemfile).

`ticketReviewPolicy` defaults to `carry-to-final`. Per-ticket critical/high
findings still receive automatic repair rounds, but a green ticket that does not
converge is integrated with its findings attached. The builder reconciles those
findings once all tickets are present, and the final whole-branch review remains
blocking. Set it to `blocking` only when per-ticket non-convergence should stop
for an owner decision.

Treat `.duet.json` as durable repository policy: verification, shared writable
directories, sandbox requirements, parallelism imposed by shared resources,
timeouts, publishing policy, artifacts, review rounds, and instructions that
should apply to every spec. Pass run-specific choices on the command line:

    duet plan --spec <spec.md> --builder claude --reviewer codex \
      --claude-model <model> --codex-model <model>
    duet start --spec <spec.md> --builder claude --reviewer codex \
      --claude-model <model> --codex-model <model>

The base ref and one-off push or PR decisions also belong on the command line.
Repository role or model defaults remain supported when they are intentional
policy for every run, but should not be committed merely for one spec.

## Teardown

After `duet status <run-id>` reports `phase=done`, run:

    duet teardown <run-id>

Teardown removes the completed run's integration worktree, any remaining ticket
worktrees, and verified ticket branches already contained in the integration
head. It preserves the integration branch, state, events, frozen inputs,
decisions, job and review logs, and harvested artifacts. The command is
idempotent and records what it removed in `state.json`.

There is deliberately no force mode. Teardown refuses a live or incomplete run,
a dirty or moved worktree, an unexpected path or branch, a moved integration
head, or a ticket branch that is not contained in the integration branch. Fix or
preserve that state explicitly rather than deleting around the guard.

## Guarantees the script enforces

- The builder never grades; the reviewer never edits (its worktree is reset if it does).
- Only `critical` and `high` findings send a ticket back. `medium` and `low` are recorded
  against the ticket and carried to the final review. The builder is told, per round, which
  findings to fix and which to leave alone: fixing notes rewrites the diff, and a rewritten
  diff is a fresh surface for the next review to find fresh defects in.
- A green ticket that exhausts its automatic review rounds is merged with the exact review
  head and findings recorded. Its dependencies can continue. Carried critical/high findings
  go to a final reconciliation build, and every carried item is shown to the final reviewer.
- Builders choose the simplest reversible implementation supported by the spec and existing
  contracts. They request owner input only when no safe reversible path exists; missing tests,
  documentation, assertions, refactors, and ordinary implementation choices are engineering work.
- A re-review is incremental: it confirms the prior blocking findings are discharged and reads
  what changed since it last looked. Newly discovered defects keep their actual severity,
  including critical/high, even in unchanged code. A check failure does not count as an
  independent review; the first reviewer still examines the complete ticket diff.
- The builder runs targeted tests while working and the full chain once, at the end. The
  supervisor's run is the authoritative one. The post-merge check is skipped when the ticket
  branched from the current integration head and nothing landed in between, since the gate
  check already proved that tree.
- A verdict is tied to an exact head SHA; any new commit needs a new review. If the final
  reviewer changes the checkout or HEAD, its verdict is discarded, the checked commit is
  restored, and the run stops for fresh verification and review on resume.
- APPROVED with critical/high findings is treated as REVISE. REVISE with no findings is BLOCKED.
  REVISE with only medium/low notes becomes APPROVED with those notes recorded.
- Tests only count when the supervisor ran them on a clean checkout whose HEAD and tracked
  files remain unchanged. Failed fallback commits stop before review and preserve the work.
  Builder reports are context.
- The reviewer is shown the builder's own `checks_run` and `deviations`, after its own reading,
  as claims to verify. Some of what a ticket owes — a mutation run and reverted, a capture taken —
  leaves no trace in the tree by design, and a reviewer who cannot see the claim reports it as
  missing. A claim shown to be false is itself a high finding.
- Merges are serialised. A merge that turns the integration check red is undone.
- Gitignored tickets are snapshotted before worktrees exist, so every worker can read them.
- An answer is written beside its decision as well as into the run's state, so answering
  while a supervisor is still running cannot be lost when that supervisor next saves.
- Final verification repairs have their own bounded attempt counter, separate from completed
  review rounds. `maxRounds` applies on both start and resume. Final check/fix logs use unique
  attempt numbers; retrying simplify preserves the earlier attempt logs.
- A failed or blocked final simplify/fix stops the run and preserves partial work. A successful
  agent process alone is insufficient: its structured result must also report completion.
- Every transition is saved; `duet resume <id>` continues after a crash, stop, or answered decision.
  A resume re-reads `.duet.json` for the check, writable dirs, sandbox, models, timeout and
  builder instructions, so a config fix applies to the run that needed it. Add `--retry-failed`
  to revive tickets whose agent call died rather than whose work was wrong.
- One supervisor per run, enforced by a pid lock: a second refuses to start, a stale one is taken over.
- Completed-run teardown verifies every destructive target before removing any of them, retains the
  integration branch and audit record, and can be repeated safely.

## Not enforced, by design

- Merging the integration branch into your mainline. That stays yours.
- Whether a final whole-branch review that cannot converge should be accepted or receive
  additional guidance. That remains the owner's call; Duet presents it once after all runnable
  ticket work and automatic repair rounds are complete.
- Anything about the agents' own sandboxes beyond the flags passed: the builder runs with
  full permissions inside its worktree; the reviewer is read-only.

## Regression tests

Run `python3 ~/.duet/duet_regression_test.py` (requires Python 3, Bun and Git).
The suite invokes the real CLI in disposable Git repositories, substitutes fake
Codex/Claude executables, and uses deterministic check commands. It never calls
a model provider or connects to a project database. Each fixture has its own
DUET_HOME, Git configuration and process group.

Set `DUET_UNDER_TEST` to an alternate supervisor path to test a staged copy.
The tests cover advisory and strict ticket review policies, carried-finding reconciliation,
the blocking final gate, genuine owner decisions, retry limits, independent review instructions,
commit/verification integrity, failed final passes, resume behavior, explicit model plans,
successful completion with both agent adapters, and guarded teardown.

A run in `stopped` state has paused its own automation. It does not stop application
servers or unrelated sessions. Inspect the recorded reason, address it, then
resume. Exhausted retry limits require explicit additional guidance/configuration;
resume does not silently reset the attempt budget.
