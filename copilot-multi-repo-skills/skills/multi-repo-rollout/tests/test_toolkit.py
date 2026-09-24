"""Tests for the multi-repo toolkit. Standard library only; no network. Run from the toolkit root:
    python3 -m unittest discover -s tests
The Flow tests drive the same commands the skills use, against the sandbox (scripts/sandbox.py)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'examples' / 'disable-feature-builds'))
import common  # noqa: E402
import limits  # noqa: E402
import config_edit  # noqa: E402
import git_probe  # noqa: E402
import repo_facts  # noqa: E402

GIT_ENV = {'GIT_AUTHOR_NAME': 'Bot', 'GIT_AUTHOR_EMAIL': 'bot@example.invalid', 'GIT_COMMITTER_NAME': 'Bot',
           'GIT_COMMITTER_EMAIL': 'bot@example.invalid', 'GIT_CONFIG_COUNT': '1',
           'GIT_CONFIG_KEY_0': 'commit.gpgsign', 'GIT_CONFIG_VALUE_0': 'false'}
DIRECT = common.target_id('demo/direct', 'main')
FEATURE = common.target_id('demo/feature-branch', 'feature/checkout')
PROPS = common.target_id('demo/properties', 'main')
BROKEN = common.target_id('demo/broken-build', 'main')


class Env:
    """A temp dir with its own repo-facts, and helpers to run toolkit commands."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='mrt-test-'))
        self.env = dict(os.environ, MULTI_REPO_HOME=str(self.tmp / 'home'), REPO_FACTS_DIR=str(self.tmp / 'facts'), ROLLOUT_LOCK_DIR=str(self.tmp / 'locks'),
                        SCM_CONFIG=str(self.tmp / 'sbx' / 'scm.json'), ROLLOUT_LIMITS=str(self.tmp / 'limits.json'),
                        **GIT_ENV)
        (self.tmp / 'limits.json').write_text(json.dumps({'partial_clone_hosts': ['local']}))
        for k in ('MULTI_REPO_HOME', 'REPO_FACTS_DIR', 'ROLLOUT_LOCK_DIR', 'SCM_CONFIG', 'ROLLOUT_LIMITS'):
            os.environ[k] = self.env[k]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def raw(self, script, *args, cwd=ROOT, stdin=None):
        path = ROOT / ('tests' if script == 'check_package.py' else 'scripts') / script
        return subprocess.run([sys.executable, str(path), *map(str, args)], cwd=cwd,
                              capture_output=True, text=True, env=self.env, input=stdin)

    def ok(self, script, *args):
        p = self.raw(script, *args)
        self.assertEqual(p.returncode, 0, f'{script} {args}\n{p.stdout}\n{p.stderr}')
        return json.loads(p.stdout) if p.stdout.strip().startswith(('{', '[')) else p.stdout

    def blocked(self, script, *args):
        p = self.raw(script, *args)
        self.assertNotEqual(p.returncode, 0, f'expected BLOCKED: {script} {args}\n{p.stdout}')
        return p.stderr + p.stdout

    def git(self, repo, *args, check=True):
        p = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, env=self.env)
        if check:
            self.assertEqual(p.returncode, 0, f'git {args}: {p.stderr}')
        return p


# ------------------------------------------------------------------ helpers without a run

class HelperTests(Env, unittest.TestCase):
    def test_target_ids_are_stable_readable_and_distinct(self):
        self.assertEqual(common.target_id('A/b', 'main'), common.target_id('A/b', 'main'))
        self.assertNotEqual(common.target_id('A/b-c', 'main'), common.target_id('A/b', 'c-main'))
        self.assertTrue(common.target_id('ABC/orders', 'feature/x').startswith('abc-orders--feature-x-'))
        self.assertEqual(common.branch_name('{key}/{recipe}-{target}', 'ENG-1', 'Disable X', 't1'),
                         'ENG-1/disable-x-t1')
        with self.assertRaises(ValueError):
            common.branch_name('{key}..{target}', 'ENG-1', 'r', 't')

    def test_commit_subject_rule(self):
        self.assertTrue(common.check_subject('ENG-12', 'ENG-12 Do it'))
        for bad in ('ENG-12Do it', 'eng-12 Do it', 'Do it ENG-12', 'ENG-12 ', 'ENG-13 Do it'):
            with self.assertRaises(ValueError):
                common.check_subject('ENG-12', bad)

    def test_config_edit_is_exact_and_idempotent(self):
        new, changed = config_edit.transform('{\n  "a": 1,\n  "featureBuilds": true\n}\n', 'featureBuilds', 'json')
        self.assertTrue(changed)
        self.assertEqual(new, '{\n  "a": 1,\n  "featureBuilds": false\n}\n')
        self.assertEqual(config_edit.transform(new, 'featureBuilds', 'json'), (new, False))
        self.assertEqual(config_edit.transform('x=1\nfeatureBuilds=true\n', 'featureBuilds', 'properties')[0],
                         'x=1\nfeatureBuilds=false\n')
        for text, fmt in (('{"featureBuilds": true, "featureBuilds": false}', 'json'), ('{"a": 1}', 'json'),
                          ('{"featureBuilds": "true"}', 'json'), ('featureBuilds=true\nfeatureBuilds=false', 'properties')):
            with self.assertRaises(ValueError):
                config_edit.transform(text, 'featureBuilds', fmt)

    def test_url_normalization_matches_ssh_https_and_bitbucket_scm(self):
        same = ['ssh://git@bitbucket.example.com:7999/abc/orders.git', 'https://bitbucket.example.com/scm/abc/orders.git',
                'git@bitbucket.example.com:abc/orders.git', 'https://user@bitbucket.example.com/scm/ABC/orders']
        self.assertEqual(len({git_probe.norm_url(u) for u in same}), 1)
        self.assertNotEqual(git_probe.norm_url('git@github.com:a/b.git'), git_probe.norm_url('git@github.com:a/c.git'))

    def test_doctor_needs_no_setup(self):
        out = self.ok('mr.py', 'doctor')
        self.assertTrue(out['ok'])
        self.assertFalse((self.tmp / 'home' / 'config.json').exists())  # nothing to configure

    def test_scm_host_routing_sends_each_token_only_to_its_own_kind_of_server(self):
        import http.server
        import threading
        import scm

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = {'/rest/api/1.0/application-properties': {'version': '8.19.1', 'displayName': 'Bitbucket'},
                        '/api/v3/meta': {'installed_version': '3.14.0'}}.get(self.path)
                if self.server.kind == 'bitbucket' and self.path.startswith('/api/v3') or \
                        self.server.kind == 'github' and self.path.startswith('/rest/'):
                    body = None
                self.send_response(200 if body else 404)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode() if body else b'')

            def log_message(self, *a):
                pass
        servers = {}
        for kind in ('bitbucket', 'github'):
            srv = http.server.HTTPServer(('127.0.0.1', 0), H)
            srv.kind = kind
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            servers[kind] = srv
        os.environ.pop('SCM_CONFIG', None)
        os.environ.update(MR_PROBE_SCHEME='http', BITBUCKET_TOKEN='bb', GITHUB_TOKEN='gh')
        try:
            bb = f"127.0.0.1:{servers['bitbucket'].server_address[1]}"
            gh = f"127.0.0.1:{servers['github'].server_address[1]}"
            self.assertEqual((scm.detect(bb)['kind'], scm.detect(bb)['token_env']), ('bitbucket', 'BITBUCKET_TOKEN'))
            self.assertEqual((scm.detect(gh)['kind'], scm.detect(gh)['token_env']), ('github', 'GITHUB_TOKEN'))
            self.assertEqual(scm.detect(gh)['api'], f'http://{gh}/api/v3')
            with self.assertRaises(ValueError):
                scm.detect('127.0.0.1:1')  # nothing answers: no token is sent anywhere
            os.environ.pop('BITBUCKET_TOKEN')
            (self.tmp / 'home' / 'cache' / 'scm-hosts.json').unlink()
            with self.assertRaises(ValueError):
                scm.detect(bb)  # a Bitbucket host never gets the GitHub token
        finally:
            for srv in servers.values():
                srv.shutdown()
            for k in ('MR_PROBE_SCHEME', 'BITBUCKET_TOKEN', 'GITHUB_TOKEN'):
                os.environ.pop(k, None)
            os.environ['SCM_CONFIG'] = self.env['SCM_CONFIG']

    def test_repo_path_comes_from_the_clone_url(self):
        import scm
        self.assertEqual(scm.repo_path('ssh://git@bitbucket.example.com:7999/ABC/orders.git'), 'ABC/orders')
        self.assertEqual(scm.repo_path('https://u@bitbucket.example.com/scm/ABC/orders.git'), 'ABC/orders')
        self.assertEqual(scm.repo_path('git@github.com:Org/Repo.git'), 'Org/Repo')

    def test_repo_facts_merge_keeps_notes_and_reports_stale(self):
        repo_facts.merge('ABC/svc', {'build': './mvnw -q verify', 'dev_branch': 'develop'})
        p = repo_facts.path_for('ABC/svc')
        p.write_text(p.read_text().replace('## Notes', '## Notes\nNeeds Docker.'))
        repo_facts.merge('ABC/svc', {'reviewers': '@team'})
        f = repo_facts.get('ABC/svc')
        self.assertEqual((f['build'], f['dev_branch'], f['reviewers']), ('./mvnw -q verify', 'develop', '@team'))
        self.assertIn('Needs Docker.', p.read_text())
        self.assertEqual(repo_facts.listing(stale_days=30), [])
        p.write_text(p.read_text().replace(f['verified'][:10], '2020-01-01'))
        self.assertEqual(len(repo_facts.listing(stale_days=30)), 1)

    def test_package_check_passes(self):
        self.ok('check_package.py')


