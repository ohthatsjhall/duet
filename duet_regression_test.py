"""Run: python3 duet_regression_test.py. Uses only disposable repos and fake agents."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import unittest

SUPERVISOR = Path(os.environ.get('DUET_UNDER_TEST', Path(__file__).with_name('duet.ts'))).resolve()
BUN = shutil.which('bun')

FAKE_AGENT = r'''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
p = pathlib.Path
prompt = sys.stdin.read()
scenario = os.environ.get('DUET_TEST_SCENARIO', '')
review = 'You are the INDEPENDENT REVIEWER.' in prompt
simplify = 'doing the final quality pass' in prompt
final = p.cwd().name == 'integration'
log = p(os.environ['DUET_TEST_CALLS'])
with log.open('a') as f:
    f.write(json.dumps({'review':review, 'final':final, 'simplify':simplify, 'prompt':prompt, 'args':sys.argv, 'secret':p('secret.env').exists(), 'bg_disabled':os.environ.get('CLAUDE_CODE_DISABLE_BACKGROUND_TASKS')=='1'})+'\n')
if review:
    result = {'verdict':'APPROVED','summary':'Acceptance inspected','findings':[], 'coverage':['fixture acceptance'], 'limitations':[]}
    if scenario == 'empty-revise': result['verdict'] = 'REVISE'
    if (scenario == 'revise-once' and not final) or (scenario == 'final-revise-once' and final):
        prior=[json.loads(line) for line in log.read_text().splitlines()]
        if len([r for r in prior if r['review'] and r['final']==final])==1:
            result['verdict']='REVISE'
            result['findings']=[{'id':'F1','severity':'high','axis':'spec','path':'behavior.txt','evidence':'Fixture correction required','fix':'Correct fixture'}]
    if scenario == 'critical':
        if '--json-schema' in sys.argv:
            schema=json.loads(sys.argv[sys.argv.index('--json-schema')+1])
        else:
            schema=json.loads(p(sys.argv[sys.argv.index('--output-schema')+1]).read_text())
        if 'critical' not in schema['properties']['findings']['items']['properties']['severity']['enum']:
            sys.exit(2)
        result['findings']=[{'id':'F1','severity':'critical','axis':'spec','path':'behavior.txt','evidence':'Fixture privacy leak','fix':'Remove leak'}]
    if scenario == 'medium':
        result['verdict'] = 'REVISE'
        result['findings'] = [{'id':'F1','severity':'medium','axis':'standards','path':'behavior.txt','evidence':'Comment could be clearer','fix':'Clarify comment'}]
else:
    if not final:
        p('behavior.txt').write_text('implemented\n')
        if scenario == 'semantic-conflict': p(f'part-{p.cwd().name}.txt').write_text('ticket part\n')
        subprocess.run(['git','add','-A'],check=True,stdout=subprocess.DEVNULL)
        subprocess.run(['git','commit','--allow-empty','-qm','Implement fixture'],check=True,stdout=subprocess.DEVNULL)
    if scenario == 'leaves-server' and not final:
        server=subprocess.Popen(['sleep','300'],cwd=os.getcwd(),start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        p(os.environ['DUET_TEST_CALLS']).with_name('server.pid').write_text(str(server.pid))
    if scenario == 'semantic-conflict' and not final and 'FIX ONLY THESE' in prompt:
        if 'INTEGRATE' in prompt.split('FIX ONLY THESE')[1].split('DO NOT FIX')[0]:
            p('resolved.txt').write_text('reconciled with integration\n')
            subprocess.run(['git','add','resolved.txt'],check=True,stdout=subprocess.DEVNULL)
            subprocess.run(['git','commit','-qm','Resolve semantic conflict'],check=True,stdout=subprocess.DEVNULL)
    if scenario == 'commit-fails' and not final:
        p('behavior.txt').write_text('uncommitted correction\n')
        common=subprocess.check_output(['git','rev-parse','--git-common-dir'],text=True).strip()
        hook=p(common)/'hooks'/'pre-commit'
        hook.write_text('#!/bin/sh\nexit 1\n'); hook.chmod(0o755)
    result = {'status':'completed','summary':'Fixture built','commits':[], 'checks_run':[], 'decision_requests':[], 'deviations':[]}
    if scenario == 'owner-decision' and not final:
        result['status']='blocked'
        result['summary']='No safe reversible choice exists'
        result['decision_requests']=[{'id':'D','title':'Choose retention policy','question':'Which irreversible retention policy applies?','options':['A','B'],'recommendation':'A','blocks_ticket':True}]
if simplify and scenario.startswith('simplify-'):
    p('simplify-note.txt').write_text('preserved partial work\n')
    if scenario == 'simplify-cli-fails': sys.exit(1)
    result['status'] = 'blocked' if scenario == 'simplify-blocked' else 'failed'
    result['summary'] = 'Fixture simplify did not complete'
if review and final and scenario == 'reviewer-commits':
    p('behavior.txt').write_text('unreviewed change\n')
    subprocess.run(['git','add','behavior.txt'],check=True)
    subprocess.run(['git','commit','-qm','Unexpected reviewer edit'],check=True)
if final and not review and not simplify and scenario == 'fix-failed':
    result['status']='failed'; result['summary']='Fixture final repair failed'
if p(sys.argv[0]).name == 'codex':
    p(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps(result))
    print(json.dumps({'type':'thread.started','thread_id':'simulated'}))
    if scenario == 'codex-crash-before-turn':
        sys.stdout.flush(); os._exit(139)
    if scenario == 'codex-crash-after-reply':
        print(json.dumps({'type':'turn.completed','usage':{}})); sys.stdout.flush(); os._exit(139)
else:
    print(json.dumps({'subtype':'success','is_error':False,'structured_output':result}))
'''

class DuetCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='duet-cli-regression-', dir='/tmp')
        self.root = Path(self.tmp.name)
        self.repo = self.root/'repo'; self.repo.mkdir()
        self.home = self.root/'duet-home'; self.home.mkdir()
        self.bin = self.root/'bin'; self.bin.mkdir()
        for agent in ('codex','claude'):
            executable = self.bin/agent
            executable.write_text(FAKE_AGENT); executable.chmod(0o755)
        self.calls = self.root/'calls.jsonl'
        self.env = {**os.environ, 'DUET_HOME':str(self.home),
                    'PATH':str(self.bin)+os.pathsep+os.environ['PATH'],
                    'DUET_TEST_CALLS':str(self.calls), 'DUET_TEST_SCENARIO':'',
                    'GIT_CONFIG_GLOBAL':os.devnull, 'GIT_CONFIG_NOSYSTEM':'1',
                    'GIT_TERMINAL_PROMPT':'0'}
        for key in ('GIT_DIR','GIT_WORK_TREE','GIT_INDEX_FILE','GIT_COMMON_DIR'):
            self.env.pop(key,None)
        self.git('init','-q'); self.git('config','user.email','fixture@example.test')
        self.git('config','user.name','Duet fixture'); self.git('config','commit.gpgsign','false')
        (self.repo/'initial.txt').write_text('preserve original checkout\n')
        self.git('add','.'); self.git('commit','-qm','Initial fixture')
        self.base = self.git('rev-parse','HEAD').stdout.strip()
        self.feature = self.root/'feature'; (self.feature/'issues').mkdir(parents=True)
        self.spec = self.feature/'PRD.md'; self.spec.write_text('# Fixture\nImplement behavior.txt.\n')
        self.ticket = self.feature/'issues/01-behavior.md'
        self.ticket.write_text('# 01: Implement behavior\n\n**Blocked by:** None\n\n**Status:** ready-for-agent\n\n- [ ] behavior.txt contains implemented.\n')

    def tearDown(self):
        self.tmp.cleanup()

    def git(self,*args):
        return subprocess.run(['git',*args],cwd=self.repo,env=self.env,text=True,capture_output=True,check=True)

    def cli(self,*args,timeout=12):
        proc = subprocess.Popen([BUN,str(SUPERVISOR),*args],cwd=self.repo,env=self.env,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        try:
            stdout,stderr=proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid,signal.SIGKILL)
            proc.communicate()
            raise
        result = subprocess.CompletedProcess(proc.args,proc.returncode,stdout,stderr)
        self.last_output = result.stdout+'\n'+result.stderr
        return result

    def start(self,scenario='',builder='codex',**config):
        self.env['DUET_TEST_SCENARIO']=scenario
        (self.repo/'.duet.json').write_text(json.dumps({'check':'true','parallel':1,**config}))
        result=self.cli('start','--spec',str(self.spec),'--builder',builder)
        runs=list((self.home/'runs').glob('*/state.json'))
        self.assertEqual(len(runs),1,self.last_output)
        self.run_path=runs[0].parent
        self.state=json.loads(runs[0].read_text())
        return result

    def records(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def test_blocking_ticket_review_policy_preserves_strict_gate(self):
        self.start('empty-revise',ticketReviewPolicy='blocking')
        self.assertEqual(self.state['phase'],'stopped',self.last_output)
        self.assertEqual(self.state['tickets']['01']['status'],'needs_decision')
        self.assertNotIn('done (duet:',self.ticket.read_text())
        self.assertEqual(self.git('rev-parse',self.state['branch']).stdout.strip(),self.base)

    def test_final_check_failure_stops_after_two_fixes(self):
        self.start(checkFinal='false')
        self.assertEqual(self.state['phase'],'stopped',self.last_output)
        repairs=[r for r in self.records() if r['final'] and not r['review'] and not r['simplify']]
        self.assertEqual(len(repairs),2)
        self.assertEqual(len(list((self.run_path/'jobs/final').glob('check-*.log'))),3)
        self.assertEqual(len(list((self.run_path/'jobs/final').glob('fix-*'))),2)

    def test_check_failure_does_not_skip_first_independent_review(self):
        marker=self.root/'check-failed-once'
        check=f'if [ -f behavior.txt ] && [ ! -f "{marker}" ]; then touch "{marker}"; exit 1; fi'
        self.start(check=check)
        self.assertEqual(self.state['phase'],'done',self.last_output)
        first_review=next(r for r in self.records() if r['review'])
        self.assertNotIn('THIS IS A RE-REVIEW',first_review['prompt'])

    def test_repeat_review_can_report_newly_discovered_high_defects(self):
        self.start('revise-once')
        self.assertEqual(self.state['phase'],'done',self.last_output)
        reviews=[r for r in self.records() if r['review'] and not r['final']]
        self.assertEqual(len(reviews),2)
        self.assertIn('PRIOR FINDINGS',reviews[1]['prompt'])
        self.assertNotIn('never as high',reviews[1]['prompt'])
        self.assertIn('newly discovered',reviews[1]['prompt'])

    def test_critical_findings_are_carried_but_still_block_the_final_gate(self):
        self.start('critical')
        self.assertEqual(self.state['tickets']['01']['status'],'merged',self.last_output)
        self.assertEqual(self.state['tickets']['01']['carriedReview']['findings'][0]['severity'],'critical')
        self.assertEqual(self.state['phase'],'stopped',self.last_output)
        self.assertEqual(self.state['finalStep'],'review')
        self.assertIn('critical',self.ticket.read_text())
        self.assertNotEqual(self.git('rev-parse',self.state['branch']).stdout.strip(),self.base)

    def test_ticket_review_limit_carries_findings_into_final_reconciliation(self):
        self.start('revise-once',maxRounds=1)
        self.assertEqual(self.state['phase'],'done',self.last_output)
        ticket=self.state['tickets']['01']
        self.assertEqual(ticket['status'],'merged')
        self.assertEqual(ticket['carriedReview']['findings'][0]['id'],'F1')
        self.assertEqual(self.state['decisions'],{})
        ticket_reviews=[r for r in self.records() if r['review'] and not r['final']]
        self.assertEqual(len(ticket_reviews),1)
        reconcile=next(r for r in self.records() if r['final'] and not r['review'] and not r['simplify'])
        self.assertIn('unresolved per-ticket review findings',reconcile['prompt'])
        self.assertIn('F1',reconcile['prompt'])
        final_review=next(r for r in self.records() if r['review'] and r['final'])
        self.assertIn('PER-TICKET REVIEW ITEMS CARRIED',final_review['prompt'])
        self.assertIn('F1',final_review['prompt'])

    def test_true_owner_decision_still_stops_the_ticket(self):
        self.start('owner-decision')
        self.assertEqual(self.state['phase'],'stopped',self.last_output)
        self.assertEqual(self.state['tickets']['01']['status'],'needs_decision')
        self.assertEqual(len(self.state['decisions']),1)
        self.assertEqual(self.git('rev-parse',self.state['branch']).stdout.strip(),self.base)

    def test_builder_is_told_to_choose_safe_reversible_defaults(self):
        self.start()
        builder=next(r for r in self.records() if not r['review'] and not r['final'])
        self.assertIn('simplest reversible choice',builder['prompt'])
        self.assertIn('Missing tests, documentation, contract assertions, refactors',builder['prompt'])

    def test_rejected_commit_preserves_work_and_never_requests_review(self):
        self.start('commit-fails')
        self.assertEqual(self.state['tickets']['01']['status'],'failed',self.last_output)
        self.assertFalse(any(r['review'] for r in self.records()))
        wt=Path(self.state['tickets']['01']['worktree'])
        self.assertEqual((wt/'behavior.txt').read_text(),'uncommitted correction\n')
        self.assertEqual(self.git('rev-parse',self.state['branch']).stdout.strip(),self.base)

    def test_check_cannot_approve_a_dirty_tree(self):
        self.start(check='if [ -f behavior.txt ]; then echo changed-by-check > behavior.txt; fi')
        self.assertEqual(self.state['tickets']['01']['status'],'failed',self.last_output)
        self.assertFalse(any(r['review'] for r in self.records()))
        self.assertEqual(self.git('rev-parse',self.state['branch']).stdout.strip(),self.base)
        logs=list((self.run_path/'jobs/01').glob('check-*.log'))
        self.assertTrue(any('worktree' in p.read_text().lower() for p in logs))

    def test_failed_simplify_stops_and_can_resume(self):
        self.start('simplify-failed')
        self.assertEqual(self.state['phase'],'stopped',self.last_output)
        self.assertEqual(self.state['finalStep'],'simplify')
        self.assertFalse(any(r['review'] and r['final'] for r in self.records()))
        self.assertEqual((Path(self.state['integrationWorktree'])/'simplify-note.txt').read_text(),'preserved partial work\n')
        self.env['DUET_TEST_SCENARIO']=''
        self.cli('resume',self.state['id'])
        self.state=json.loads((self.run_path/'state.json').read_text())
        self.assertEqual(self.state['phase'],'done',self.last_output)
        self.assertNotIn('stopped:',self.last_output)
        original_reply=json.loads((self.run_path/'jobs/final/simplify/reply.json').read_text())
        self.assertEqual(original_reply['status'],'failed')

    def test_repository_instructions_reach_every_agent_phase(self):
        self.start(extraInstructions='Keep the fixture developer servers untouched.')
        self.assertEqual(self.state['phase'],'done',self.last_output)
        for record in self.records():
            self.assertIn('Keep the fixture developer servers untouched.',record['prompt'])

    def test_red_integration_check_sends_the_conflict_back_as_blocking(self):
        # Two parallel tickets that merge cleanly but break the integrated check together.
        (self.feature/'issues/02-sibling.md').write_text('# 02: Sibling\n\n**Blocked by:** None\n\n**Status:** ready-for-agent\n')
        red_when_combined = '[ "$(ls part-*.txt 2>/dev/null | wc -l)" -lt 2 ] || [ -f resolved.txt ]'
        self.start('semantic-conflict',parallel=2,check=red_when_combined)
        self.assertEqual(self.state['phase'],'done',self.last_output)
        fixes=[r['prompt'] for r in self.records() if not r['review'] and 'FIX ONLY THESE' in r['prompt']]
        self.assertTrue(any('INTEGRATE' in f.split('FIX ONLY THESE')[1].split('DO NOT FIX')[0] for f in fixes))
        self.assertTrue(any('post-merge-check.log' in f for f in fixes))

    def test_builders_climb_the_ladder_and_reviewers_do_not(self):
        self.start('revise-once')
        self.assertEqual(self.state['phase'],'done',self.last_output)
        builds=[r['prompt'] for r in self.records() if not r['review']]
        reviews=[r['prompt'] for r in self.records() if r['review']]
        self.assertTrue(builds and reviews)
        for prompt in builds: self.assertIn('ECONOMY',prompt)
        for prompt in reviews: self.assertNotIn('ECONOMY',prompt)
        self.assertTrue(any('prefer reusing or deleting' in p for p in builds if 'FIX ONLY THESE' in p))
        self.assertTrue(any('ponytail-review' in r['prompt'] for r in self.records() if r['simplify']))

    def test_worktree_files_reach_every_worktree_but_are_never_committed(self):
        (self.repo/'.gitignore').write_text('secret.env\n'); self.git('add','.gitignore'); self.git('commit','-qm','Ignore secret')
        (self.repo/'secret.env').write_text('KEY=local\n')
        self.start(worktreeFiles=['secret.env','missing.env'])
        self.assertEqual(self.state['phase'],'done',self.last_output)
        builds=[r for r in self.records() if not r['review']]
        self.assertTrue(builds and all(r['secret'] for r in builds))
        self.assertNotIn('secret.env',self.git('ls-tree','-r','--name-only',self.state['branch']).stdout)

    def test_ui_work_is_verified_in_a_browser_and_reviewed_from_its_evidence(self):
        self.start()
        self.assertEqual(self.state['phase'],'done',self.last_output)
        tickets=[r['prompt'] for r in self.records() if not r['review'] and not r['final']]
        reviews=[r['prompt'] for r in self.records() if r['review']]
        for prompt in tickets:
            self.assertIn('agent-browser',prompt); self.assertIn('impeccable',prompt)
        for prompt in reviews: self.assertIn('browser evidence',prompt)

    def test_claude_builds_start_with_the_implement_slash_command(self):
        self.start(builder='claude')
        self.assertEqual(self.state['phase'],'done',self.last_output)
        first=[r['prompt'] for r in self.records() if not r['review'] and not r['final']][0]
        self.assertTrue(first.startswith('/implement '),first[:80])

    def test_codex_builds_do_not_use_slash_commands(self):
        self.start(builder='codex')
        first=[r['prompt'] for r in self.records() if not r['review'] and not r['final']][0]
        self.assertFalse(first.startswith('/'),first[:80])

    def test_headless_claude_runs_subagents_in_the_foreground(self):
        # In `claude -p` with a required JSON result, ending a turn to wait for background agents ends
        # the job: the session demands the result and the builder reports its unfinished work as failed.
        self.start(builder='claude')
        claude=[r for r in self.records() if r['args'][0].endswith('claude')]
        self.assertTrue(claude and all(r['bg_disabled'] for r in claude))

    def test_a_codex_crash_after_a_completed_turn_keeps_its_reply(self):
        self.start('codex-crash-after-reply',builder='claude')
        self.assertEqual(self.state['phase'],'done',self.last_output)
        self.assertEqual(self.state['tickets']['01']['lastReview']['verdict'],'APPROVED')

    def test_a_codex_crash_before_its_turn_completes_still_fails(self):
        self.start('codex-crash-before-turn',builder='claude')
        self.assertEqual(self.state['tickets']['01']['status'],'failed',self.last_output)
        self.assertIn('139',self.state['tickets']['01']['note'])

    def test_processes_a_builder_leaves_in_its_worktree_are_stopped(self):
        # A dev server left running in the worktree competes with the check for the same files and memory.
        self.start('leaves-server')
        self.assertEqual(self.state['phase'],'done',self.last_output)
        pid=int((self.root/'server.pid').read_text())
        try:
            os.kill(pid,0); alive=True
        except ProcessLookupError:
            alive=False
        if alive: os.kill(pid,9)
        self.assertFalse(alive)

    def test_final_repeat_review_receives_previous_review_commit(self):
        self.start('final-revise-once')
        self.assertEqual(self.state['phase'],'done',self.last_output)
        reviews=[r for r in self.records() if r['review'] and r['final']]
        self.assertEqual(len(reviews),2)
        self.assertIn('THIS IS A RE-REVIEW',reviews[1]['prompt'])
        previous_head=reviews[0]['prompt'].split('Head commit: ')[1].splitlines()[0]
        self.assertIn('git diff '+previous_head+'..',reviews[1]['prompt'])

    def test_final_reviewer_cannot_approve_a_different_commit(self):
        self.start('reviewer-commits')
        self.assertEqual(self.state['phase'],'stopped',self.last_output)
        self.assertIn('reviewer',self.state['stopReason'])

    def test_blocked_simplify_does_not_proceed_to_final_review(self):
        self.start('simplify-blocked')
        self.assertEqual(self.state['phase'],'stopped',self.last_output)
        self.assertFalse(any(r['review'] and r['final'] for r in self.records()))

    def test_failed_simplify_process_keeps_partial_work(self):
        self.start('simplify-cli-fails',builder='claude')
        self.assertEqual(self.state['phase'],'stopped',self.last_output)
        self.assertEqual((Path(self.state['integrationWorktree'])/'simplify-note.txt').read_text(),'preserved partial work\n')

    def test_failed_final_fix_stops_without_claiming_completion(self):
        self.start('fix-failed',checkFinal='false')
        self.assertEqual(self.state['phase'],'stopped',self.last_output)
        self.assertIn('fix did not complete',self.state['stopReason'])
        self.assertEqual(len([r for r in self.records() if r['final'] and not r['review'] and not r['simplify']]),1)

    def test_check_cannot_approve_when_it_changes_head(self):
        self.start(check='if [ -f behavior.txt ]; then git commit --allow-empty -qm unexpected-check-commit; fi')
        self.assertEqual(self.state['tickets']['01']['status'],'failed',self.last_output)
        self.assertFalse(any(r['review'] for r in self.records()))

    def test_medium_notes_do_not_block_success(self):
        self.start('medium')
        self.assertEqual(self.state['phase'],'done',self.last_output)
        self.assertIn('medium',self.ticket.read_text())

    def test_both_builder_adapters_complete_without_touching_original_checkout(self):
        self.start(builder='claude')
        self.assertEqual(self.state['phase'],'done',self.last_output)
        self.assertEqual(self.git('rev-parse','HEAD').stdout.strip(),self.base)
        self.assertFalse((self.repo/'behavior.txt').exists())
        self.assertEqual(self.git('show',self.state['branch']+':behavior.txt').stdout,'implemented\n')
        self.assertIn('done (duet:',self.ticket.read_text())

    def test_plan_prints_explicit_model_selection(self):
        result=self.cli('plan','--spec',str(self.spec),'--builder','claude','--reviewer','codex',
                        '--claude-model','claude-opus-5[1m]','--codex-model','gpt-5.6-sol',
                        '--check','true')
        self.assertEqual(result.returncode,0,self.last_output)
        self.assertIn('models    claude=claude-opus-5[1m]',result.stdout)
        self.assertIn('codex=gpt-5.6-sol',result.stdout)
        self.assertIn('reviews   tickets=carry-to-final    final=blocking',result.stdout)

    def test_teardown_removes_derived_state_but_preserves_branch_and_audit(self):
        self.start()
        self.assertEqual(self.state['phase'],'done',self.last_output)
        run_id=self.state['id']; integration=Path(self.state['integrationWorktree'])
        integration_branch=self.state['branch']; ticket_branch=self.state['tickets']['01']['branch']
        jobs=self.run_path/'jobs'
        self.assertTrue(integration.exists())
        self.assertEqual(self.git('rev-parse','--verify',integration_branch).returncode,0)
        self.assertEqual(self.git('rev-parse','--verify',ticket_branch).returncode,0)

        result=self.cli('teardown',run_id)
        self.assertEqual(result.returncode,0,self.last_output)
        self.assertFalse(integration.exists())
        self.assertEqual(self.git('rev-parse','--verify',integration_branch).returncode,0)
        missing=subprocess.run(['git','rev-parse','--verify',ticket_branch],cwd=self.repo,
                               env=self.env,text=True,capture_output=True)
        self.assertNotEqual(missing.returncode,0)
        self.assertTrue(jobs.exists())
        state=json.loads((self.run_path/'state.json').read_text())
        self.assertEqual(state['teardown']['retainedBranch'],integration_branch)
        self.assertIn(str(integration),state['teardown']['removedWorktrees'])

        status=self.cli('status',run_id)
        self.assertIn('done. worktrees cleaned',status.stdout)
        self.assertNotIn('integration worktree:',status.stdout)
        repeated=self.cli('teardown',run_id)
        self.assertEqual(repeated.returncode,0,self.last_output)
        self.assertIn('already torn down',repeated.stdout)

    def test_teardown_refuses_a_run_that_is_not_done(self):
        self.start('empty-revise')
        result=self.cli('teardown',self.state['id'])
        self.assertNotEqual(result.returncode,0)
        self.assertIn('allowed only after the run is done',self.last_output)
        self.assertTrue(Path(self.state['integrationWorktree']).exists())

    def test_teardown_refuses_a_dirty_completed_worktree(self):
        self.start()
        integration=Path(self.state['integrationWorktree'])
        marker=integration/'owner-note.txt'; marker.write_text('preserve me\n')
        result=self.cli('teardown',self.state['id'])
        self.assertNotEqual(result.returncode,0)
        self.assertIn('uncommitted files',self.last_output)
        self.assertTrue(marker.exists())
        self.assertEqual(self.git('rev-parse','--verify',self.state['branch']).returncode,0)

if __name__=='__main__':
    unittest.main(verbosity=2)
