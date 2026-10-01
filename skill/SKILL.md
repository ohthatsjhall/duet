---
name: duet
description: "Drive a spec and its tickets to a reviewed, integrated branch with two agents: one builds, the other reviews. Use after /to-tickets when the user wants the build to run to completion without relaying between Claude and Codex."
argument-hint: "<path to spec.md> [--builder claude|codex] [--parallel N] [--push] [--pr]"
disable-model-invocation: true
---

`duet` is a supervisor script, not a conversation. It runs every ticket through build → verify → independent review → merge, then reconciles carried findings and runs final simplify → verify → whole-branch review. Per-ticket review is advisory after its automatic repair budget; the final whole-branch review is the blocking quality gate. Duet asks the owner only when no safe reversible choice exists; exhausted mechanical failures stop with diagnostics rather than pretending to be product decisions.

Roles are fixed per run and alternate between runs. Whoever builds never grades.

## Steps

1. Confirm the spec has a tickets directory next to it (`issues/NN-slug.md`, Pocock format with `**Blocked by:**` and `**Status:**` lines). If not, run `/to-tickets` first.
2. Show the plan without running anything:

       duet plan --spec <spec.md>

   Check the frontier and the detected verification command. If the command is wrong, add `"check"` to `.duet.json` at the repo root or pass `--check "<cmd>"`.
3. Start the run in the background so this session stays free. Report the run id to the user:

       duet start --spec <spec.md> [flags]

4. Do not implement tickets yourself while a run is active on them. Check progress with `duet status <run-id>`; the events log is `~/.duet/runs/<run-id>/events.jsonl`.
5. Do not surface ordinary engineering work as an owner decision. Duet should make the simplest reversible choice supported by the spec and repository, record material assumptions, and continue. Missing tests, documentation, contract assertions, refactors, and implementation details are fixed automatically or carried to the final gate. If the run stops because no safe reversible choice exists—such as contradictory product behavior or public contracts, destructive data semantics, privacy/security policy, external spending or side effects, or missing credentials—show the user the decision file under `~/.duet/runs/<run-id>/decisions/`, get their answer, record it, and resume:

       duet answer <run-id> D-01 "<answer>"
       duet resume <run-id>

6. A green ticket with unresolved review findings is integrated with those findings recorded and carried forward; it does not stop the run. After all tickets merge, the builder reconciles carried critical/high findings against the combined tree, and the reviewer re-evaluates them during the authoritative whole-branch review. If that final gate cannot converge within its repair budget, present the single consolidated final review to the user. When the run is done, tell the user the integration branch name and that merging it is theirs to do. Review findings and verdicts are already appended to each ticket's `## Comments`.
7. After reporting the completed result, run:

       duet teardown <run-id>

   Teardown is allowed only for a fully completed run. It removes the run's derived worktrees and
   merged per-ticket branches, which are the main disk cost, while preserving the integration branch,
   run state, event log, inputs, decisions, review logs, harvested artifacts, and committed project
   files. If it refuses because a worktree is dirty, a branch is unmerged, recorded state moved, or a
   supervisor is live, report that condition and preserve the files; do not force-delete around it.

## Configuration lifetime

Keep `.duet.json` for repository-wide defaults that should apply to every run: verification commands,
post-merge checks, external writable cache directories, sandbox policy, parallelism imposed by shared
test resources, timeouts, push/PR policy, artifact paths, review rounds, and durable repository
instructions. `ticketReviewPolicy` is also durable workflow policy: `carry-to-final` is the default;
use `blocking` only when every ticket needs owner intervention after review non-convergence.

Pass choices made for one spec or run as launch flags so they are frozen into that run's state without
changing later runs:

    duet plan --spec <spec.md> --builder claude --reviewer codex --claude-model <model> --codex-model <model>
    duet start --spec <spec.md> --builder claude --reviewer codex --claude-model <model> --codex-model <model>

Builder/reviewer roles, model pins, the base ref, and one-off push/PR choices normally belong on the
command line. Put a role or model in `.duet.json` only when it is a deliberate repository-wide policy,
not merely the choice for the current spec. The plan prints both selected roles and model identifiers;
verify them before starting.

## Choosing the builder

Roles are per run: one builder, the other agent reviews, alternating between runs by default. Override for a run with `--builder claude|codex`, or for one ticket by adding a `**Builder:** codex` line under the ticket's `**Blocked by:**` line when running `/to-tickets`. A ticket with an override is still reviewed by the other agent.