# ------------------------------------------------------------------ recipe tools on the sandbox

class RecipeTests(Env, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.sbx = self.tmp / 'sbx'
        self.ok('sandbox.py', 'make', '--out', self.sbx)
        self.clone = self.tmp / 'ws' / 'direct'
        self.ok('git_probe.py', 'prepare', '--url', self.sbx / 'remotes/demo__direct.git', '--path', self.clone)

    def test_trial_shows_real_diff_and_keeps_nothing(self):
        o = self.ok('recipe_run.py', 'trial', '--recipe', 'examples/disable-feature-builds',
                    '--clone', self.clone, '--branch', 'main')
        self.assertEqual((o['before']['status'], o['after']['status']), ('needs_change', 'compliant'))
        self.assertIn('+  "featureBuilds": false', o['diff'])
        # automation clones have no checkout: every file shows as a staged deletion, nothing else
        self.assertEqual([x for x in self.git(self.clone, 'status', '--porcelain').stdout.splitlines()
                          if not x.startswith('D ')], [])
        self.assertEqual(self.git(self.clone, 'worktree', 'list').stdout.count('\n'), 1)

    def test_check_on_ref_and_scaffold_recipe_is_unknown(self):
        o = self.ok('recipe_run.py', 'check', '--recipe', 'examples/disable-feature-builds',
                    '--repo', self.clone, '--ref', 'origin/main')
        self.assertEqual(o['status'], 'needs_change')

    def test_hooks_installed_and_block_commits_in_the_clone(self):
        self.ok('git_probe.py', 'hooks-check', '--repo', self.clone)
        (self.clone / 'x.txt').write_text('x')
        self.git(self.clone, 'add', 'x.txt')
        p = self.git(self.clone, 'commit', '-m', 'ENG-900 x', check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('target worktree', p.stderr)

    def test_prepare_refuses_other_origin_and_dirty_clone(self):
        self.assertIn('Origin mismatch', self.blocked('git_probe.py', 'prepare', '--url',
                                                        self.sbx / 'remotes/demo__done.git', '--path', self.clone))
        (self.clone / 'config.json').write_text('{}')
        self.assertIn('Dirty', self.blocked('git_probe.py', 'prepare', '--url',
                                              self.sbx / 'remotes/demo__direct.git', '--path', self.clone))

    def test_sandbox_pr_server(self):
        self.assertIn('push first', self.blocked('sandbox.py', 'pr-create', '--store', self.sbx / 'scm', '--repo',
                                                 'demo/direct', '--source', 'nope', '--dest', 'main', '--title', 't'))
        repos = self.ok('sandbox.py', 'repo-list', '--store', self.sbx / 'scm')
        self.assertEqual(len(repos), 8)


# ------------------------------------------------------------------ full flows

class Flow(Env):
    recipe = 'examples/disable-feature-builds'

    def setUp(self):
        super().setUp()
        self.sbx = self.tmp / 'sbx'
        self.ok('sandbox.py', 'make', '--out', self.sbx)
        self.store = self.sbx / 'scm'
        card = self.ok('sandbox.py', 'jira-get', '--dir', self.sbx, '--key', 'ENG-900')
        self.ok('run_state.py', 'init', '--root', self.tmp / 'runs', '--id', 'r1', '--recipe', self.recipe,
                '--jira', card['key'], '--jira-title', card['title'], '--jira-url', card['url'],
                '--jira-evidence', 'sandbox jira-get', '--repos', self.sbx / 'repos.txt', '--request', 'Disable feature builds')
        self.run_dir = self.tmp / 'runs' / 'r1'

    def discover(self, *names, workers=4, extra=()):
        args = ['--repo', *[x for n in names for x in ('--repo', n)][1:]] if names else ['--pending']
        return self.ok('recipe_run.py', 'discover', '--run', self.run_dir, *args, '--workspaces', self.tmp / 'ws',
                       '--days', '30', '--workers', workers, '--verbose', *extra)

    def plan(self, *extra):
        return self.ok('run_state.py', 'plan', '--run', self.run_dir, '--strategy', 'test', *extra)

    def approve(self, *targets, pilots=(), extra=()):
        return self.ok('run_state.py', 'approve', '--run', self.run_dir, '--evidence', 'User: go',
                       *(['--targets', *targets] if targets else []), *(['--pilot', *pilots] if pilots else []), *extra)

    def manifest(self):
        return json.loads((self.run_dir / 'manifest.json').read_text())

    def timings(self):
        p = self.run_dir / 'timings.jsonl'
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def target(self, tid):
        return next(t for t in self.manifest()['plan']['targets'] if t['id'] == tid)

    def ready_items(self, *tasks, extra=()):
        return {i['target']: i['task'] for i in self.ok('run_state.py', 'take', '--run', self.run_dir, '--limit', '50',
                                                        *(['--task', *tasks] if tasks else []), *extra)}

    def deliver(self, *tids, extra=()):
        return {r['target']: r for r in self.ok('recipe_run.py', 'deliver', '--run', self.run_dir, '--target', *tids, *extra)}

    def record(self, tid, status, *extra):
        return self.ok('run_state.py', 'record', '--run', self.run_dir, '--target', tid, '--status', status,
                       '--evidence', 'test', *extra)

    def open_pr(self, tid):
        t = self.target(tid)
        cur = self.git(t['worktree'], 'rev-parse', f"origin/{t['branch']}").stdout.strip()
        self.ok('run_state.py', 'gate', '--run', self.run_dir, '--target', tid, '--action', 'create_pr', '--current-sha', cur)
        pr = self.ok('sandbox.py', 'pr-create', '--store', self.store, '--repo', t['repo'], '--source', t['source_branch'],
                     '--dest', t['branch'], '--title', 'ENG-900 Disable feature builds')
        self.record(tid, 'pr_opened', '--pr-url', pr['url'], '--pr-state', 'open', '--ci', 'pending')
        return pr

    def ci_passed(self, tid, pr):
        self.ok('sandbox.py', 'ci', '--store', self.store, '--id', pr['id'])
        self.record(tid, 'observed', '--ci', 'passed')

    def ready(self, pilots=()):
        self.discover()
        self.plan()
        self.approve(pilots=pilots)

    def questions(self):
        return self.ok('run_state.py', 'questions', '--run', self.run_dir, '--all')


class DiscoveryTests(Flow, unittest.TestCase):
    def test_discovery_outcomes_match_the_sandbox_design(self):
        out = {r['repo']: r for r in self.discover()}
        self.assertEqual(out['demo/unreachable']['outcome'], 'unreadable')
        self.assertEqual({b['branch']: b['status'] for b in out['demo/feature-branch']['branches']},
                         {'main': 'compliant', 'feature/checkout': 'needs_change'})
        p = self.plan()
        self.assertEqual(p['counts'], {'needs_change': 4, 'compliant': 2, 'excluded': 0, 'unresolved': 2})
        self.assertEqual(sorted(p['needs_approval']), sorted([DIRECT, FEATURE, PROPS, BROKEN]))
        preview = (self.run_dir / 'preview.md').read_text()
        self.assertIn('Local preview: nothing has been pushed', preview)
        self.assertIn('duplicate key', preview)
        self.assertEqual(repo_facts.get('demo/direct')['build'], 'sh build.sh')  # plan merged the facts
        self.assertTrue(any(t['stage'] == 'ls-remote' and t.get('failed') for t in self.timings()))

    def test_plan_requires_complete_discovery_and_nothing_runs_before_approval(self):
        self.discover('demo/direct')
        self.assertIn('Discovery not finished', self.blocked('run_state.py', 'plan', '--run', self.run_dir, '--strategy', 's'))
        self.discover()
        self.plan()
        wt = Path(self.target(DIRECT)['worktree'])
        self.assertIn('not approved', self.git(wt, 'commit', '-am', 'ENG-900 too early', check=False).stderr)
        self.assertEqual(self.ready_items('deliver'), {})

    def test_question_keeps_target_out_until_answered(self):
        self.discover()
        self.ok('run_state.py', 'ask', '--run', self.run_dir, '--target', DIRECT, '--text', 'Inherited?')
        self.plan()
        self.assertEqual(self.target(DIRECT)['disposition'], 'unresolved')
        self.ok('run_state.py', 'answer', '--run', self.run_dir, '--question', self.questions()[0]['id'],
                '--answer', 'no', '--evidence', 'User: not inherited')
        self.plan()
        self.assertEqual(self.target(DIRECT)['disposition'], 'needs_change')


class AgentEditTests(Flow, unittest.TestCase):
    """A scripted recipe whose apply can't handle a repo: an agent edits it before approval."""

    def setUp(self):
        self.recipe_copy = Path(tempfile.mkdtemp(prefix='mrt-recipe-')) / 'no-apply'
        shutil.copytree(ROOT / 'examples/disable-feature-builds', self.recipe_copy)
        (self.recipe_copy / 'apply.py').unlink()
        self.recipe = str(self.recipe_copy)
        super().setUp()

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.recipe_copy.parent, ignore_errors=True)

    def test_edit_then_capture_makes_it_approvable(self):
        self.discover()
        p = self.plan()
        self.assertIn(DIRECT, p['needs_edit'])
        self.assertEqual(self.ready_items()[DIRECT], 'edit')
        self.assertIn('needs an edit', self.blocked('run_state.py', 'approve', '--run', self.run_dir,
                                                    '--targets', DIRECT, '--evidence', 'x'))
        wt = Path(self.target(DIRECT)['worktree'])
        (wt / 'config.json').write_text((wt / 'config.json').read_text().replace('true', 'false'))
        e = self.ok('recipe_run.py', 'capture', '--run', self.run_dir, '--target', DIRECT)
        self.assertEqual(e['post_check']['status'], 'compliant')
        self.assertIn(DIRECT, self.plan()['needs_approval'])
        self.approve(DIRECT)


class ExecutionTests(Flow, unittest.TestCase):
    def test_pilot_needs_green_ci_then_releases_the_rest(self):
        self.ready(pilots=[DIRECT])
        self.assertEqual(self.ready_items('deliver'), {DIRECT: 'deliver'})
        self.assertEqual(self.deliver(DIRECT)[DIRECT]['status'], 'pushed')
        self.assertEqual(self.ready_items()[DIRECT], 'open_pr')
        pr = self.open_pr(DIRECT)
        self.assertIn('Waiting for pilot', self.blocked('run_state.py', 'gate', '--run', self.run_dir, '--target', FEATURE,
                                                        '--action', 'create_pr', '--current-sha', 'a' * 40))
        self.ci_passed(DIRECT, pr)
        self.assertEqual(sorted(self.ready_items('deliver')), sorted([FEATURE, PROPS, BROKEN]))
        wt = Path(self.target(DIRECT)['worktree'])
        self.assertEqual(self.git(wt, 'log', '-1', '--format=%s').stdout.strip(), 'ENG-900 Disable feature builds')
        self.ok('run_state.py', 'report', '--run', self.run_dir)
        self.assertIn('1 PRs open (1 green CI)', (self.run_dir / 'jira-table.md').read_text())
        self.ok('run_state.py', 'gate', '--run', self.run_dir, '--action', 'jira_comment')

    def test_pause_is_just_approving_the_pilot_first(self):
        self.discover()
        self.plan()
        self.approve(DIRECT, pilots=[DIRECT])
        self.deliver(DIRECT)
        self.ci_passed(DIRECT, self.open_pr(DIRECT))
        self.assertEqual(self.ready_items('deliver'), {})
        self.assertEqual(self.ok('run_state.py', 'report', '--run', self.run_dir)['needs_approval']['count'], 3)
        self.approve()
        self.assertEqual(len(self.ready_items('deliver')), 3)

    def test_preexisting_build_failure_asks_and_answer_requeues(self):
        self.ready()
        r = self.deliver(BROKEN)[BROKEN]
        self.assertEqual((r['status'], 'pre-existing' in r['reason']), ('blocked', True))
        q = self.questions()
        self.assertEqual(q[0]['target'], BROKEN)
        self.assertNotIn(BROKEN, self.ready_items())
        self.assertEqual(q[0]['choices'], ['draft', 'skip', 'retry'])
        self.assertIn('--choice', self.blocked('run_state.py', 'answer', '--run', self.run_dir, '--question', q[0]['id'],
                                               '--answer', 'x', '--evidence', 'x'))
        self.ok('run_state.py', 'answer', '--run', self.run_dir, '--question', q[0]['id'], '--answer', 'retry later',
                '--choice', 'retry', '--evidence', 'User: retry')
        self.assertEqual(self.ready_items()[BROKEN], 'deliver')

    def test_build_broken_by_change_is_failed_not_pushed(self):
        self.ready()
        r = self.deliver(DIRECT, extra=['--build', 'grep -q true config.json'])[DIRECT]
        self.assertEqual(r['status'], 'failed')
        self.assertIn('change broke it', r['reason'])
        t = self.target(DIRECT)
        self.assertEqual(self.git(t['worktree'], 'ls-remote', 'origin', t['source_branch']).stdout, '')

    def test_hooks_enforce_prefix_content_branch_and_no_force(self):
        self.ready()
        t = self.target(DIRECT)
        wt = Path(t['worktree'])
        self.git(wt, 'add', '-A')
        self.assertIn('must start with', self.git(wt, 'commit', '-m', 'Disable builds', check=False).stderr)
        (wt / 'extra.txt').write_text('not in the preview')
        self.git(wt, 'add', '-A')
        self.assertIn('differs from the approved preview', self.git(wt, 'commit', '-m', 'ENG-900 more', check=False).stderr)
        (wt / 'extra.txt').unlink()
        self.assertEqual(self.deliver(DIRECT)[DIRECT]['status'], 'pushed')
        self.assertIn('Only', self.git(wt, 'push', 'origin', 'HEAD:main', check=False).stderr)
        self.git(wt, 'commit', '--amend', '-m', 'ENG-900 Reworded')
        self.assertIn('Force-push', self.git(wt, 'push', '-f', 'origin', t['source_branch'], check=False).stderr)

    def test_commits_made_without_hooks_are_caught_when_recorded(self):
        self.ready()
        t = self.target(DIRECT)
        wt = Path(t['worktree'])
        self.git(wt, 'add', '-A')
        self.git(wt, 'commit', '--no-verify', '-m', 'unprefixed subject')
        self.git(wt, 'push', '--no-verify', '-q', 'origin', t['source_branch'])
        pr = self.ok('sandbox.py', 'pr-create', '--store', self.store, '--repo', t['repo'], '--source', t['source_branch'],
                     '--dest', 'main', '--title', 't')
        o = self.record(DIRECT, 'pr_opened', '--pr-url', pr['url'])
        self.assertEqual(o['status'], 'blocked')
        self.assertIn('lacks the "ENG-900 " prefix', o['violations'][0])
        self.assertIn('Policy check failed', self.questions()[0]['text'])

    def test_moved_destination_heals_by_rediscovery_when_the_diff_is_identical(self):
        self.ready()
        other = self.tmp / 'other'
        self.git(self.tmp, 'clone', '-q', self.sbx / 'remotes/demo__direct.git', other)
        (other / 'README.md').write_text('moved\n')
        self.git(other, 'add', '-A')
        self.git(other, 'commit', '-qm', 'someone else')
        self.git(other, 'push', '-q', 'origin', 'main')
        self.assertIn('moved', self.deliver(DIRECT)[DIRECT]['reason'])
        self.discover('demo/direct')
        self.assertNotIn(DIRECT, self.plan()['needs_approval'])  # identical diff: approval still holds
        self.ok('run_state.py', 'retry', '--run', self.run_dir, '--target', DIRECT, '--evidence', 'rediscovered')
        self.assertEqual(self.deliver(DIRECT)[DIRECT]['status'], 'pushed')

    def test_dependencies_hold_targets_back(self):
        self.discover()
        (self.tmp / 'deps.json').write_text(json.dumps({FEATURE: [{'target': DIRECT, 'milestone': 'pr_opened'}]}))
        self.plan('--deps', self.tmp / 'deps.json')
        self.approve()
        self.assertNotIn(FEATURE, self.ready_items())
        self.deliver(DIRECT)
        self.open_pr(DIRECT)
        self.assertIn(FEATURE, self.ready_items())

    def test_no_jira_blocks_the_status_comment(self):
        self.discover()
        self.plan()
        self.approve(extra=['--no-jira'])
        self.assertIn('not approved', self.blocked('run_state.py', 'gate', '--run', self.run_dir, '--action', 'jira_comment'))


class ChangeOfCourseTests(Flow, unittest.TestCase):
    def test_revise_reapproves_only_changed_diffs_and_adds_a_follow_up_commit(self):
        self.ready()
        prs = {}
        for tid in (DIRECT, PROPS):
            self.deliver(tid)
            prs[tid] = self.open_pr(tid)
        v2 = self.tmp / 'recipe-v2'
        shutil.copytree(ROOT / self.recipe, v2)
        a = (v2 / 'apply.py').read_text()
        a = a.replace("    if did:\n", "    if fmt == 'properties' and '# off' not in text:\n"
                      "        new = new.replace('featureBuilds=false', '# off\\nfeatureBuilds=false')\n"
                      "        did = True\n    if did:\n")
        (v2 / 'apply.py').write_text(a)
        self.ok('run_state.py', 'revise', '--run', self.run_dir, '--recipe', v2, '--evidence', 'add comment')
        self.discover()
        self.assertEqual(self.plan()['needs_approval'], [PROPS])  # only the changed diff comes back
        items = self.ready_items()
        self.assertNotIn(DIRECT, items)  # unchanged and delivered: done
        self.assertNotIn(PROPS, items)  # changed: waits for approval
        self.approve()
        self.assertEqual(self.deliver(PROPS)[PROPS]['status'], 'pushed')
        o = self.record(PROPS, 'pr_updated', '--pr-url', prs[PROPS]['url'], '--pr-state', 'open')
        self.assertEqual(o['violations'], [])
        wt = self.target(PROPS)['worktree']
        self.assertEqual(self.git(wt, 'rev-list', '--count', f"{self.target(PROPS)['observed_sha']}..HEAD").stdout.strip(), '2')

    def test_abort_allows_only_closing(self):
        self.ready()
        self.deliver(DIRECT)
        pr = self.open_pr(DIRECT)
        t = self.target(DIRECT)
        wt = Path(t['worktree'])
        self.assertIn('needs a recorded abort', self.blocked('run_state.py', 'gate', '--run', self.run_dir,
                                                             '--target', DIRECT, '--action', 'close_pr'))
        self.assertNotEqual(self.git(wt, 'push', 'origin', '--delete', t['source_branch'], check=False).returncode, 0)
        o = self.ok('run_state.py', 'abort', '--run', self.run_dir, '--targets', DIRECT, '--evidence', 'User: drop it')
        self.assertEqual(o['close_these_prs'][0]['pr_url'], pr['url'])
        self.ok('run_state.py', 'gate', '--run', self.run_dir, '--target', DIRECT, '--action', 'close_pr')
        self.git(wt, 'push', '-q', 'origin', '--delete', t['source_branch'])
        (wt / 'x').write_text('x')
        self.git(wt, 'add', '-A')
        self.assertIn('aborted', self.git(wt, 'commit', '-m', 'ENG-900 x', check=False).stderr)
        self.assertNotIn(DIRECT, self.ready_items())

    def test_after_the_pr_exists_follow_up_commits_need_only_prefix_and_branch(self):
        self.ready()
        self.deliver(DIRECT)
        self.open_pr(DIRECT)
        other = self.tmp / 'other'
        self.git(self.tmp, 'clone', '-q', self.sbx / 'remotes/demo__direct.git', other)
        (other / 'README.md').write_text('moved\n')
        self.git(other, 'add', '-A')
        self.git(other, 'commit', '-qm', 'someone else')
        self.git(other, 'push', '-q', 'origin', 'main')
        t = self.target(DIRECT)
        wt = Path(t['worktree'])
        self.git(wt, 'fetch', '-q', 'origin', 'main')
        self.git(wt, 'merge', '--no-ff', 'origin/main', '-m', 'ENG-900 Merge main')
        self.git(wt, 'push', '-q', 'origin', t['source_branch'])  # no repair mode needed
        self.assertIn('must start with', self.git(wt, 'commit', '--allow-empty', '-m', 'no prefix', check=False).stderr)


class AgentModeTests(Flow, unittest.TestCase):
    """Mode: agent. Approve the approach and pilots; each diff is made, validated and reviewed, then goes out as a draft PR."""

    def setUp(self):
        self.recipe_copy = Path(tempfile.mkdtemp(prefix='mrt-recipe-')) / 'agent'
        shutil.copytree(ROOT / 'examples/disable-feature-builds', self.recipe_copy)
        (self.recipe_copy / 'apply.py').unlink()
        md = self.recipe_copy / 'recipe.md'
        md.write_text(md.read_text().replace('Mode: scripted · Risk: low', 'Mode: agent · Risk: high'))
        (self.recipe_copy / 'check.py').write_text(
            "import json, sys\nfrom pathlib import Path\n"
            "if Path('build.properties').is_file():\n"
            "    print(json.dumps({'status': 'maybe', 'evidence': 'properties layout; needs a look'})); sys.exit()\n"
            "try:\n    v = json.loads(Path('config.json').read_text())['featureBuilds']\n"
            "except Exception as e:\n    print(json.dumps({'status': 'unknown', 'evidence': str(e)})); sys.exit()\n"
            "print(json.dumps({'status': 'needs_change' if v is True else 'compliant', 'evidence': f'featureBuilds={v}'}))\n")
        self.recipe = str(self.recipe_copy)
        super().setUp()

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.recipe_copy.parent, ignore_errors=True)

    def test_agent_recipe_flow(self):
        self.discover()
        p = self.plan()
        self.assertEqual(p['mode'], 'agent')
        self.assertIn(PROPS, p['undecided'])
        self.assertIn('diff made after approval', (self.run_dir / 'preview.md').read_text())
        self.assertEqual(self.ready_items()[PROPS], 'decide')
        self.ok('recipe_run.py', 'decide', '--run', self.run_dir, '--target', PROPS, '--status', 'needs_change',
                '--evidence', 'featureBuilds=true in build.properties')
        self.assertIn(PROPS, self.plan()['needs_approval'])
        self.assertEqual(self.approve(DIRECT, pilots=[DIRECT])['pr_mode'], 'draft')
        self.assertEqual(self.ready_items()[DIRECT], 'change')
        self.assertIn('not passed validation', self.deliver(DIRECT)[DIRECT]['reason'])
        wt = Path(self.target(DIRECT)['worktree'])
        (wt / 'config.json').write_text((wt / 'config.json').read_text().replace('true', 'false'))
        self.assertEqual(self.deliver(DIRECT, extra=['--dry'])[DIRECT]['status'], 'validated')
        self.assertEqual(self.ready_items()[DIRECT], 'review')
        self.assertIn('not been reviewed', self.deliver(DIRECT)[DIRECT]['reason'])
        self.record(DIRECT, 'observed', '--reviewed')
        self.assertEqual(self.ready_items()[DIRECT], 'deliver')
        self.assertEqual(self.deliver(DIRECT)[DIRECT]['status'], 'pushed')
        self.assertEqual(self.open_pr(DIRECT) and self.manifest()['results'][DIRECT]['status'], 'pr_opened')

    def test_edit_is_replayed_not_lost_when_the_destination_moves(self):
        self.discover()
        self.plan()
        self.approve(DIRECT)
        wt = Path(self.target(DIRECT)['worktree'])
        (wt / 'config.json').write_text((wt / 'config.json').read_text().replace('true', 'false'))
        other = self.tmp / 'other'
        self.git(self.tmp, 'clone', '-q', self.sbx / 'remotes/demo__direct.git', other)
        (other / 'README.md').write_text('moved\n')
        self.git(other, 'add', '-A')
        self.git(other, 'commit', '-qm', 'someone else')
        self.git(other, 'push', '-q', 'origin', 'main')
        self.discover('demo/direct')
        self.assertIn('"featureBuilds": false', (wt / 'config.json').read_text())
        self.assertTrue((wt / 'README.md').exists())


class LimitsTests(Env, unittest.TestCase):
    def test_slot_is_shared_across_processes(self):
        code = ('import sys,time; sys.path.insert(0, %r); import limits\n'
                'with limits.slot("x", 1): time.sleep(0.3)') % str(ROOT / 'scripts')
        t0 = time.monotonic()
        procs = [subprocess.Popen([sys.executable, '-c', code], env=self.env) for _ in range(3)]
        for pr in procs:
            pr.wait()
        self.assertGreaterEqual(time.monotonic() - t0, 0.85)  # one at a time, not three in parallel

    def test_token_bucket_and_shared_hold(self):
        t0 = time.monotonic()
        for _ in range(4):
            limits.throttle('api:test', rate=10, burst=1)
        self.assertGreaterEqual(time.monotonic() - t0, 0.25)
        limits.hold('api:test2', 0.5)
        t0 = time.monotonic()
        limits.throttle('api:test2', rate=100, burst=5)
        self.assertGreaterEqual(time.monotonic() - t0, 0.4)

    def test_retry_only_transient(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise ValueError('HTTP 503 Service Unavailable')
            return 'ok'
        self.assertEqual(limits.retry(flaky, base=0.01), 'ok')
        calls.clear()

        def denied():
            calls.append(1)
            raise ValueError('HTTP 401 Unauthorized')
        with self.assertRaises(ValueError):
            limits.retry(denied, base=0.01)
        self.assertEqual(len(calls), 1)


class SpeedTests(Flow, unittest.TestCase):
    def disc(self, repo):
        return json.loads((self.run_dir / 'discovery' / (common.slug(repo, 80) + '.json')).read_text())

    def test_partial_clone_downloads_only_the_checked_file(self):
        self.discover('demo/done')
        clone = self.tmp / 'ws' / 'demo-done'
        self.assertEqual(self.git(clone, 'config', 'remote.origin.promisor').stdout.strip(), 'true')
        # the check ran on the sparse checkout (config.json only); the completeness audit then re-checked this
        # compliant verdict on a full checkout, which is what downloads the rest
        self.assertEqual(self.disc('demo/done')['targets'][0]['check_mode'], 'sparse')
        self.assertTrue(any(t['stage'] == 'checkout' and t.get('mode') == 'full' for t in self.timings()))

    def test_identical_files_are_checked_once_and_unchanged_tips_are_not_fetched(self):
        self.discover(workers=1)  # concurrent discovery may check identical files twice (harmless race)
        entries = {e['id']: e for r in ('demo/done', 'demo/feature-branch') for e in self.disc(r)['targets']}
        same = [entries[common.target_id('demo/done', 'main')], entries[common.target_id('demo/feature-branch', 'main')]]
        self.assertEqual(sorted(e['check_cached'] for e in same), [False, True])  # identical config.json
        fetches = sum(t['stage'] in ('fetch', 'clone') for t in self.timings())
        self.discover('demo/direct', 'demo/done')
        self.assertEqual(sum(t['stage'] in ('fetch', 'clone') for t in self.timings()), fetches)
        self.assertTrue(all(e['check_cached'] for e in self.disc('demo/done')['targets']))

    def test_api_discovery_clones_only_repos_that_need_the_change(self):
        out = {r['repo']: r for r in self.ok('recipe_run.py', 'discover', '--run', self.run_dir, '--pending',
                                            '--via', 'api', '--workspaces', self.tmp / 'ws', '--verbose')}
        self.assertFalse((self.tmp / 'ws' / 'demo-done').exists())
        self.assertFalse((self.tmp / 'ws' / 'demo-missing').exists())
        self.assertTrue((self.tmp / 'ws' / 'demo-direct').exists())
        self.assertIn('via API', self.disc('demo/done')['reason'])
        p = self.plan()
        # every branch, API mode included: demo/feature-branch's feature/checkout is found too
        self.assertEqual(p['counts'], {'needs_change': 4, 'compliant': 2, 'excluded': 0, 'unresolved': 2})
        self.assertTrue(any(t['stage'] == 'api-read' for t in self.timings()))
        self.assertEqual(out['demo/unreachable']['outcome'], 'unreadable')
        self.assertTrue(any(t['stage'] == 'ls-remote' and t.get('failed') for t in self.timings()))

    def test_streaming_discovery_new_targets_need_approval(self):
        self.discover('demo/direct', 'demo/feature-branch')
        p = self.plan('--allow-missing')
        self.assertIn('demo/done', p['pending_repos'])
        self.assertIn('discovery still running', (self.run_dir / 'preview.md').read_text())
        self.approve(pilots=[DIRECT])
        self.discover()  # the rest, while the pilot runs
        self.assertEqual(sorted(self.plan()['needs_approval']), sorted([PROPS, BROKEN]))  # new targets: not joined
        self.deliver(DIRECT)
        self.ci_passed(DIRECT, self.open_pr(DIRECT))
        self.assertEqual(sorted(self.ready_items('deliver')), [FEATURE])
        self.approve()
        self.assertEqual(sorted(self.ready_items('deliver')), sorted([FEATURE, PROPS, BROKEN]))

    def test_partial_clone_is_off_unless_enabled_for_the_host(self):
        (self.tmp / 'limits.json').write_text('{}')
        self.discover('demo/done')
        clone = self.tmp / 'ws' / 'demo-done'
        self.assertEqual(self.git(clone, 'config', 'remote.origin.promisor', check=False).stdout.strip(), '')

    def test_validate_ahead_runs_before_approval_and_delivery_reuses_it(self):
        self.discover()
        self.plan()
        out = {r['target']: r['status'] for r in self.ok('recipe_run.py', 'deliver', '--run', self.run_dir, '--dry',
                                                        '--target', DIRECT, FEATURE, PROPS, BROKEN)}
        self.assertEqual(out[DIRECT], 'validated')
        self.assertEqual(out[BROKEN], 'blocked')  # the pre-existing failure surfaces before approval
        self.assertTrue(any(q['target'] == BROKEN for q in self.questions()))
        builds = sum(t['stage'] == 'build' for t in self.timings())
        self.approve()
        self.assertEqual(self.deliver(DIRECT)[DIRECT]['status'], 'pushed')
        self.assertEqual(sum(t['stage'] == 'build' for t in self.timings()), builds)  # no second build
        self.assertIn('Bottleneck', self.ok('run_state.py', 'report', '--run', self.run_dir) and
                      (self.run_dir / 'report.md').read_text())

    def test_ci_window_holds_new_work(self):
        self.ready()
        self.deliver(DIRECT)
        pr = self.open_pr(DIRECT)
        self.assertEqual(self.ready_items('deliver', extra=['--max-pending-ci', '1']), {})
        self.ci_passed(DIRECT, pr)
        self.assertEqual(len(self.ready_items('deliver', extra=['--max-pending-ci', '1'])), 1)

    def test_scm_adapter_opens_observes_and_closes_prs(self):
        self.ready()
        self.deliver(DIRECT, PROPS)
        opened = {r['target']: r for r in self.ok('scm.py', 'open-prs', '--run', self.run_dir)}
        self.assertEqual({r['status'] for r in opened.values()}, {'pr_opened'})
        self.assertEqual(self.manifest()['results'][DIRECT].get('violations'), None)
        self.assertEqual(self.ok('scm.py', 'open-prs', '--run', self.run_dir), [])
        self.ok('sandbox.py', 'ci', '--store', self.store, '--id', 1)
        status = self.ok('scm.py', 'pr-status', '--run', self.run_dir)
        self.assertEqual(status['prs'], {'open/CI passed': 1, 'open/CI pending': 1})
        self.assertEqual(len(status['changed']), 1)  # only the PR whose CI finished is reported as changed
        self.ok('run_state.py', 'abort', '--run', self.run_dir, '--targets', PROPS, '--evidence', 'User: drop it')
        self.assertEqual(self.ok('scm.py', 'close-prs', '--run', self.run_dir, '--target', PROPS,
                                 '--comment', 'Withdrawn')[0]['status'], 'closed')
        prs = json.loads((self.store / 'prs.json').read_text())
        self.assertEqual(sorted(p['state'] for p in prs), ['closed', 'open'])
        repos = self.ok('scm.py', 'list-repos', '--host', 'local', '--project', 'demo', '--out', self.tmp / 'r.txt')
        self.assertEqual(repos['repos'], 8)


class RecipeLevelTests(Flow, unittest.TestCase):
    def setUp(self):
        self.recipe_copy = Path(tempfile.mkdtemp(prefix='mrt-recipe-')) / 'check-only'
        shutil.copytree(ROOT / 'examples/disable-feature-builds', self.recipe_copy)
        md = self.recipe_copy / 'recipe.md'
        md.write_text(md.read_text().replace('Validation: full', 'Validation: check'))
        self.recipe = str(self.recipe_copy)
        super().setUp()

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.recipe_copy.parent, ignore_errors=True)

    def test_check_level_recipe_skips_the_local_build(self):
        self.ready()
        r = self.deliver(BROKEN)[BROKEN]  # its build always fails; a check-level recipe doesn't run it
        self.assertEqual(r['status'], 'pushed')

    def test_trial_warns_when_check_paths_miss_a_file(self):
        md = self.recipe_copy / 'recipe.md'
        md.write_text(md.read_text().replace('Check paths: config.json, build.properties', 'Check paths: build.properties'))
        clone = self.tmp / 'c'
        self.ok('git_probe.py', 'prepare', '--url', self.sbx / 'remotes/demo__direct.git', '--path', clone)
        o = self.ok('recipe_run.py', 'trial', '--recipe', self.recipe_copy, '--clone', clone, '--branch', 'main')
        self.assertIn('reads other files', o['warning'])


class ScanTests(Env, unittest.TestCase):
    """repo-scan: read-only, no run, many repos at once."""

    def setUp(self):
        super().setUp()
        self.sbx = self.tmp / 'sbx'
        self.ok('sandbox.py', 'make', '--out', self.sbx)
        self.version_check = self.tmp / 'flag.py'
        self.version_check.write_text(
            "import json, sys\nfrom pathlib import Path\n"
            "p = Path('config.json')\n"
            "if not p.is_file():\n    print(json.dumps({'status': 'not_applicable', 'evidence': 'no config.json'})); sys.exit()\n"
            "try:\n    v = json.loads(p.read_text()).get('featureBuilds')\n"
            "except Exception as e:\n    print(json.dumps({'status': 'unknown', 'evidence': str(e)})); sys.exit()\n"
            "print(json.dumps({'status': 'compliant' if v is False else 'needs_change', 'value': str(v), 'evidence': 'config.json'}))\n")

    def scan(self, *args):
        return self.ok('mr.py', 'scan', '--check', self.version_check, '--paths', 'config.json', *args)

    def test_scan_answers_across_repos_with_values(self):
        o = self.scan('--repos', self.sbx / 'repos.txt', '--name', 's1')
        self.assertEqual(o['repos'], 8)
        self.assertEqual(o['by_value'], {'True': 3, 'False': 3})
        self.assertEqual(o['by_status']['not_applicable'], 2)  # properties and missing have no config.json
        self.assertEqual([x['repo'] for x in o['not_scanned']], ['demo/unreachable'])
        report = Path(o['report']).read_text()
        self.assertIn('## By value', report)
        self.assertTrue(Path(o['csv']).read_text().startswith('repo,branch,sha,status,value,evidence'))
        self.assertFalse(list((self.tmp / 'home').glob('runs/*')))  # no run was created

    def test_identical_files_are_checked_once_even_concurrently(self):
        o = self.scan('--repos', self.sbx / 'repos.txt', '--workers', '8')
        rows = json.loads((Path(o['report']).parent / 'results.json').read_text())
        same = [b for r in rows if r['repo'] in ('demo/done', 'demo/feature-branch') for b in r['branches'] if b['branch'] == 'main']
        self.assertEqual(sorted(b['cached'] for b in same), [False, True])

    def test_scan_by_project_through_the_adapter(self):
        o = self.scan('--project', 'demo', '--host', 'local', '--name', 'byproj')
        self.assertEqual(o['repos'], 8)
        self.assertTrue((self.tmp / 'home' / 'scans' / 'byproj' / 'repos-demo.txt').is_file())

    def test_branches_known_to_be_old_are_not_fetched_again(self):
        rem = json.loads(self.raw('git_probe.py', 'ls-remote', '--url', f"file://{self.sbx}/remotes/demo__feature-branch.git").stdout)
        dates = self.tmp / 'home' / 'cache' / 'tip-dates.json'
        dates.parent.mkdir(parents=True, exist_ok=True)
        dates.write_text(json.dumps({rem['heads']['feature/checkout']: '2020-01-01T00:00:00+00:00'}))
        (self.tmp / 'r.txt').write_text(f"demo/feature-branch file://{self.sbx}/remotes/demo__feature-branch.git\n")
        o = self.scan('--repos', self.tmp / 'r.txt', '--days', '30')
        self.assertEqual(o['branches'], 1)  # only main: feature/checkout is known to be old
        clone = self.tmp / 'home' / 'clones' / 'demo-feature-branch'
        self.assertEqual(self.git(clone, 'rev-parse', '--verify', '--quiet', 'origin/feature/checkout', check=False).stdout, '')

    def test_mr_resolves_run_and_recipe_names(self):
        card = self.ok('sandbox.py', 'jira-get', '--dir', self.sbx, '--key', 'ENG-900')
        self.ok('mr.py', 'init', '--id', 'named', '--recipe', 'disable-feature-builds', '--jira', 'ENG-900',
                '--jira-title', card['title'], '--jira-url', card['url'], '--jira-evidence', 'x',
                '--repos', self.sbx / 'repos.txt', '--request', 'x')
        self.assertEqual([r['run_id'] for r in self.ok('mr.py', 'runs')], ['named'])
        self.assertIn('report', self.ok('mr.py', 'report', '--run', 'named'))
        self.assertEqual(self.raw('mr.py', 'nope').returncode, 2)


class ReviewRegressionTests(Flow, unittest.TestCase):
    """Each test reproduces a failure from the v3.2.1 framework review and checks the fix."""

    def move_destination(self, repo='direct', files=None):
        other = self.tmp / ('o-' + repo)
        if not other.exists():
            self.git(self.tmp, 'clone', '-q', self.sbx / f'remotes/demo__{repo}.git', other)
        for name, text in (files or {'README.md': 'moved\n'}).items():
            (other / name).write_text(text)
        self.git(other, 'add', '-A')
        self.git(other, 'commit', '-qm', 'someone else')
        self.git(other, 'push', '-q', 'origin', 'main')

    def maybe_recipe(self):
        rc = self.tmp / 'rc'
        shutil.copytree(ROOT / 'examples/disable-feature-builds', rc)
        (rc / 'check.py').write_text("import json;print(json.dumps({'status':'maybe','evidence':'look'}))\n")
        self.ok('run_state.py', 'revise', '--run', self.run_dir, '--recipe', rc, '--evidence', 'x')

    def test_1_moved_base_revalidates_instead_of_reusing_an_old_build(self):
        self.discover()
        self.plan()
        self.deliver(DIRECT, extra=['--dry'])
        self.approve()
        self.move_destination(files={'build.sh': 'exit 47\n'})
        self.deliver(DIRECT)  # blocked: moved
        self.discover('demo/direct')
        self.plan()
        self.ok('run_state.py', 'retry', '--run', self.run_dir, '--target', DIRECT, '--evidence', 'rediscovered')
        builds = sum(t['stage'] == 'build' for t in self.timings())
        r = self.deliver(DIRECT)[DIRECT]
        self.assertNotEqual(r['status'], 'pushed')
        self.assertGreater(sum(t['stage'] == 'build' for t in self.timings()), builds)

    def test_2_replay_conflict_keeps_the_edit_and_stops_the_target(self):
        self.discover()
        self.plan()
        wt = Path(self.target(DIRECT)['worktree'])
        (wt / 'custom.txt').write_text('agent work\n')
        self.move_destination(files={'custom.txt': 'theirs\n'})
        self.discover('demo/direct')
        self.assertEqual((wt / 'custom.txt').read_text(), 'agent work\n')
        q = [q for q in self.questions() if q['target'] == DIRECT]
        self.assertIn('no longer applies', q[0]['text'])
        cp = load_target_info(wt)['replay']['checkpoint']
        self.assertIn('agent work', Path(cp).read_text())
        self.plan('--allow-missing')
        self.assertEqual(self.target(DIRECT)['disposition'], 'unresolved')

    def test_2b_clean_replay_still_moves_the_edit_forward(self):
        self.discover()
        self.plan()
        wt = Path(self.target(DIRECT)['worktree'])
        (wt / 'custom.txt').write_text('agent work\n')
        self.move_destination()
        self.discover('demo/direct')
        self.assertEqual((wt / 'custom.txt').read_text(), 'agent work\n')
        self.assertTrue((wt / 'README.md').read_text().startswith('moved'))

    def test_3_reconciling_an_existing_pr_keeps_fields_intact(self):
        self.ready()
        self.deliver(DIRECT)
        self.ok('scm.py', 'open-prs', '--run', self.run_dir)
        url = self.manifest()['results'][DIRECT]['pr_url']
        self.record(DIRECT, 'pushed')  # e.g. a follow-up push, or a resume after a timeout
        self.ok('scm.py', 'open-prs', '--run', self.run_dir, '--target', DIRECT)
        r = self.manifest()['results'][DIRECT]
        self.assertEqual((r['pr_url'], r['pr_state'], r['validation'], r['status']), (url, 'open', 'passed', 'pr_updated'))
        self.assertIn('does not look like a PR link', self.blocked('run_state.py', 'record', '--run', self.run_dir, '--target',
                                                                   DIRECT, '--status', 'observed', '--pr-url', 'open',
                                                                   '--evidence', 'x'))

    def test_4_parallel_decisions_on_one_repo_all_survive(self):
        import threading
        import recipe_run
        self.maybe_recipe()
        self.discover('demo/feature-branch')
        f = self.run_dir / 'discovery' / 'demo-feature-branch.json'
        for _ in range(10):
            d = json.loads(f.read_text())
            for e in d['targets']:
                e['status'] = 'maybe'
            f.write_text(json.dumps(d))
            ts = [threading.Thread(target=recipe_run.decide, args=(str(self.run_dir), e['id'], 'compliant', 'e'))
                  for e in d['targets']]
            [t.start() for t in ts]
            [t.join() for t in ts]
            self.assertTrue(all(e['status'] == 'compliant' for e in json.loads(f.read_text())['targets']))

    def test_6_draft_exception_advances_once_and_stays_visible(self):
        self.ready()
        self.deliver(BROKEN)
        q = self.questions()[0]
        self.ok('run_state.py', 'answer', '--run', self.run_dir, '--question', q['id'], '--answer', 'draft it',
                '--choice', 'draft', '--evidence', 'User: open as draft')
        r = self.deliver(BROKEN)[BROKEN]
        self.assertEqual(r['status'], 'pushed')
        self.assertEqual(self.manifest()['results'][BROKEN]['validation'], 'exception')
        t = self.target(BROKEN)
        cur = self.git(t['worktree'], 'rev-parse', f"origin/{t['branch']}").stdout.strip()
        g = self.ok('run_state.py', 'gate', '--run', self.run_dir, '--target', BROKEN, '--action', 'create_pr', '--current-sha', cur)
        self.assertEqual(g['pr_mode'], 'draft')
        self.ok('run_state.py', 'report', '--run', self.run_dir)
        self.assertIn('baseline build already failing', (self.run_dir / 'jira-table.md').read_text())
        self.assertEqual([x for x in self.questions() if x['target'] == BROKEN], [])

    def test_7_api_maybe_then_decide_clones_lazily(self):
        self.maybe_recipe()
        self.ok('recipe_run.py', 'discover', '--run', self.run_dir, '--repo', 'demo/direct', '--via', 'api',
                '--workspaces', self.tmp / 'ws')
        self.plan('--allow-missing')
        self.ok('recipe_run.py', 'decide', '--run', self.run_dir, '--target', DIRECT, '--status', 'needs_change',
                '--evidence', 'featureBuilds is on')
        self.plan('--allow-missing')
        self.assertTrue(Path(self.target(DIRECT)['worktree']).is_dir())

    def test_8_ci_window_counts_work_in_flight(self):
        self.discover()
        self.plan()
        self.approve(extra=['--max-pending-ci', '1'])
        a = self.ok('run_state.py', 'take', '--run', self.run_dir, '--worker', 'w1', '--task', 'deliver', '--limit', '5')
        b = self.ok('run_state.py', 'take', '--run', self.run_dir, '--worker', 'w2', '--task', 'deliver', '--limit', '5')
        self.assertEqual(len(a) + len(b), 1)
        other = next(t for t in (DIRECT, PROPS, FEATURE) if t != a[0]['target'])
        self.assertIn('CI window full', self.deliver(other)[other]['reason'])
        self.assertEqual(self.deliver(a[0]['target'], extra=['--worker', 'w1'])[a[0]['target']]['status'], 'pushed')

    def test_diagnose_finds_stranded_work(self):
        self.ready()
        self.deliver(DIRECT)
        probs = self.ok('recipe_run.py', 'diagnose', '--run', self.run_dir)['problems']
        self.assertIn('pushed, but no PR recorded', [p['problem'] for p in probs if p['target'] == DIRECT])

    def test_progress_separates_pr_state_from_remediation_and_clean_frees_worktrees(self):
        self.ready()
        self.deliver(DIRECT)
        pr = self.open_pr(DIRECT)
        self.ok('sandbox.py', 'pr-merge', '--store', self.store, '--id', pr['id'])
        o = self.ok('recipe_run.py', 'progress', '--run', self.run_dir)
        rows = {r['target']: r['state'] for r in json.loads(Path(o['report']).with_suffix('.json').read_text())}
        self.assertEqual(rows[DIRECT], 'remediated')
        self.assertEqual(rows[FEATURE], 'still needs change')
        self.record(DIRECT, 'merged', '--pr-url', pr['url'], '--pr-state', 'merged')
        self.assertIn(DIRECT, self.ok('recipe_run.py', 'clean', '--run', self.run_dir)['removed'])
        self.assertFalse(Path(self.target(DIRECT)['worktree']).exists())

    def test_discover_prints_a_summary_not_every_branch(self):
        o = self.ok('recipe_run.py', 'discover', '--run', self.run_dir, '--pending', '--workspaces', self.tmp / 'ws')
        self.assertEqual(o['repos'], 8)
        self.assertTrue(Path(o['details']).is_file())
        self.assertLess(len(json.dumps(o)), 1200)


class AgentRetryTests(AgentModeTests):
    def test_5_failed_validation_keeps_the_claim_then_succeeds(self):
        self.discover()
        self.plan()
        self.approve(DIRECT, pilots=[DIRECT])
        item = self.ok('run_state.py', 'take', '--run', self.run_dir, '--worker', 'w1', '--task', 'change')[0]
        self.assertEqual(item['target'], DIRECT)
        r = self.deliver(DIRECT, extra=['--dry'])[DIRECT]
        self.assertEqual(r['status'], 'validation_failed')
        self.assertEqual(self.manifest()['claims'][DIRECT]['worker'], 'w1')
        wt = Path(self.target(DIRECT)['worktree'])
        (wt / 'config.json').write_text((wt / 'config.json').read_text().replace('true', 'false'))
        self.assertEqual(self.deliver(DIRECT, extra=['--dry'])[DIRECT]['status'], 'validated')
        self.assertEqual(self.ready_items()[DIRECT], 'review')
        renew = self.ok('run_state.py', 'take', '--run', self.run_dir, '--worker', 'w9', '--renew')
        self.assertEqual(renew['renewed'], [])

    def test_agent_gate_binds_review_to_the_base(self):
        self.discover()
        self.plan()
        self.approve(DIRECT, pilots=[DIRECT])
        wt = Path(self.target(DIRECT)['worktree'])
        (wt / 'config.json').write_text((wt / 'config.json').read_text().replace('true', 'false'))
        self.deliver(DIRECT, extra=['--dry'])
        self.record(DIRECT, 'observed', '--reviewed')
        m = self.manifest()['results'][DIRECT]
        self.assertEqual(m['reviewed_base'], m['validated_base'])


def load_target_info(wt):
    return json.loads((Path(wt) / '.git').read_text().split('gitdir:')[1].strip() and
                      (Path(subprocess.run(['git', '-C', str(wt), 'rev-parse', '--git-dir'], capture_output=True,
                                           text=True).stdout.strip()) / 'rollout-target.json').read_text())


class RealWorldTests(Env, unittest.TestCase):
    """Behaviors found by the realistic simulation runs (tests/simulation)."""

    def setUp(self):
        super().setUp()
        self.remotes = self.tmp / 'remotes'
        self.remotes.mkdir()
        self.lines = []
        (self.tmp / 'sbx' / 'scm').mkdir(parents=True)
        (self.tmp / 'sbx' / 'scm' / 'prs.json').write_text('[]')

    def repo(self, name, files, develop=False, policy=False):
        bare = self.remotes / (name.replace('/', '__') + '.git')
        self.git(self.tmp, 'init', '-q', '--bare', '-b', 'main', bare)
        if policy:
            h = bare / 'hooks' / 'pre-receive'
            h.write_text('#!/bin/sh\nwhile read o n ref; do case "$ref" in refs/heads/main|refs/heads/develop|'
                         'refs/heads/feature/*) ;; *) echo "Branch $ref does not match the branching model" >&2; exit 1;; '
                         'esac; done\n')
            h.chmod(0o755)
        w = self.tmp / 'work' / name.replace('/', '__')
        w.mkdir(parents=True)
        self.git(w, 'init', '-q', '-b', 'main')
        for k, v in files.items():
            (w / k).write_text(v)
            if k.endswith('.sh'):
                (w / k).chmod(0o755)
        self.git(w, 'add', '-A')
        self.git(w, 'commit', '-qm', 'init')
        self.git(w, 'push', '-q', f'file://{bare}', 'main')
        if develop:
            (w / 'DEV.md').write_text('dev\n')
            self.git(w, 'add', '-A')
            self.git(w, 'commit', '-qm', 'dev')
            self.git(w, 'push', '-q', f'file://{bare}', 'HEAD:develop')
        self.lines.append(f'{name} file://{bare}')
        return bare

    def start(self, run='r', fmt=None):
        (self.tmp / 'repos.txt').write_text('\n'.join(self.lines) + '\n')
        self.ok('run_state.py', 'init', '--root', self.tmp / 'runs', '--id', run, '--recipe', 'examples/disable-feature-builds',
                '--jira', 'ENG-1', '--jira-title', 't', '--jira-url', 'u', '--jira-evidence', 'e',
                '--repos', self.tmp / 'repos.txt', '--request', 'x', *(['--branch-format', fmt] if fmt else []))
        self.run_dir = self.tmp / 'runs' / run
        return self.ok('recipe_run.py', 'discover', '--run', self.run_dir, '--pending', '--workspaces', self.tmp / 'ws')

    def plan(self):
        return self.ok('run_state.py', 'plan', '--run', self.run_dir, '--strategy', 's')

    def deliver(self, *tids):
        return {r['target']: r for r in self.ok('recipe_run.py', 'deliver', '--run', self.run_dir, '--target', *tids)}

    CFG = '{\n  "featureBuilds": true\n}\n'

    def add_branches(self, bare, names, old=()):
        w = self.tmp / 'br' / bare.name
        self.git(self.tmp, 'clone', '-q', f'file://{bare}', w)
        for n in names:
            env = dict(self.env, **({'GIT_COMMITTER_DATE': '2020-01-01T00:00:00', 'GIT_AUTHOR_DATE': '2020-01-01T00:00:00'}
                                    if n in old else {}))
            self.git(w, 'checkout', '-q', '-B', n, 'origin/main')
            (w / (n.replace('/', '_') + '.txt')).write_text(n)
            subprocess.run(['git', '-C', str(w), 'add', '-A'], env=env, check=True)
            subprocess.run(['git', '-C', str(w), 'commit', '-qm', n], env=env, check=True)
            self.git(w, 'push', '-q', 'origin', n)

    NAMES = ['master', 'develop', 'develop-2', 'Develop_orders', 'release/1.x', 'hotfix/urgent', 'users/bob/tmp',
             'feature/develop-sync']

    def test_every_branch_is_found_whatever_its_name(self):
        bare = self.repo('A/many', {'config.json': self.CFG, 'build.sh': 'exit 0\n'})
        self.add_branches(bare, self.NAMES)
        d = self.start()
        self.assertEqual(d['coverage']['scanned'], 1 + len(self.NAMES))
        self.assertEqual(d['coverage']['excluded_by_type'], 0)
        self.assertEqual(self.plan()['counts']['needs_change'], 1 + len(self.NAMES))

    def test_recipe_branch_types_and_cutoff_narrow_scope_and_report_what_was_left_out(self):
        bare = self.repo('A/typed', {'config.json': self.CFG, 'build.sh': 'exit 0\n'})
        self.add_branches(bare, self.NAMES + ['develop-legacy'], old={'develop-legacy'})
        rc = self.tmp / 'rc'
        shutil.copytree(ROOT / 'examples/disable-feature-builds', rc)
        md = rc / 'recipe.md'
        md.write_text(md.read_text().replace('Branches: all', 'Branches: develop*, release/*; active within 30 days'))
        (self.tmp / 'repos.txt').write_text('\n'.join(self.lines) + '\n')
        self.ok('run_state.py', 'init', '--root', self.tmp / 'runs', '--id', 'r', '--recipe', rc, '--jira', 'ENG-1',
                '--jira-title', 't', '--jira-url', 'u', '--jira-evidence', 'e', '--repos', self.tmp / 'repos.txt', '--request', 'x')
        self.run_dir = self.tmp / 'runs' / 'r'
        d = self.ok('recipe_run.py', 'discover', '--run', self.run_dir, '--pending', '--workspaces', self.tmp / 'ws')
        c = d['coverage']
        self.assertEqual(c['selection'], {'branch_types': ['develop*', 'release/*'], 'active_within_days': 30, 'source': 'recipe'})
        self.assertEqual((c['scanned'], c['excluded_by_age']), (4, 1))  # develop, develop-2, Develop_orders, release/1.x
        self.assertIn('A/typed: feature/develop-sync', c['possible_variants'])
        self.assertEqual(c['excluded_by_type'], 5)  # main, master, hotfix/urgent, users/bob/tmp, feature/develop-sync

    def test_rollout_branches_are_never_targets(self):
        bare = self.repo('A/own', {'config.json': self.CFG, 'build.sh': 'exit 0\n'})
        self.start()
        self.plan()
        self.ok('run_state.py', 'approve', '--run', self.run_dir, '--evidence', 'go')
        tid = common.target_id('A/own', 'main')
        self.assertEqual(self.deliver(tid)[tid]['status'], 'pushed')
        d = self.ok('recipe_run.py', 'discover', '--run', self.run_dir, '--repo', 'A/own', '--workspaces', self.tmp / 'ws')
        self.assertEqual((d['coverage']['scanned'], d['coverage']['own_rollout_branches']), (1, 1))

    def test_build_from_ci_config_is_adopted_instead_of_calling_the_branch_broken(self):
        self.repo('A/prof', {'config.json': self.CFG,
                             'build.sh': '[ "$1" = "--profile" ] && exit 0\necho "missing -Pci"; exit 1\n',
                             'pom.xml': '<project/>\n',
                             'Jenkinsfile': "pipeline { steps { sh './build.sh --profile ci' } }\n"})
        self.start()
        self.plan()
        self.ok('run_state.py', 'approve', '--run', self.run_dir, '--evidence', 'go')
        tid = common.target_id('A/prof', 'main')
        self.assertEqual(self.deliver(tid)[tid]['status'], 'pushed')
        self.assertEqual(repo_facts.get('A/prof')['build'], './build.sh --profile ci')

    def test_missing_build_tool_is_an_environment_question_not_a_broken_branch(self):
        self.repo('A/tool', {'config.json': self.CFG, 'Makefile': 'test:\n\ttrue\n'})
        self.start()
        self.plan()
        self.ok('run_state.py', 'approve', '--run', self.run_dir, '--evidence', 'go')
        tid = common.target_id('A/tool', 'main')
        repo_facts.merge('A/tool', {'build': 'definitely-not-a-tool verify'})
        r = self.deliver(tid)[tid]
        self.assertIn('cannot run on this machine', r['reason'])
        q = self.ok('run_state.py', 'questions', '--run', self.run_dir)
        self.assertEqual(q[0]['choices'], ['retry', 'skip'])

    def test_husky_style_build_does_not_disable_the_rollout(self):
        self.repo('A/husky', {'config.json': self.CFG, 'build.sh': 'git config core.hooksPath .husky; exit 0\n'})
        self.start()
        self.plan()
        self.ok('run_state.py', 'approve', '--run', self.run_dir, '--evidence', 'go')
        tid = common.target_id('A/husky', 'main')
        r = self.deliver(tid)[tid]
        self.assertEqual(r['status'], 'pushed')

    def test_default_branch_names_pass_branch_rules_and_old_formats_can_be_renamed(self):
        self.repo('A/pol', {'config.json': self.CFG, 'build.sh': 'exit 0\n'}, policy=True)
        self.start(fmt='{key}/{recipe}-{target}')
        self.plan()
        self.ok('run_state.py', 'approve', '--run', self.run_dir, '--evidence', 'go')
        tid = common.target_id('A/pol', 'main')
        r = self.deliver(tid)[tid]
        self.assertIn('server refused branch', r['reason'])
        rb = self.ok('recipe_run.py', 'branches', '--run', self.run_dir, '--format', 'feature/{key}-{recipe}')
        self.assertEqual(rb['examples'][0]['to'], 'feature/ENG-1-disable-feature-builds')
        self.plan()
        self.ok('run_state.py', 'retry', '--run', self.run_dir, '--target', tid, '--evidence', 'renamed')
        self.assertEqual(self.deliver(tid)[tid]['status'], 'pushed')

    def test_similar_questions_are_grouped_and_answered_together(self):
        for n in ('A/b1', 'A/b2'):
            self.repo(n, {'config.json': self.CFG, 'build.sh': 'echo broken; exit 1\n'})
        self.start()
        self.plan()
        self.ok('run_state.py', 'approve', '--run', self.run_dir, '--evidence', 'go')
        self.deliver(common.target_id('A/b1', 'main'), common.target_id('A/b2', 'main'))
        groups = self.ok('run_state.py', 'questions', '--run', self.run_dir)
        self.assertEqual((len(groups), groups[0]['count']), (1, 2))
        o = self.ok('run_state.py', 'answer', '--run', self.run_dir, '--question', *groups[0]['ids'], '--answer', 'skip',
                    '--choice', 'skip', '--evidence', 'User: skip')
        self.assertEqual(len(o['answered']), 2)
        self.assertEqual(len(json.loads((self.run_dir / 'manifest.json').read_text())['aborted']), 2)

    def test_plan_suggests_one_pilot_per_diff_kind(self):
        self.repo('A/j1', {'config.json': self.CFG, 'build.sh': 'exit 0\n'})
        self.repo('A/j2', {'config.json': self.CFG, 'build.sh': 'exit 0\n'})
        self.repo('A/p1', {'build.properties': 'featureBuilds=true\n', 'build.sh': 'exit 0\n'})
        self.start()
        self.assertEqual(len(self.plan()['suggested_pilots']), 2)

    def test_scripted_change_is_redone_when_the_same_file_moved(self):
        bare = self.repo('A/mv', {'config.json': '{\n  "name": "a",\n  "featureBuilds": true\n}\n', 'build.sh': 'exit 0\n'})
        self.start()
        self.plan()
        self.ok('run_state.py', 'approve', '--run', self.run_dir, '--evidence', 'go')
        other = self.tmp / 'other'
        self.git(self.tmp, 'clone', '-q', f'file://{bare}', other)
        (other / 'config.json').write_text('{\n  "name": "renamed",\n  "featureBuilds": true\n}\n')
        self.git(other, 'commit', '-qam', 'rename')
        self.git(other, 'push', '-q', 'origin', 'main')
        self.ok('recipe_run.py', 'discover', '--run', self.run_dir, '--repo', 'A/mv', '--workspaces', self.tmp / 'ws')
        tid = common.target_id('A/mv', 'main')
        self.assertEqual(self.plan()['needs_approval'], [tid])  # the re-applied diff differs: approve again
        self.assertEqual(self.ok('run_state.py', 'questions', '--run', self.run_dir), [])  # no replay-conflict question


class CompletenessTests(RealWorldTests):
    """Guards against missing a branch that needs the change."""

    OVERRIDE_CHECK = ("import json, os, sys\nfrom pathlib import Path\n"
              "v = json.loads(Path('config.json').read_text()).get('featureBuilds')\n"
              "if Path('override.json').is_file():\n    v = json.loads(Path('override.json').read_text()).get('featureBuilds', v)\n"
              "print(json.dumps({'status': 'compliant' if v is False else 'needs_change', 'evidence': 'config'}))\n")

    def test_audit_catches_a_check_reading_undeclared_files_and_rechecks_everything(self):
        for i in range(6):  # the override that turns it back on lives outside the declared Check paths
            self.repo(f'A/o{i}', {'config.json': '{"featureBuilds": false}\n', 'override.json': '{"featureBuilds": true}\n'})
        (self.tmp / 'repos.txt').write_text('\n'.join(self.lines) + '\n')
        chk = self.tmp / 'override_check.py'
        chk.write_text(self.OVERRIDE_CHECK)
        o = self.ok('mr.py', 'scan', '--check', chk, '--paths', 'config.json', '--repos', self.tmp / 'repos.txt')
        self.assertTrue(o['audit']['mismatches'])
        self.assertIn('re-checked on a full checkout', o['audit']['action'])
        self.assertEqual(o['by_status'], {'needs_change': 6})  # nothing missed after the recheck

    def test_lfs_pointer_is_unknown_not_a_verdict(self):
        self.repo('A/lfs', {'config.json': 'version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 12\n'})
        (self.tmp / 'repos.txt').write_text('\n'.join(self.lines) + '\n')
        o = self.ok('mr.py', 'scan', '--check', 'disable-feature-builds', '--repos', self.tmp / 'repos.txt')
        rows = json.loads((Path(o['report']).parent / 'results.json').read_text())
        self.assertEqual(rows[0]['branches'][0]['status'], 'unknown')
        self.assertIn('Git LFS', rows[0]['branches'][0]['evidence'])


if __name__ == '__main__':
    unittest.main()
