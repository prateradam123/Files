#!/usr/bin/env python3
"""Run a recipe's check/apply scripts and record per-repo discovery results.

Recipe folder contract (see examples/recipe-template.md):
  recipe.md            what and why, for humans and agents
  check.py|check.sh    REQUIRED. Run with cwd = a checkout of one repo at one commit. Prints one JSON line:
                       {"status": "compliant|needs_change|not_applicable|unknown", "evidence": "..."}
  apply.py|apply.sh    optional. Run with cwd = the target worktree. Edits files in place, idempotently.
                       Prints {"changed": true|false, "notes": "..."}; non-zero exit means "agent needed".
The same check decides discovery, verifies the edit, and later confirms the destination is done.
"""
import argparse
import contextlib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CHECK_STATUSES, DEV_BRANCHES, TOOLKIT, branch_matches, branch_name, data_home, dir_hash, find_script, git, recipes_dir, interpreter_for, load_target, now,  # noqa: E402
                    recipe_meta, toolkit_version,
                    patch_hash, read_json, require, shape_hash, slug, target_file, target_id,
                    worktree_patch, write_json)
import run_state  # noqa: E402
import repo_facts  # noqa: E402
import git_probe  # noqa: E402
import limits  # noqa: E402


def _run(script, cwd, timeout, extra_env=None):
    env = dict(os.environ, RECIPE_DIR=str(Path(script).parent.resolve()), TOOLKIT_DIR=str(TOOLKIT),
               **(extra_env or {}))
    try:
        p = subprocess.run(interpreter_for(script), cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, f'timed out after {timeout}s'
    out = None
    for line in reversed(p.stdout.strip().splitlines()):
        try:
            v = json.loads(line)
            if isinstance(v, dict):
                out = v
                break
        except json.JSONDecodeError:
            continue
    if p.returncode:
        return None, (p.stderr.strip() or p.stdout.strip())[-600:] or f'exit {p.returncode}'
    if out is None:
        return None, 'no JSON line on stdout'
    return out, None


def run_check(recipe_dir, tree, env=None):
    script = find_script(recipe_dir, 'check')
    if not script:
        return {'status': 'unknown', 'evidence': 'recipe has no check script; write one before running'}
    out, err = _run(script, tree, 600, env)
    if err:
        return {'status': 'unknown', 'evidence': 'check failed: ' + err}
    if out.get('status') not in CHECK_STATUSES:
        return {'status': 'unknown', 'evidence': 'check returned invalid status: ' + str(out.get('status'))}
    r = {'status': out['status'], 'evidence': str(out.get('evidence', ''))[:1000]}
    if out.get('value') is not None:  # inventories: e.g. the version found, grouped in scan reports
        r['value'] = str(out['value'])[:200]
    return r


def run_apply(recipe_dir, tree, env=None):
    script = find_script(recipe_dir, 'apply')
    if not script:
        return None, 'no apply script'
    return _run(script, tree, 1800, env)


def clone_host(clone):
    key = str(clone)
    if key not in _HOSTS:
        _HOSTS[key] = git_probe.host_of(git_probe.origin_url(clone))
    return _HOSTS[key]


_HOSTS = {}


def path_matches(path, patterns):
    return any(fnmatch.fnmatchcase(path, p) or (p.startswith('**/') and fnmatch.fnmatchcase(path, p[3:]))
               for p in patterns)


def matched_files(clone, sha, patterns):
    """(path, blob id) for tracked files matching the recipe's check paths. Trees are local, even in a
    blobless clone, so this costs no network."""
    rows = []
    exact = not any(c in ''.join(patterns) for c in '*?[')
    listing = git(clone, 'ls-tree', '-r', '--full-tree', sha, *(['--', *patterns] if exact else []))
    for line in listing.splitlines():
        meta, path = line.split('\t', 1)
        mode, kind, obj = meta.split()
        if kind == 'blob' and path_matches(path, patterns):
            rows.append((path, obj))
    return rows


@contextlib.contextmanager
def temp_tree(clone, sha, run=None, repo=None):
    """A full throwaway checkout (fetches the commit's file contents once, holding a git slot)."""
    tmp = Path(tempfile.mkdtemp(prefix='rollout-check-'))
    path = tmp / 'tree'
    with limits.timed(run, 'checkout', 'git:' + clone_host(clone), repo=repo, mode='full'):
        git(clone, 'worktree', 'add', '--detach', str(path), sha, env=limits.net_env())
    try:
        yield path
    finally:
        git(clone, 'worktree', 'remove', '--force', str(path), check=False)
        git(clone, 'worktree', 'prune', check=False)
        shutil.rmtree(tmp, ignore_errors=True)


@contextlib.contextmanager
def sparse_tree(clone, sha, paths, run=None, repo=None):
    """Only the listed files, checked out into a temp folder with a temp index: the clone is untouched and
    only those blobs are downloaded."""
    tmp = Path(tempfile.mkdtemp(prefix='rollout-sparse-'))
    tree = tmp / 'tree'
    tree.mkdir()
    try:
        if paths:
            lst = tmp / 'paths'
            lst.write_text('\0'.join(paths) + '\0')
            env = dict(limits.net_env(), GIT_INDEX_FILE=str(tmp / 'index'), GIT_LITERAL_PATHSPECS='1')
            with limits.timed(run, 'checkout', 'git:' + clone_host(clone), repo=repo, mode='sparse', files=len(paths)):
                git(clone, f'--work-tree={tree}', 'checkout', sha, f'--pathspec-from-file={lst}',
                    '--pathspec-file-nul', env=env)
        yield tree
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cache_dir(clone):
    d = Path(clone).parent / '.cache' / 'checks'
    d.mkdir(parents=True, exist_ok=True)
    return d


FORCE_FULL = False  # set by --full, or by the audit when sparse checks disagree with full ones
LFS_POINTER = b'version https://git-lfs.github.com/spec/v1'


def check_commit(recipe_dir, recipe_sha, clone, sha, env, run=None, repo=None, full=False):
    """Run the recipe check on one commit as cheaply as possible, with a content-keyed cache:
    - `Check paths` declared: key = recipe + the blob ids of just those files, so identical files (templated
      repos, feature branches that never touched them) are checked once; runs on a sparse checkout.
    - otherwise: key = recipe + commit; runs on a full throwaway checkout.
    If the check script reads TARGET_REPO/TARGET_BRANCH, those join the key."""
    meta = recipe_meta(recipe_dir)
    files = matched_files(clone, sha, meta['check_paths']) if meta['check_paths'] else None
    full = full or FORCE_FULL
    basis = [toolkit_version(), recipe_sha] + ([f'{p}:{b}' for p, b in files] if files is not None and not full else [sha])
    if full:
        basis.append('full')
    if meta['check_uses_target']:
        basis += [env.get('TARGET_REPO', ''), env.get('TARGET_BRANCH', '')]
    key = hashlib.sha256('\n'.join(basis).encode()).hexdigest()
    cf = cache_dir(clone) / f'{key}.json'
    with cache_lock(cf):
        if cf.is_file():
            limits.record(run, 'check', 0, repo=repo, cached=True)
            return {**read_json(cf), 'cached': True}
        sparse = files is not None and len(files) <= 500 and not full
        with (sparse_tree(clone, sha, [p for p, _ in files], run, repo) if sparse else temp_tree(clone, sha, run, repo)) as tree:
            lfs = [p for p, _ in (files or []) if (tree / p).is_file() and (tree / p).read_bytes()[:len(LFS_POINTER)] == LFS_POINTER]
            if lfs:  # the real content isn't here: never guess from a pointer
                out = {'status': 'unknown', 'evidence': 'stored in Git LFS (content not downloaded): ' + ', '.join(lfs[:3])}
            else:
                with limits.timed(run, 'check', repo=repo, mode='sparse' if sparse else 'full'):
                    out = run_check(recipe_dir, tree, env)
        out['mode'] = 'sparse' if sparse else 'full'
        if not out['evidence'].startswith('check failed'):  # never cache a crash or timeout
            write_json(cf, out)
        return out


@contextlib.contextmanager
def cache_lock(cache_file):
    with open(str(cache_file) + '.lock', 'a+') as f:
        limits._lock(f)
        try:
            yield
        finally:
            limits._unlock(f)


def discovery_path(run, repo):
    return Path(run) / 'discovery' / (slug(repo, 80) + '.json')


def load_discovery(run, repo, url=None, clone=None):
    p = discovery_path(run, repo)
    d = read_json(p) if p.exists() else {'repo': repo, 'url': url, 'complete': False, 'targets': [], 'questions': []}
    if clone:
        d['clone'] = str(Path(clone).resolve())
    if url:
        d['url'] = url
    return p, d


def source_branch_for(s, fmt, d, repo, branch, tid):
    old = next((e for e in d.get('targets', []) if e['id'] == tid), {})
    if old.get('source_branch') and old.get('worktree') and Path(old['worktree']).exists():
        return old['source_branch']  # never rename a branch that already has a worktree (mr branches does that)
    dev = branch in DEV_BRANCHES or branch == repo_facts.get(repo).get('dev_branch')
    key, recipe = s['jira']['key'], s['recipe']['slug']
    taken = {e.get('source_branch') for e in d.get('targets', []) if e['id'] != tid}
    name = branch_name(fmt, key, recipe, tid, branch, dev)
    if name in taken:
        name = branch_name(fmt, key, recipe, tid, branch, dev=False)
    if name in taken:
        name = f'{name}-{tid[-5:]}'
    return name


@contextlib.contextmanager
def edit_discovery(run, repo, url=None, clone=None):
    """The only way to change a discovery file: read, modify and write under a per-repo lock, so parallel
    workers deciding different branches of the same repo can't overwrite each other."""
    path = discovery_path(run, repo)
    with cache_lock(path):
        _, d = load_discovery(run, repo, url, clone)
        yield d
        write_json(path, d)


def find_entry(run, tid):
    for f in (Path(run) / 'discovery').glob('*.json'):
        d = read_json(f)
        e = next((x for x in d['targets'] if x['id'] == tid), None)
        if e:
            return d, e
    raise ValueError('Unknown target ' + tid)


def upsert(d, entry):
    d['targets'] = [e for e in d['targets'] if e['id'] != entry['id']] + [entry]
    d['targets'].sort(key=lambda e: e['id'])


def changed_files(patch):
    return sorted({line.split(' b/', 1)[1] for line in patch.splitlines() if line.startswith('diff --git ')})


def capture_into(run, entry, recipe_dir, wt):
    t = load_target(wt)
    text = worktree_patch(wt, t['base_sha'])
    post = run_check(recipe_dir, wt, {'TARGET_REPO': entry['repo'], 'TARGET_BRANCH': entry['branch']})
    entry['post_check'] = post
    if not text.strip():
        for k in ('preview_patch', 'preview_patch_sha256', 'preview_shape'):
            entry.pop(k, None)
        entry['files'] = []
        return entry
    p = Path(run) / 'previews' / (entry['id'] + '.patch')
    p.write_text(text)
    entry.update(preview_patch=str(p.resolve()), preview_patch_sha256=patch_hash(text),
                 preview_shape=shape_hash(text), files=changed_files(text), captured_at=now())
    return entry


def ensure_worktree(clone, wt, src, sha, meta):
    """Create (or reuse) the target worktree on its own source branch. If the destination moved under an
    uncommitted edit, the edit is saved as a checkpoint and replayed (3-way) in a throwaway worktree first;
    only a clean replay is applied to the real one. On conflict the worktree is left exactly as it was."""
    wt = Path(wt)
    if wt.exists():
        t = load_target(wt)
        require(t and t['target'] == meta['target'], f'{wt} exists but is not this target\'s worktree')
        if git(wt, 'rev-list', f"{t['base_sha']}..HEAD").split() or t['base_sha'] == sha:
            return t  # committed work, or nothing moved: keep the worktree exactly as it is
        old = worktree_patch(wt, t['base_sha'])
        if old.strip() and meta.get('redo_if_patch') and patch_hash(old) == meta['redo_if_patch']:
            # Exactly what the recipe's apply script produced, untouched since: redo it on the new base
            # (preview re-applies) rather than replaying a stale diff. Kept as a checkpoint anyway.
            ck = Path(meta['run']) / 'checkpoints' / f"{meta['target']}-{t['base_sha'][:10]}-{int(time.time())}.patch"
            ck.parent.mkdir(parents=True, exist_ok=True)
            ck.write_text(old)
            git(wt, 'reset', '--hard', sha)
            git(wt, 'clean', '-fd')
            t.update(base_sha=sha, replay={'status': 'redone by the apply script', 'checkpoint': str(ck)})
            write_json(target_file(wt), t)
            return t
        if old.strip():
            ck = Path(meta['run']) / 'checkpoints' / f"{meta['target']}-{t['base_sha'][:10]}-{int(time.time())}.patch"
            ck.parent.mkdir(parents=True, exist_ok=True)
            ck.write_text(old)
            tmp = Path(tempfile.mkdtemp(prefix='rollout-replay-'))
            try:
                git(wt, 'worktree', 'add', '--detach', str(tmp / 'tree'), sha)
                r = subprocess.run(['git', '-C', str(tmp / 'tree'), 'apply', '--3way', '--whitespace=nowarn'],
                                   input=old, capture_output=True, text=True)
            finally:
                git(wt, 'worktree', 'remove', '--force', str(tmp / 'tree'), check=False)
                shutil.rmtree(tmp, ignore_errors=True)
            if r.returncode or 'conflict' in (r.stdout + r.stderr).lower():
                t['replay'] = {'status': 'conflict', 'checkpoint': str(ck), 'new_base': sha,
                               'error': (r.stderr or r.stdout).strip()[-300:]}
                write_json(target_file(wt), t)
                return t
            git(wt, 'reset', '--hard', sha)
            git(wt, 'clean', '-fd')
            git(wt, 'apply', '--3way', '--whitespace=nowarn', str(ck))
            git(wt, 'reset', '-q')  # leave the replayed change unstaged, like a fresh edit
            t['replay'] = {'status': 'ok', 'checkpoint': str(ck)}
        else:
            git(wt, 'reset', '--hard', sha)
            git(wt, 'clean', '-fd')
        t['base_sha'] = sha
        write_json(target_file(wt), t)
        return t
    wt.parent.mkdir(parents=True, exist_ok=True)
    exists = git(clone, 'rev-parse', '--verify', '--quiet', 'refs/heads/' + src, check=False).strip()
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')  # full checkout (LFS included: builds may need it)
    with limits.timed(meta.get('run_dir'), 'worktree', 'git:' + clone_host(clone), repo=meta['repo']):
        if exists:
            git(clone, 'worktree', 'add', str(wt), src, env=env)
        else:
            git(clone, 'worktree', 'add', '-b', src, str(wt), sha, env=env)
    t = {**{k: v for k, v in meta.items() if k not in ('run_dir', 'redo_if_patch')}, 'base_sha': sha}
    write_json(target_file(wt), t)
    return t


def preview(run, repo, clone, branch, no_apply=False, url=None, build_hint=None, sha=None):
    run = str(Path(run).resolve())
    s = run_state.load(run)
    recipe_dir, v = run_state.active_recipe(run, s)
    run_state.recipe_intact(run, s)
    clone = Path(clone).resolve()
    sha = sha or git(clone, 'rev-parse', '--verify', f'origin/{branch}^{{commit}}').strip()
    tid = target_id(repo, branch)
    _, d = load_discovery(run, repo, url, clone)
    src = source_branch_for(s, s['branch_format'], d, repo, branch, tid)
    old = next((e for e in d['targets'] if e['id'] == tid), {})
    env = {'TARGET_REPO': repo, 'TARGET_BRANCH': branch}
    chk = check_commit(recipe_dir, v['sha256'], clone, sha, env, run, repo)
    e = {'id': tid, 'repo': repo, 'branch': branch, 'observed_sha': sha, 'status': chk['status'],
         'evidence': f"{chk['evidence']} (at {branch}@{sha[:10]})", 'recipe_version': v['version'],
         'questions': old.get('questions', []), 'previewed_at': now(), 'check_mode': chk.get('mode'),
         'check_cached': bool(chk.get('cached'))}
    if old.get('worktree') and Path(old['worktree']).exists() and chk['status'] != 'needs_change':
        t = load_target(old['worktree'])
        if t and git(old['worktree'], 'rev-list', f"{t['base_sha']}..HEAD").split():
            e['note'] = 'destination no longer needs change but this target has commits; see its PR'
    if chk['status'] == 'needs_change':
        wt = clone.parent / '_wt' / s['run_id'] / tid
        redo = old.get('preview_patch_sha256') if old.get('implementation') == 'script' and not no_apply else None
        info = ensure_worktree(clone, wt, src, sha, {'run': run, 'target': tid, 'repo': repo,
                                                    'jira_key': s['jira']['key'], 'source_branch': src,
                                                    'destination': branch, 'run_dir': run, 'redo_if_patch': redo})
        if info['base_sha'] != sha and (info.get('replay') or {}).get('status') == 'conflict':
            rp = info['replay']
            text = (f"{branch} moved and this target's uncommitted edit no longer applies. Nothing was discarded: the "
                    f"worktree is unchanged at the old base, and a checkpoint is at {rp['checkpoint']}.")
            if not any(q['status'] == 'open' and q.get('target') == tid and 'no longer applies' in q['text']
                       for q in s['questions']):
                run_state.ask(run, text, 'Reapply the edit on the new base by hand, or discard it (reset the '
                              'worktree) and rediscover this repo.', tid, choices=['retry', 'skip'])
            e.update(worktree=str(wt), source_branch=src, implementation='agent', replay=rp,
                     observed_sha=info['base_sha'], status='needs_change')
            with edit_discovery(run, repo, url, clone) as d:
                upsert(d, e)
            return e
        impl, notes = 'agent', ''
        if not no_apply:
            with limits.timed(run, 'apply', repo=repo):
                out, err = run_apply(recipe_dir, wt, env)
            if err == 'no apply script':
                notes = 'no apply script; agent edits the worktree, then runs capture'
            elif err:
                notes = 'apply failed: ' + err
            else:
                notes = str(out.get('notes', ''))
                impl = 'script'
        build = repo_facts.get(repo).get('build') or build_hint
        e.update(worktree=str(wt), source_branch=src, implementation=impl, apply_notes=notes,
                 change=s['recipe']['title'] + (f' — {notes}' if notes and impl == 'script' else ''),
                 validation=(f'`{build}` in the worktree, then the recipe check must report compliant' if build
                             else 'recipe check must report compliant; repo build command not yet in repo-facts'))
        with limits.timed(run, 'capture', repo=repo):
            capture_into(run, e, recipe_dir, wt)
        if impl == 'script' and e.get('post_check', {}).get('status') != 'compliant':
            e['implementation'] = 'hybrid'
    with edit_discovery(run, repo, url, clone) as d:
        upsert(d, e)
    return e


def decide(run, tid, status, evidence):
    """A worker's judgement on a `maybe` (or, in agent recipes, `unknown`) target, with its reasoning. For
    needs_change the target's worktree is created, cloning the repo first if discovery only used the API."""
    require(status in {'needs_change', 'compliant', 'not_applicable'}, 'status: needs_change|compliant|not_applicable')
    require(evidence.strip(), 'Say what you looked at and why')
    run = str(Path(run).resolve())
    s = run_state.load(run)
    recipe_dir, v = run_state.active_recipe(run, s)
    d, e = find_entry(run, tid)
    upd = {'status': status, 'evidence': f"decided: {evidence} (at {e['branch']}@{e['observed_sha'][:10]})",
           'decided_at': now(), 'recipe_version': v['version']}
    clone = Path(d['clone']) if d.get('clone') else None
    if status == 'needs_change':
        if not clone or not clone.exists():
            require(d.get('url'), f"{d['repo']}: no clone URL recorded")
            clone = data_home() / 'clones' / slug(d['repo'], 80)
            git_probe.prepare(d['url'], clone, [e['branch']], None, run, d['repo'])
        if subprocess.run(['git', '-C', str(clone), 'cat-file', '-e', e['observed_sha'] + '^{commit}'],
                          capture_output=True).returncode:
            raise ValueError(f"{e['branch']}@{e['observed_sha'][:10]} is no longer on the server; rediscover {d['repo']}")
        src = source_branch_for(s, s['branch_format'], d, d['repo'], e['branch'], tid)
        wt = clone.parent / '_wt' / s['run_id'] / tid
        ensure_worktree(clone, wt, src, e['observed_sha'],
                        {'run': run, 'target': tid, 'repo': d['repo'], 'jira_key': s['jira']['key'],
                         'source_branch': src, 'destination': e['branch'], 'run_dir': run})
        impl = 'agent'
        if recipe_meta(recipe_dir)['mode'] == 'scripted' and find_script(recipe_dir, 'apply'):
            out, err = run_apply(recipe_dir, wt, {'TARGET_REPO': d['repo'], 'TARGET_BRANCH': e['branch']})
            if not err and (out or {}).get('changed'):
                impl = 'script'  # the recipe's own script could make the change: no agent edit needed
        tmp = {**e, **upd, 'worktree': str(wt), 'source_branch': src, 'implementation': impl}
        capture_into(run, tmp, recipe_dir, wt)
        upd = {k: tmp[k] for k in tmp if k not in e or tmp[k] != e.get(k)}
    with edit_discovery(run, d['repo'], clone=clone) as fresh:
        cur = next(x for x in fresh['targets'] if x['id'] == tid)
        cur.update(upd)
    return {'target': tid, 'status': status}


def override(run, tid, status, evidence):
    """Correct the check's verdict for one target, only with the user's approval. Always reported."""
    require(evidence and evidence.strip(), "Record the user's words that approve this override (--evidence)")
    out = decide(run, tid, status, 'OVERRIDE approved by the user: ' + evidence)
    run_state.add_override(run, tid, status, evidence)
    return {**out, 'override': True, 'next': 'mr plan --run <run-id> ...'}


def capture(run, tid):
    run = str(Path(run).resolve())
    s = run_state.load(run)
    recipe_dir, _ = run_state.active_recipe(run, s)
    d, e = find_entry(run, tid)
    require(e.get('worktree'), f'{tid} has no worktree (status {e["status"]})')
    before = e.get('implementation')
    capture_into(run, e, recipe_dir, e['worktree'])
    e['implementation'] = 'hybrid' if before == 'script' else before or 'agent'
    with edit_discovery(run, d['repo']) as fresh:
        upsert(fresh, e)
    return e


def trial(recipe, clone, branch):
    """Check, apply, re-check in a throwaway checkout and return the real diff. Nothing is kept."""
    sha = git(clone, 'rev-parse', '--verify', f'origin/{branch}^{{commit}}').strip()
    out = {'recipe': str(Path(recipe).resolve()), 'clone': str(Path(clone).resolve()), 'branch': branch, 'sha': sha}
    meta = recipe_meta(recipe)
    if meta['check_paths']:
        files = [p for p, _ in matched_files(clone, sha, meta['check_paths'])]
        with sparse_tree(clone, sha, files) as tree:
            out['sparse_check'] = run_check(recipe, tree)
    with temp_tree(clone, sha) as tree:
        out['before'] = run_check(recipe, tree)
        if meta['check_paths'] and out['sparse_check']['status'] != out['before']['status']:
            out['warning'] = ('check.py gives a different answer with only the `Check paths` files present: it reads '
                              'other files. Add them to `Check paths`, or remove that line.')
        if out['before']['status'] == 'needs_change':
            res, err = run_apply(recipe, tree)
            out['apply'] = res if res else {'error': err}
            out['after'] = run_check(recipe, tree)
            out['diff'] = worktree_patch(tree, sha)
    return out


def repo_done(run, repo, outcome, reason, facts_file=None, url=None, clone=None):
    require(outcome in {'scanned', 'unreadable', 'excluded'}, 'outcome must be scanned|unreadable|excluded')
    require(reason.strip(), 'reason required')
    with edit_discovery(run, repo, url, clone) as d:
        d.update(outcome=outcome, reason=reason, complete=True, completed_at=now())
        if facts_file:
            d['facts'] = read_json(facts_file)
    return {'repo': repo, 'outcome': outcome, 'targets': len(d['targets'])}


def question(run, text, recommendation=None, repo=None, tid=None):
    return run_state.ask(run, text, recommendation, tid, repo)


CI_FILES = ('Jenkinsfile', 'bitbucket-pipelines.yml', '.gitlab-ci.yml')
BUILD_WORDS = re.compile(r'(\bmvnw?\b|\bgradlew?\b|build\.sh|\bmake\b|\bnpm\b|\byarn\b)')


def ci_command(read, listdir=None):
    """The build command the repo's CI runs, from its CI config (Jenkinsfile, Bitbucket Pipelines, GitLab CI,
    GitHub Actions). That's usually the most reliable build command there is."""
    files = list(CI_FILES) + (listdir('.github/workflows') if listdir else [])
    for f in files:
        text = read(f)
        if not text:
            continue
        if f == 'Jenkinsfile':
            cands = re.findall(r"""\bsh\s*\(?\s*(?:script:\s*)?['"]([^'"]+)['"]""", text)
        else:
            cands = [m.strip().strip('"\'') for m in re.findall(r'^\s*(?:-\s+|run:\s*)(.+)$', text, re.M)]
        for c in cands:
            if BUILD_WORDS.search(c) and not re.search(r'\b(deploy|publish|release|push)\b', c):
                return c.strip(), f
    return None, None


BUILD_HINTS = [('mvnw', './mvnw -q -B verify', None), ('gradlew', './gradlew -q build', None),
               ('pom.xml', 'mvn -q -B verify', 'mvn'), ('build.gradle.kts', 'gradle -q build', 'gradle'),
               ('build.gradle', 'gradle -q build', 'gradle'), ('build.sh', 'sh build.sh', None),
               ('package.json', 'npm test', 'npm'), ('Makefile', 'make test', 'make')]


def detect_build(clone, sha):
    names = set(git(clone, 'ls-tree', '--name-only', sha).split())

    def read(path):
        if path.split('/')[0] not in names:  # most repos have none of the CI files: don't ask git for them
            return None
        p = subprocess.run(['git', '-C', str(clone), 'show', f'{sha}:{path}'], capture_output=True, text=True,
                           env=limits.net_env())
        return p.stdout if p.returncode == 0 else None

    def listdir(d):
        if '.github' not in names:
            return []
        return [x for x in git(clone, 'ls-tree', '--name-only', f'{sha}:{d}', check=False).split()
                if x.endswith(('.yml', '.yaml'))] and \
            [f'{d}/{x}' for x in git(clone, 'ls-tree', '--name-only', f'{sha}:{d}', check=False).split()
             if x.endswith(('.yml', '.yaml'))]
    cmd, src = ci_command(read, listdir)
    if cmd:
        return cmd, f'from {src} at {sha[:10]}; confirm on first build'
    for f, cmd, tool in BUILD_HINTS:
        if f in names and (tool is None or shutil.which(tool)):
            return cmd, f'detected from {f} at {sha[:10]}; confirm on first build'
    return None, None


def record_check_only(run, repo, url, branch, sha, chk, version):
    """Discovery entry for a branch that needs no change (no clone or worktree involved)."""
    tid = target_id(repo, branch)
    with edit_discovery(run, repo, url) as d:
        old = next((e for e in d['targets'] if e['id'] == tid), {})
        upsert(d, {'id': tid, 'repo': repo, 'branch': branch, 'observed_sha': sha, 'status': chk['status'],
                   'evidence': f"{chk['evidence']} (at {branch}@{sha[:10]})", 'recipe_version': version,
                   'questions': old.get('questions', []), 'previewed_at': now(), 'check_mode': chk.get('mode'),
                   'check_cached': bool(chk.get('cached'))})


def api_check(run, repo, url, sha, meta, recipe_dir, recipe_sha, env, cache):
    """Tier 2 through the SCM API: read only the recipe's check paths, run the same check.py on them."""
    import scm
    adapter = scm.for_url(url)
    with tempfile.TemporaryDirectory(prefix='rollout-api-') as tmp:
        basis = [toolkit_version(), recipe_sha]
        for rel in meta['check_paths']:
            with limits.timed(run, 'api-read', repo=repo):
                content = adapter.read_file(scm.address(adapter, url, repo), sha, rel)
            if content is not None:
                f = Path(tmp) / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(content)
                basis.append(rel + ':' + hashlib.sha256(content.encode()).hexdigest())
        if meta['check_uses_target']:
            basis += [env['TARGET_REPO'], env['TARGET_BRANCH']]
        cf = Path(cache) / (hashlib.sha256(('api\n' + '\n'.join(basis)).encode()).hexdigest() + '.json')
        with cache_lock(cf):
            if cf.is_file():
                limits.record(run, 'check', 0, repo=repo, cached=True)
                return {**read_json(cf), 'cached': True}
            with limits.timed(run, 'check', repo=repo, mode='api'):
                out = run_check(recipe_dir, tmp, env)
            out['mode'] = 'api'
            if not out['evidence'].startswith('check failed'):
                write_json(cf, out)
            return out


def own_branch_names(runs_root, repo, s=None):
    """Branches this toolkit created: every source branch recorded by any run, plus this run's naming prefix."""
    names = set()
    for f in Path(runs_root).glob(f'*/discovery/{slug(repo, 80)}.json'):
        try:
            names |= {e.get('source_branch') for e in read_json(f).get('targets', [])}
        except ValueError:
            pass
    names.discard(None)
    prefixes = set()
    if s:
        fmt = s['branch_format'].split('{target}')[0].split('{branch}')[0]
        prefixes.add(fmt.format(key=s['jira']['key'], recipe=slug(s['recipe']['slug'], 30)))
    return prefixes, names


def select_branches(heads, patterns, bases, own):
    prefixes, names = own
    own_found = sorted(b for b in heads if b in names or any(p and b.startswith(p) for p in prefixes))
    typed = [b for b in sorted(heads) if b not in own_found and (branch_matches(b, patterns) or b in (bases or []))]
    excluded = [b for b in sorted(heads) if b not in own_found and b not in typed]
    return typed, excluded, own_found


def coverage(heads, typed, excluded_type, old, own_found, scanned, patterns):
    """What was and wasn't looked at, so nothing is left out silently."""
    cores = {re.sub(r'[^a-z]', '', p.lower()) for p in (patterns or [])} - {''}
    variants = [b for b in excluded_type if any(c in re.sub(r'[^a-z]', '', b.lower()) for c in cores)]
    return {'branches': len(heads), 'scanned': len(scanned), 'excluded_by_type': len(excluded_type),
            'type_sample': excluded_type[:10], 'possible_variants': variants[:10], 'excluded_by_age': len(old),
            'newest_excluded_age': max((d for _, d in old), default=None), 'own_rollout_branches': len(own_found)}


def coverage_text(c):
    return (f" Coverage: {c['scanned']} of {c['branches']} branches scanned; {c['excluded_by_type']} excluded by type"
            f"{' (possible variants: ' + ', '.join(c['possible_variants']) + ')' if c['possible_variants'] else ''}, "
            f"{c['excluded_by_age']} by age, {c['own_rollout_branches']} rollout branches.")


@contextlib.contextmanager
def tip_dates():
    """Commit dates by SHA. A commit's date never changes, so this cache never goes stale."""
    f = data_home() / 'cache' / 'tip-dates.json'
    f.parent.mkdir(parents=True, exist_ok=True)
    with cache_lock(f):
        d = read_json(f) if f.is_file() else {}
        yield d
        write_json(f, d)


def fetch_active(url, clone, heads, candidates, days, always, run=None, repo=None):
    """Fetch the candidate branches that may be active; return (active, [(branch, date) older than the cutoff]).
    A commit's date never changes, so branches already known (by tip SHA) to be too old are never fetched again.
    `always` (the default branch, explicit bases) is exempt from the cutoff."""
    if days is None:
        if candidates:
            git_probe.prepare(url, clone, candidates, heads, run, repo)
        return list(candidates), []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with tip_dates() as d:
        old = [(b, d[heads[b]]) for b in candidates if b not in always and d.get(heads[b])
               and datetime.fromisoformat(d[heads[b]]) < cutoff]
    known_old = {b for b, _ in old}
    todo = [b for b in candidates if b not in known_old]
    if todo:
        git_probe.prepare(url, clone, todo, heads, run, repo)
    local = dict(line.split(' ', 1) for line in git(
        clone, 'for-each-ref', '--format=%(refname:strip=3) %(committerdate:iso-strict)', 'refs/remotes/origin/').splitlines()
        if ' ' in line)
    keep = []
    with tip_dates() as d:
        for b in todo:
            when = d.get(heads[b]) or local.get(b) or git(clone, 'log', '-1', '--format=%cI', heads[b]).strip()
            d[heads[b]] = when
            if b in always or datetime.fromisoformat(when) >= cutoff:
                keep.append(b)
            else:
                old.append((b, when))
    return keep, old


def discover_repo(run, repo, url, workspaces, bases=None, patterns=None, days=None, no_apply=False, via='git'):
    """One repo, cheapest tier first:
      1. `git ls-remote`: branch tips + default branch, no clone. Empty or unreachable repos stop here.
      2. fetch only the chosen branches, blobless, skipping any whose local tip is already current
         (or, with --via api, read just the `Check paths` files through the SCM API: no clone at all);
      3. check each branch on a sparse checkout of the `Check paths` files (content-cached);
      4. full worktree, apply and diff only where the check says needs_change."""
    run = str(Path(run).resolve())
    s = run_state.load(run)
    recipe_dir, v = run_state.active_recipe(run, s)
    meta = recipe_meta(recipe_dir)
    facts = repo_facts.get(repo)
    try:
        rem = git_probe.ls_remote(url, run, repo)
    except ValueError as e:
        return repo_done(run, repo, 'unreadable', 'ls-remote failed: ' + str(e)[:300], url=url)
    heads = rem['heads']
    if not heads:
        return repo_done(run, repo, 'excluded', 'empty repository (no branches)', url=url)
    pats = patterns or meta['branch_patterns']
    cutoff = days if days is not None else meta['active_days']
    typed, excl_type, own_found = select_branches(heads, pats, bases, own_branch_names(Path(run).parent, repo, s))
    always = [b for b in [rem['default'], *(bases or [])] if b in typed]
    missing, base_note = [b for b in (bases or []) if b not in heads], ''
    if not typed:
        cov = coverage(heads, typed, excl_type, [], own_found, [], pats)
        r = repo_done(run, repo, 'scanned', "no branch matches the recipe's branch types." + coverage_text(cov), url=url)
        return {**r, 'coverage': cov, 'branches': []}
    note, old = '', []
    if via == 'api' and (cutoff is not None or len(typed) > 5 or not meta['check_paths']
                         or any(c in ''.join(meta['check_paths']) for c in '*?[')):
        note, via = ' API mode needs exact `Check paths`, no activity cutoff and at most 5 branches; used Git.', 'git'
    chosen = typed
    if via == 'api':
        cache = Path(workspaces) / '.cache' / 'checks'
        cache.mkdir(parents=True, exist_ok=True)
        needs = []
        for b in chosen:
            env = {'TARGET_REPO': repo, 'TARGET_BRANCH': b}
            chk = api_check(run, repo, url, heads[b], meta, recipe_dir, v['sha256'], env, cache)
            if chk['status'] == 'needs_change':
                needs.append(b)
            else:
                record_check_only(run, repo, url, b, heads[b], chk, v['version'])
        branches = chosen
        out = [{'branch': b, 'status': 'needs_change' if b in needs else 'checked via API'} for b in chosen]
        clone = Path(workspaces) / slug(repo, 80)
        if needs:
            try:
                git_probe.prepare(url, clone, needs, heads, run, repo)
            except ValueError as e:
                return repo_done(run, repo, 'unreadable', 'fetch failed: ' + str(e)[:300], url=url)
        preview_these = needs
    else:
        clone = Path(workspaces) / slug(repo, 80)
        try:
            branches, old = fetch_active(url, clone, heads, typed, cutoff, always, run, repo)
        except ValueError as e:
            return repo_done(run, repo, 'unreadable', 'fetch failed: ' + str(e)[:300], url=url)
        out, preview_these = [], branches
    new_facts = {}
    if preview_these and not facts.get('build'):
        cmd, why = detect_build(clone, heads.get(preview_these[0]) or git(clone, 'rev-parse', f'origin/{preview_these[0]}').strip())
        if cmd:
            new_facts.update(build=cmd, build_source=why)
    for b in preview_these:
        try:
            e = preview(run, repo, clone, b, no_apply, url, new_facts.get('build'), heads.get(b))
            out = [x for x in out if x['branch'] != b] + [{'branch': b, 'status': e['status'],
                                                           'implementation': e.get('implementation'),
                                                           'cached': e.get('check_cached')}]
        except ValueError as err:
            question(run, f'Preview failed on {b}: {err}', repo=repo)
            out.append({'branch': b, 'status': 'error', 'error': str(err)[:200]})
    current, retired = set(branches), []
    with edit_discovery(run, repo, url) as dd:
        keep = []
        for e in dd['targets']:
            if e['branch'] in current:
                keep.append(e)
                continue
            wtp = Path(e.get('worktree') or '/nonexistent')
            info = load_target(wtp) if wtp.is_dir() else None
            has_work = s['results'].get(e['id'], {}).get('pr_url') or (
                info and git(wtp, 'rev-list', f"{info['base_sha']}..HEAD").split())
            if has_work:
                e['note'] = 'branch no longer selected for this run, but it has commits or a PR: left as is'
                keep.append(e)
            else:
                retired.append(e['id'])
        dd['targets'] = keep
    cov = coverage(heads, typed, excl_type, old, own_found, branches, pats)
    reason = f"branches checked{' via API' if via == 'api' else ''}: " + ', '.join(branches) + '.' + coverage_text(cov) + note
    if retired:
        reason += f' Retired (branch no longer selected): {", ".join(retired)}.'
    if missing:
        reason += ' Missing base(s): ' + ', '.join(missing) + '.'
    facts_file = None
    if new_facts:
        facts_file = Path(run) / 'discovery' / ('.' + slug(repo, 80) + '.facts')
        write_json(facts_file, new_facts)
    r = repo_done(run, repo, 'scanned', reason, facts_file, url, clone if clone.exists() else None)
    if facts_file:
        facts_file.unlink()
    return {**r, 'reason': reason, 'coverage': cov, 'branches': sorted(out, key=lambda x: x['branch'])}


def discover(run, repos=None, pending=False, workers=4, workspaces=None, bases=None, patterns=None,
             days=None, no_apply=False, limit=None, via='git'):
    """Discover several repos concurrently. The per-host `git` limit applies across all processes, so
    `--workers` above it only queues. `--limit N` takes the next N pending repos (streaming)."""
    import concurrent.futures
    workspaces = workspaces or str(data_home() / 'clones')
    s = run_state.load(run)
    scope = {r['name']: r.get('url') for r in s['scope']['repos']}
    if pending:
        names = [r['name'] for r in run_state.pending_repos(run, s)]
    else:
        names = list(repos or [])
    names = names[:limit] if limit else names
    unknown = [n for n in names if n not in scope]
    require(not unknown, 'Not in run scope: ' + ', '.join(unknown))
    require(all(scope[n] for n in names), 'Every repo needs a clone URL in the run scope file')
    global FORCE_FULL
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(discover_repo, run, n, scope[n], workspaces, bases, patterns, days, no_apply, via): n
                for n in names}
        for f in concurrent.futures.as_completed(futs):
            try:
                results.append(f.result())
            except Exception as e:  # one repo never stops the others
                results.append({'repo': futs[f], 'error': str(e)[:300]})
    recipe_dir, v = run_state.active_recipe(run, s)
    items = []
    for f in (Path(run) / 'discovery').glob('*.json'):
        d = read_json(f)
        items += [{'repo': d['repo'], 'branch': e['branch'], 'sha': e['observed_sha'], 'status': e['status'],
                   'mode': e.get('check_mode'), 'clone': Path(d['clone'])} for e in d['targets']
                  if d.get('clone') and d['repo'] in names and not e.get('check_cached')]
    audit = run_audit(items, recipe_dir, v['sha256'], run) if not FORCE_FULL else {'sampled': 0, 'mismatches': [], 'full': True}
    if audit['mismatches']:
        FORCE_FULL = True
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            results = list(pool.map(lambda n: discover_repo(run, n, scope[n], workspaces, bases, patterns, days,
                                                            no_apply, via), names))
        audit['action'] = 'sparse checks disagreed with full checkouts: every branch was re-checked on a full checkout'
    for r in results:
        r['audit'] = audit
    return sorted(results, key=lambda r: r['repo'])


def scan_repo(scan_dir, repo, url, recipe_dir, recipe_sha, clones, bases=None, patterns=None, days=None, via='git'):
    """Discovery's tiers 1-3 for one repo, read-only: no run, no worktrees, no ledger."""
    meta, facts = recipe_meta(recipe_dir), repo_facts.get(repo)
    try:
        rem = git_probe.ls_remote(url, scan_dir, repo)
    except ValueError as e:
        return {'repo': repo, 'outcome': 'unreadable', 'reason': str(e)[:300], 'branches': []}
    heads = rem['heads']
    if not heads:
        return {'repo': repo, 'outcome': 'excluded', 'reason': 'empty repository (no branches)', 'branches': []}
    pats = patterns or meta['branch_patterns']
    cutoff = days if days is not None else meta['active_days']
    typed, excl_type, own_found = select_branches(heads, pats, bases, own_branch_names(data_home() / 'runs', repo))
    always = [b for b in [rem['default'], *(bases or [])] if b in typed]
    if not typed:
        cov = coverage(heads, typed, excl_type, [], own_found, [], pats)
        return {'repo': repo, 'outcome': 'scanned', 'coverage': cov, 'branches': [],
                'reason': "no branch matches the branch types." + coverage_text(cov)}
    chosen, old = typed, []
    api = (via == 'api' and cutoff is None and len(typed) <= 5 and meta['check_paths']
           and not any(c in ''.join(meta['check_paths']) for c in '*?['))
    rows = []
    if api:
        cache = Path(clones) / '.cache' / 'checks'
        cache.mkdir(parents=True, exist_ok=True)
        branches = chosen
        results = [(b, api_check(scan_dir, repo, url, heads[b], meta, recipe_dir, recipe_sha,
                                 {'TARGET_REPO': repo, 'TARGET_BRANCH': b}, cache)) for b in chosen]
    else:
        clone = Path(clones) / slug(repo, 80)
        try:
            branches, old = fetch_active(url, clone, heads, typed, cutoff, always, scan_dir, repo)
        except ValueError as e:
            return {'repo': repo, 'outcome': 'unreadable', 'reason': 'fetch failed: ' + str(e)[:300], 'branches': []}
        results = [(b, check_commit(recipe_dir, recipe_sha, clone, heads[b], {'TARGET_REPO': repo, 'TARGET_BRANCH': b},
                                    scan_dir, repo)) for b in branches]
    for b, chk in results:
        rows.append({'branch': b, 'sha': heads[b], 'status': chk['status'], 'value': chk.get('value'),
                     'evidence': chk['evidence'], 'cached': bool(chk.get('cached')), 'mode': chk.get('mode')})
    cov = coverage(heads, typed, excl_type, old, own_found, branches, pats)
    return {'repo': repo, 'outcome': 'scanned', 'branches': rows, 'coverage': cov,
            'reason': f"branches checked{' via API' if api else ''}: " + ', '.join(branches) + '.' + coverage_text(cov)}


def scan(check, repos_file=None, projects=(), host=None, paths=None, bases=None, patterns=None, days=None,
         via='git', workers=8, name=None):
    """Answer one question across many repos, read-only. `check` is a recipe folder (or a recipe name) or a
    single check script; `paths` names the files a bare script reads. Scope: a repos file, or projects
    listed through the SCM adapter. Writes report.md, results.csv and results.json."""
    import concurrent.futures
    import csv
    home = data_home()
    sid = name or datetime.now().strftime('%Y%m%d-%H%M%S')
    d = home / 'scans' / sid
    d.mkdir(parents=True, exist_ok=False)
    src, rd = Path(check).expanduser(), d / 'recipe'
    if not src.exists() and (recipes_dir() / check).is_dir():
        src = recipes_dir() / check
    if not src.exists() and (TOOLKIT / 'examples' / check).is_dir():
        src = TOOLKIT / 'examples' / check
    if src.is_dir():
        require((src / 'recipe.md').is_file() and find_script(src, 'check'), f'{src} needs recipe.md and a check script')
        shutil.copytree(src, rd, ignore=shutil.ignore_patterns('__pycache__'))
    else:
        require(src.is_file(), f'No recipe or check script at {check}')
        rd.mkdir()
        shutil.copy(src, rd / ('check' + src.suffix))
        (rd / 'recipe.md').write_text(f'# Scan: {src.stem}\n\nStatus: scan\n'
                                      + (f"Check paths: {', '.join(paths)}\n" if paths else ''))
    if repos_file:
        scope = run_state.read_scope(repos_file)
    else:
        import scm
        require(projects, 'Give --repos FILE or --project KEY (with the SCM adapter configured)')
        hosts = list((scm.load_config(optional=True).get('hosts') or {}))
        host = host or (hosts[0] if len(hosts) == 1 else None)
        require(host, '--project needs --host (e.g. --host bitbucket.example.com)')
        scope = []
        for pr in projects:
            f = d / f'repos-{slug(pr)}.txt'
            scm.list_repos(host, pr, out_file=f)
            scope += run_state.read_scope(f) if f.read_text().strip() else []
        require(scope, 'No repos found in ' + ', '.join(projects))
    (d / 'repos.txt').write_text(''.join(f"{r['name']} {r['url']}\n" for r in scope))
    recipe_sha, clones = dir_hash(rd), home / 'clones'
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(scan_repo, str(d), r['name'], r['url'], rd, recipe_sha, clones, bases, patterns, days, via): r['name']
                for r in scope}
        for f in concurrent.futures.as_completed(futs):
            try:
                results.append(f.result())
            except Exception as e:  # one repo never stops the others
                results.append({'repo': futs[f], 'outcome': 'error', 'reason': str(e)[:300], 'branches': []})
    results.sort(key=lambda r: r['repo'])
    global FORCE_FULL
    items = [{'repo': r['repo'], 'branch': b['branch'], 'sha': b['sha'], 'status': b['status'], 'mode': b.get('mode'),
              'clone': clones / slug(r['repo'], 80)} for r in results for b in r['branches']]
    audit = run_audit(items, rd, recipe_sha, str(d)) if not FORCE_FULL else {'sampled': 0, 'mismatches': [], 'full': True}
    if audit['mismatches']:
        FORCE_FULL = True  # the check reads more than `Check paths`: sparse verdicts can't be trusted
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            results = sorted(pool.map(lambda r: scan_repo(str(d), r['name'], r['url'], rd, recipe_sha, clones, bases,
                                                          patterns, days, 'git'), scope), key=lambda r: r['repo'])
        audit['action'] = 'sparse checks disagreed with full checkouts: every branch was re-checked on a full checkout'
    write_json(d / 'results.json', results)
    rows = [{'repo': r['repo'], **b} for r in results for b in r['branches']]
    with open(d / 'results.csv', 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=['repo', 'branch', 'sha', 'status', 'value', 'evidence'], extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    by_status, by_value = {}, {}
    for x in rows:
        by_status[x['status']] = by_status.get(x['status'], 0) + 1
        if x.get('value') is not None:
            by_value.setdefault(x['value'], []).append(f"{x['repo']} {x['branch']}")
    other = [r for r in results if r['outcome'] != 'scanned']
    cov = coverage_totals(results, recipe_meta(rd), patterns, days)
    title = recipe_title(rd)
    L = [f'# Scan {sid}: {title}', '', f"{len(results)} repos · {len(rows)} branches · "
         + ', '.join(f'{v} {k}' for k, v in sorted(by_status.items())), '']
    if by_value:
        L += ['## By value', '', '| Value | Count | Where |', '|---|---|---|']
        L += [f"| {md_cell(v)} | {len(w)} | {md_cell(', '.join(w[:8]) + (' …' if len(w) > 8 else ''))} |"
              for v, w in sorted(by_value.items(), key=lambda kv: -len(kv[1]))] + ['']
    L += ['## Results', '', '| Repo | Branch | Status | Value | Evidence |', '|---|---|---|---|---|']
    L += [f"| {x['repo']} | {x['branch']} | {x['status']} | {md_cell(x.get('value'))} | {md_cell(x['evidence'])} |" for x in rows]
    if other:
        L += ['', '## Not scanned', ''] + [f"- {r['repo']}: {r['outcome']} — {r['reason']}" for r in other]
    L += ['', '## Coverage', '',
          f"Branch types: {cov['selection']['branch_types']}; active within: {cov['selection']['active_within_days'] or 'any time'} "
          f"days ({cov['selection']['source']}).",
          f"{cov['scanned']} of {cov['branches']} branches scanned; {cov['excluded_by_type']} excluded by type, "
          f"{cov['excluded_by_age']} by age (newest excluded: {cov['newest_excluded_by_age'] or '—'}), "
          f"{cov['own_rollout_branches']} rollout branches."]
    if cov['possible_variants']:
        L += ['', '## Possible branch variants not matched by the branch types', '',
              'Widen the recipe\'s `Branches:` line if these should be included.', '']
        L += [f'- {v}' for v in cov['possible_variants']]
    perf = limits.summary(d)
    if perf.get('bottleneck'):
        L += ['', f"Bottleneck: {perf['bottleneck']}"]
    (d / 'report.md').write_text('\n'.join(L) + '\n')
    return {'scan': sid, 'report': str(d / 'report.md'), 'csv': str(d / 'results.csv'), 'repos': len(results),
            'branches': len(rows), 'by_status': by_status,
            'by_value': {v: len(w) for v, w in sorted(by_value.items(), key=lambda kv: -len(kv[1]))[:20]},
            'not_scanned': [{'repo': r['repo'], 'outcome': r['outcome'], 'reason': r['reason']} for r in other],
            'coverage': cov, 'audit': audit,
            'cached_checks': sum(bool(x.get('cached')) for x in rows)}


def md_cell(v):
    return str(v if v not in (None, '') else '—').replace('|', '\\|').replace('\n', ' ')[:300]


def recipe_title(recipe_dir):
    for line in (Path(recipe_dir) / 'recipe.md').read_text().splitlines():
        if line.startswith('# '):
            return line[2:].strip()
    return Path(recipe_dir).name


def build(cmd, directory=None, clone=None, ref=None, timeout=1800, log=None, run=None, repo=None, stage='build'):
    """Run the repo's build/test command in the target worktree, or (baseline) on an unchanged commit.
    A failure that also happens on the unchanged base is pre-existing, not caused by the change.
    Holds a `build` slot: the total across all processes stays at the configured limit."""
    require(cmd.strip(), 'Build command required (see repo-facts)')
    require(bool(directory) != bool(clone and ref), 'Use --dir, or --clone with --ref')

    def go(where):
        started = datetime.now(timezone.utc)
        with limits.timed(run, stage, 'build', repo=repo):
            try:
                p = subprocess.run(cmd, shell=True, cwd=where, capture_output=True, text=True, timeout=timeout)
                code, text = p.returncode, p.stdout + p.stderr
            except subprocess.TimeoutExpired as e:
                code, text = 124, f'timed out after {timeout}s\n' + str(e.output or '')
        if log:
            Path(log).parent.mkdir(parents=True, exist_ok=True)
            Path(log).write_text(text)
        return {'cmd': cmd, 'exit': code, 'passed': code == 0,
                'seconds': round((datetime.now(timezone.utc) - started).total_seconds(), 1),
                'tail': text[-1500:], 'log': str(Path(log).resolve()) if log else None}
    if directory:
        out = go(directory)
        out.update(where='change', sha=git(directory, 'rev-parse', 'HEAD').strip())
        return out
    sha = git(clone, 'rev-parse', '--verify', ref + '^{commit}').strip()
    with temp_tree(clone, sha, run, repo) as tree:
        out = go(tree)
    out.update(where='baseline', sha=sha)
    return out


def build_command(level, repo, run):
    """The recipe's `Validation:` level picks the local command. CI runs the full suite on every PR, so a
    config-only recipe doesn't need to repeat it locally."""
    if level == 'check':
        return None
    f = {**discovered_facts(run, repo), **repo_facts.get(repo)}
    full = f.get('build')
    if level == 'compile':
        if f.get('compile'):
            return f['compile']
        for prefix, cmd in (('./mvnw', './mvnw -q -B compile'), ('mvn ', 'mvn -q -B compile'),
                            ('./gradlew', './gradlew -q classes'), ('gradle ', 'gradle -q classes')):
            if full and full.startswith(prefix):
                return cmd
    return full


def write_result(run, tid, status, evidence, validation=None, pr_url=None, pr_state=None, ci=None,
                 checks=(), questions=(), lessons=(), published=None, extra=None, reviewed=False):
    """Results go straight into the ledger (it locks); kept as a thin wrapper for the helpers."""
    return run_state.record(run, tid, status, evidence, validation, pr_url, pr_state, ci, checks, questions,
                            lessons, published, reviewed, extra)


def discovered_facts(run, repo):
    """Facts discovery recorded but nobody merged into repo-facts yet."""
    p = discovery_path(run, repo)
    return (read_json(p).get('facts') or {}) if p.exists() else {}


def ensure_hooks(wt, checks=None):
    """Builds that install their own Git hooks (husky: `git config core.hooksPath .husky`) switch ours off.
    Put ours back in front; theirs still run after ours."""
    try:
        git_probe.hooks_check(wt)
        return True
    except ValueError:
        cd = Path(git(wt, 'rev-parse', '--git-common-dir').strip())
        clone = (cd if cd.is_absolute() else (Path(wt) / cd).resolve()).parent
        git_probe.install_hooks(clone)
        git_probe.hooks_check(wt)
        if checks is not None:
            checks.append("the build switched core.hooksPath (husky-style): rollout hooks put back in front; "
                          "the repo's own hooks still run after them")
        return False


def env_problem(b):
    return b['exit'] in (126, 127) or bool(re.search(r'command not found|: not found|No such file or directory|'
                                                     r'Permission denied', b.get('tail') or ''))


def verify(run, s, t, wt, info, build_cmd=None, timeout=1800, allow_no_build=False):
    """Recipe check, then the build for the recipe's validation level, then (on failure) the same build on
    the unchanged base. Returns {'ok': True, 'checks', 'level'} or a stop description."""
    tid = t['id']
    recipe_dir, _ = run_state.active_recipe(run, s)
    level = recipe_meta(recipe_dir)['validation']
    with limits.timed(run, 'verify-check', repo=t['repo']):
        chk = run_check(recipe_dir, wt, {'TARGET_REPO': t['repo'], 'TARGET_BRANCH': t['branch']})
    if chk['status'] != 'compliant':
        return {'ok': False, 'status': 'failed', 'validation': 'failed', 'checks': [f"recipe check: {chk['status']}"],
                'reason': f"recipe check after the change is {chk['status']}: {chk['evidence']}"}
    checks = ['recipe check: compliant']
    cmd = build_cmd or build_command(level, t['repo'], run)
    if not cmd and level == 'check':
        return {'ok': True, 'level': level, 'cmd': None, 'checks': checks + [
            'no local build: recipe declares Validation: check (the PR\'s CI runs the full build)']}
    if not cmd and not allow_no_build:
        return {'ok': False, 'status': 'blocked', 'validation': None, 'checks': checks,
                'reason': 'no build command known for this repo',
                'question': f"No build command for {t['repo']}. Add `build` to repo-facts, or approve check-only validation."}
    if not cmd:
        return {'ok': True, 'level': level, 'cmd': None,
                'checks': checks + ['no build command: validated by the recipe check only (approved)']}
    logs = Path(run) / 'logs'
    b = build(cmd, directory=wt, timeout=timeout, log=logs / f'{tid}.build.log', run=run, repo=t['repo'])
    ensure_hooks(wt, checks)
    checks.append(f"{cmd}: exit {b['exit']} ({b['seconds']}s)")
    if b['passed']:
        return {'ok': True, 'level': level, 'cmd': cmd, 'checks': checks}
    clone = Path(git(wt, 'rev-parse', '--git-common-dir').strip())
    clone = clone if clone.is_absolute() else (wt / clone).resolve()
    b0 = build(cmd, clone=clone.parent, ref=info['base_sha'], timeout=timeout, log=logs / f'{tid}.baseline.log',
               run=run, repo=t['repo'], stage='baseline-build')
    ensure_hooks(wt, checks)
    checks.append(f"{cmd} on unchanged {t['branch']}@{info['base_sha'][:10]}: exit {b0['exit']}")
    if not b0['passed'] and not build_cmd:
        # Before calling the branch broken: does the repo's CI run a different command? Prove it on the unchanged
        # commit; if it passes, it becomes this repo's build command and the change is built with it.
        ci_cmd, src = ci_command(lambda f: (wt / f).read_text() if (wt / f).is_file() else None,
                                 lambda d: [str(x.relative_to(wt)) for x in sorted((wt / d).glob('*.y*ml'))]
                                 if (wt / d).is_dir() else [])
        if ci_cmd and ci_cmd != cmd:
            b1 = build(ci_cmd, clone=clone.parent, ref=info['base_sha'], timeout=timeout,
                       log=logs / f'{tid}.baseline-ci.log', run=run, repo=t['repo'], stage='baseline-build')
            ensure_hooks(wt, checks)
            checks.append(f"{ci_cmd} (from {src}) on unchanged {t['branch']}: exit {b1['exit']}")
            if b1['passed']:
                repo_facts.merge(t['repo'], {'build': ci_cmd, 'build_source': f"{src}; proven on "
                                             f"{t['branch']}@{info['base_sha'][:10]}"}, s['run_id'])
                b2 = build(ci_cmd, directory=wt, timeout=timeout, log=logs / f'{tid}.build.log', run=run, repo=t['repo'])
                ensure_hooks(wt, checks)
                checks.append(f"{ci_cmd}: exit {b2['exit']} (now this repo's build command)")
                if b2['passed']:
                    return {'ok': True, 'level': level, 'cmd': ci_cmd, 'checks': checks}
                return {'ok': False, 'status': 'failed', 'validation': 'failed', 'checks': checks,
                        'reason': f"build fails with the change and passes without it; the change broke it "
                                  f"(log: {logs / (tid + '.build.log')})"}
        if env_problem(b0):
            last = (b0.get('tail') or '').strip().splitlines()[-1:] or ['']
            return {'ok': False, 'status': 'blocked', 'validation': 'blocked', 'checks': checks,
                    'reason': f'the build command `{cmd}` cannot run on this machine: {last[0][:200]}',
                    'question': {'text': f"The build command `{cmd}` can't run on this machine for {t['repo']} "
                                         f"({last[0][:160]}). It's the command, not the branch.",
                                 'recommendation': f"record the right one: mr facts set --repo {t['repo']} "
                                                   f"build='<command>', then answer retry",
                                 'choices': ['retry', 'skip']}}
    if not b0['passed']:
        exc = s.get('exceptions', {}).get(tid)
        if exc and exc.get('base') == info['base_sha'] and exc.get('cmd') == cmd:
            return {'ok': True, 'level': level, 'cmd': cmd, 'validation': 'exception',
                    'checks': checks + [f"baseline fails too: continuing as a draft by the user's decision ({exc['question']})"]}
        return {'ok': False, 'status': 'blocked', 'validation': 'blocked', 'checks': checks,
                'reason': 'build also fails without our change (pre-existing)',
                'question': {'text': f"The build already fails on {t['branch']} without our change "
                                     f"(log: {logs / (tid + '.baseline.log')}).",
                             'recommendation': 'skip, or retry once the branch is fixed; draft only if reviewers accept a red build',
                             'choices': ['draft', 'skip', 'retry'],
                             'exception': {'kind': 'baseline_failure', 'base': info['base_sha'], 'cmd': cmd,
                                           'log': str(logs / (tid + '.baseline.log'))}}}
    return {'ok': False, 'status': 'failed', 'validation': 'failed', 'checks': checks,
            'reason': f"build fails with the change and passes without it; the change broke it (log: {logs / (tid + '.build.log')})"}


def _stop(run, tid, v):
    write_result(run, tid, v['status'], v['reason'], validation=v.get('validation'), checks=v.get('checks', ()),
                 questions=[v['question']] if v.get('question') else [])
    return {'target': tid, 'status': v['status'], 'reason': v['reason']}


def _target_ctx(run, tid):
    run = str(Path(run).resolve())
    s = run_state.load(run)
    t = run_state.target_for(s, tid)
    wt = Path(t.get('worktree') or '/nonexistent')
    return run, s, t, wt, (load_target(wt) if wt.is_dir() else None)


def deliver_one(run, tid, message=None, build_cmd=None, timeout=1800, allow_no_build=False, dry=False,
                worker='deliver'):
    """Verify, commit and push one approved target; stops before the PR. With dry=True: only the check and
    build, nothing committed (validate-ahead, and the validation step of agent recipes). Every outcome is
    recorded in the ledger."""
    run, s, t, wt, info = _target_ctx(run, tid)
    if dry:
        require(t['disposition'] == 'needs_change' and tid not in s['aborted'], f'{tid} is not a target to change')
        if not info:
            return _stop(run, tid, {'status': 'blocked', 'reason': 'target worktree is missing'})
        v = verify(run, s, t, wt, info, build_cmd, timeout, allow_no_build)
        if not v['ok'] and v['status'] == 'failed':
            # An attempt, not a verdict: the worker keeps its claim, fixes the change and validates again.
            n = s['results'].get(tid, {}).get('validation_attempts', 0) + 1
            run_state.record(run, tid, 'observed', v['reason'], validation='failed', checks=v.get('checks', ()),
                             extra={'validation_attempts': n}, keep_claim=True)
            return {'target': tid, 'status': 'validation_failed', 'attempt': n, 'reason': v['reason']}
        if not v['ok']:
            return _stop(run, tid, v)
        write_result(run, tid, 'validated', 'check and build done (dry run: nothing committed)',
                     validation=v.get('validation', 'passed'), checks=v['checks'],
                     extra={'validated_level': v['level'], 'validated_cmd': v['cmd'], 'validation_attempts': 0})
        return {'target': tid, 'status': 'validated'}
    if not info:
        return _stop(run, tid, {'status': 'blocked', 'reason': 'target worktree is missing',
                                'question': 'Worktree missing; rerun discovery for this repo?'})
    try:
        ensure_hooks(wt)
    except ValueError as e:
        return _stop(run, tid, {'status': 'blocked', 'reason': str(e),
                                'question': 'Rollout hooks could not be put back in this worktree.'})
    res = run_state.reserve(run, tid, worker)  # the CI window counts this delivery from now on
    if not res['ok']:
        return {'target': tid, 'status': 'not_ready', 'reason': res['reason']}
    try:
        out = _deliver_reserved(run, s, t, tid, wt, info, message, build_cmd, timeout, allow_no_build)
    except Exception:
        run_state.release(run, tid, worker)
        raise
    if out['status'] == 'not_ready':
        run_state.release(run, tid, worker)
    return out


def _deliver_reserved(run, s, t, tid, wt, info, message, build_cmd, timeout, allow_no_build):
    url = git_probe.origin_url(Path(git_probe.gitdir_of(wt)).parent.parent.parent
                               if Path(git_probe.gitdir_of(wt)).parent.name == 'worktrees' else wt)
    git_probe.net_git(wt, 'fetch', '--quiet', '--no-tags', 'origin',
                      f"+refs/heads/{t['branch']}:refs/remotes/origin/{t['branch']}", url=url, run=run,
                      stage='fetch', repo=t['repo'])
    current = git(wt, 'rev-parse', f"origin/{t['branch']}").strip()
    patch = patch_hash(worktree_patch(wt, info['base_sha']))
    try:
        run_state.check_gate(run, tid, 'push', current, patch)
    except ValueError as e:
        # "Not ready yet" (unapproved, pilot pending, not validated/reviewed, dependency, open question) is not a
        # stop: nothing is recorded, the target stays in the queue. Real problems (the destination moved, the
        # diff differs) are recorded as blocked so the report shows them.
        if 'moved' in str(e) or 'differs' in str(e):
            return _stop(run, tid, {'status': 'blocked', 'reason': 'gate: ' + str(e)})
        return {'target': tid, 'status': 'not_ready', 'reason': str(e)}
    recipe_dir, _ = run_state.active_recipe(run, s)
    level = recipe_meta(recipe_dir)['validation']
    prev = s['results'].get(tid, {})
    # Reuse a validation only if everything it depended on is unchanged: the diff, the base it sits on, the
    # recipe, the validation level and the exact build command.
    if (not build_cmd and prev.get('validation') == 'passed' and prev.get('validated_patch_sha256') == patch
            and prev.get('validated_level') == level and prev.get('validated_base') == info['base_sha']
            and prev.get('validated_recipe') == s['recipe']['versions'][-1]['sha256']
            and prev.get('validated_cmd') == build_command(level, t['repo'], run)):
        with limits.timed(run, 'verify-check', repo=t['repo']):
            chk = run_check(recipe_dir, wt, {'TARGET_REPO': t['repo'], 'TARGET_BRANCH': t['branch']})
        if chk['status'] != 'compliant':
            return _stop(run, tid, {'status': 'failed', 'validation': 'failed', 'reason': 'recipe check: ' + chk['status']})
        checks = ['recipe check: compliant', 'build: reused from validate-ahead on the identical change']
    else:
        v = verify(run, s, t, wt, info, build_cmd, timeout, allow_no_build)
        if not v['ok']:
            return _stop(run, tid, v)
        checks = v['checks']
        prev = {'validation': v.get('validation', 'passed'), 'validated_cmd': v['cmd']}
    msg = message or f"{t['jira_key']} {s['recipe']['title']}"
    with limits.timed(run, 'commit', repo=t['repo']):
        if git(wt, 'status', '--porcelain').strip():
            git(wt, 'add', '-A')
            c = subprocess.run(['git', '-C', str(wt), 'commit', '-q', '-m', msg], capture_output=True, text=True)
            if c.returncode:
                return _stop(run, tid, {'status': 'blocked', 'checks': checks,
                                        'reason': 'commit refused: ' + (c.stderr.strip() or c.stdout.strip())[-500:]})
        elif not git(wt, 'rev-list', f"{info['base_sha']}..HEAD").split():
            return _stop(run, tid, {'status': 'blocked', 'reason': 'worktree has no change to deliver',
                                    'question': 'No change in the worktree. Re-preview?'})
    try:
        git_probe.net_git(wt, 'push', '-q', '-u', 'origin', info['source_branch'], url=url, run=run, stage='push',
                          repo=t['repo'])
    except ValueError as e:
        msg = str(e)
        remote = [ln.split('remote:', 1)[1].strip() for ln in msg.splitlines() if ln.startswith('remote:') and ln.split('remote:', 1)[1].strip()]
        if '[rollout hook]' not in msg and (remote or re.search(r'pre-receive|declined|not allowed', msg)):
            why = (remote or [msg.strip().splitlines()[0]])[0][:200]
            return _stop(run, tid, {'status': 'blocked', 'checks': checks,
                                    'reason': f"the server refused branch `{info['source_branch']}`: {why}. For the whole "
                                              f"run: mr branches --run {s['run_id']} --format '<allowed name, e.g. "
                                              f"feature/{{key}}-{{recipe}}>', then mr plan, then mr retry"})
        return _stop(run, tid, {'status': 'blocked', 'checks': checks, 'reason': 'push refused: ' + msg[-500:]})
    run_state.record(run, tid, 'pushed', f"check compliant, build passed, pushed {info['source_branch']}",
                     validation=prev.get('validation', 'passed'), checks=checks, patch_sha=patch,
                     extra={'validated_level': level, 'validated_cmd': prev.get('validated_cmd')})
    return {'target': tid, 'status': 'pushed'}


def deliver(run, tids, workers=3, message=None, build_cmd=None, timeout=1800, allow_no_build=False, dry=False,
            worker='deliver'):
    import concurrent.futures
    out = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(deliver_one, run, t, message, build_cmd, timeout, allow_no_build, dry, worker): t
                for t in tids}
        for f in concurrent.futures.as_completed(futs):
            try:
                out.append(f.result())
            except Exception as e:
                out.append({'target': futs[f], 'status': 'error', 'reason': str(e)[:400]})
    return sorted(out, key=lambda r: r['target'])


def rename_branches(run, fmt):
    """Change the run's branch format and rename every source branch that isn't on the server yet.
    Pushed branches (and PRs) keep their names."""
    run = str(Path(run).resolve())
    s = run_state.load(run)
    branch_name(fmt, s['jira']['key'], s['recipe']['slug'], 'x-1', 'b')  # validates the format
    renamed, kept = [], []
    for f in sorted((Path(run) / 'discovery').glob('*.json')):
        d = read_json(f)
        for e in d['targets']:
            wt = Path(e.get('worktree') or '/nonexistent')
            if not e.get('source_branch') or not wt.is_dir():
                continue
            old = e['source_branch']
            new = source_branch_for(s, fmt, {'targets': [x for x in d['targets'] if x['id'] != e['id']]},
                                    d['repo'], e['branch'], e['id'])
            if new == old:
                continue
            on_server = git(wt, 'ls-remote', 'origin', 'refs/heads/' + old, check=False).strip()
            if s['results'].get(e['id'], {}).get('pr_url') or on_server:
                kept.append({'target': e['id'], 'branch': old, 'why': 'already on the server'})
                continue
            git(wt, 'branch', '-m', old, new)
            info = load_target(wt)
            info['source_branch'] = new
            write_json(target_file(wt), info)
            with edit_discovery(run, d['repo']) as dd:
                next(x for x in dd['targets'] if x['id'] == e['id'])['source_branch'] = new
            d['targets'] = [({**x, 'source_branch': new} if x['id'] == e['id'] else x) for x in d['targets']]
            renamed.append({'target': e['id'], 'from': old, 'to': new})
    run_state.set_branch_format(run, fmt)
    return {'renamed': len(renamed), 'kept': kept, 'examples': renamed[:5],
            'next': f"mr plan --run {s['run_id']} ...; then mr retry the targets the server refused"}


def coverage_totals(results, meta=None, patterns=None, days=None):
    tot = {'branches': 0, 'scanned': 0, 'excluded_by_type': 0, 'excluded_by_age': 0, 'own_rollout_branches': 0}
    variants, sample, newest = [], [], None
    for r in results:
        c = r.get('coverage') or {}
        for k in tot:
            tot[k] += c.get(k, 0)
        variants += [f"{r['repo']}: {b}" for b in c.get('possible_variants', [])]
        sample += [f"{r['repo']}: {b}" for b in c.get('type_sample', [])]
        if c.get('newest_excluded_age') and (not newest or c['newest_excluded_age'] > newest):
            newest = c['newest_excluded_age']
    pats = patterns or (meta or {}).get('branch_patterns')
    cutoff = days if days is not None else (meta or {}).get('active_days')
    return {'selection': {'branch_types': pats or 'all', 'active_within_days': cutoff,
                          'source': 'command' if (patterns or days is not None) else 'recipe'},
            **tot, 'newest_excluded_by_age': newest, 'possible_variants': variants[:30],
            'excluded_by_type_sample': sample[:15]}


def audit_sample(items, seed):
    """A deterministic sample (at least 5, about 5%) of sparse verdicts that would leave a branch unchanged."""
    import random
    risky = [x for x in items if x['mode'] == 'sparse' and x['status'] in ('compliant', 'not_applicable')]
    k = min(len(risky), max(5, len(risky) // 20))
    return random.Random(seed).sample(risky, k) if risky else []


def run_audit(items, recipe_dir, recipe_sha, run):
    """Re-check sampled verdicts on full checkouts. A check that reads files outside `Check paths` would call a
    branch compliant just because it can't see the file that needs changing: this catches that."""
    mismatches = []
    for x in audit_sample(items, recipe_sha):
        full = check_commit(recipe_dir, recipe_sha, x['clone'], x['sha'], {'TARGET_REPO': x['repo'], 'TARGET_BRANCH': x['branch']},
                            run, x['repo'], full=True)
        if full['status'] != x['status']:
            mismatches.append({'repo': x['repo'], 'branch': x['branch'], 'sparse': x['status'], 'full': full['status']})
    return {'sampled': len(audit_sample(items, recipe_sha)), 'mismatches': mismatches}


def discover_summary(run, results):
    """What the coordinator needs to read; the full per-branch detail goes to a file."""
    outcomes, statuses, problems = {}, {}, []
    for r in results:
        outcomes[r.get('outcome', 'error')] = outcomes.get(r.get('outcome', 'error'), 0) + 1
        for b in r.get('branches', []):
            statuses[b['status']] = statuses.get(b['status'], 0) + 1
        if r.get('error') or r.get('outcome') in ('unreadable', 'error'):
            problems.append({'repo': r['repo'], 'problem': r.get('error') or r.get('reason')})
    f = Path(run) / 'logs' / f"discover-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.json"
    write_json(f, results)
    rs = run_state.load(run)
    cov = coverage_totals(results, recipe_meta(run_state.active_recipe(run, rs)[0]))
    audit = next((r['audit'] for r in results if r.get('audit')), None)
    return {'repos': len(results), 'outcomes': outcomes, 'branches': statuses, 'coverage': cov, 'audit': audit,
            'problems': problems[:20],
            'more_problems': max(0, len(problems) - 20), 'details': str(f), 'next': 'mr plan --run <run-id> ...'}


def diagnose(run):
    """Read-only health check of a run: what's stuck, stranded or inconsistent, with the fix for each."""
    import re
    run = str(Path(run).resolve())
    s = run_state.load(run)
    out = []

    def add(tid, problem, fix):
        out.append({'target': tid, 'problem': problem, 'fix': fix})
    for t in run_state.targets(s):
        tid, r = t['id'], s['results'].get(t['id'], {})
        if t['disposition'] != 'needs_change' or tid in s['aborted']:
            continue
        wt = Path(t.get('worktree') or '/nonexistent')
        info = load_target(wt) if wt.is_dir() else None
        if t.get('worktree') and not info:
            add(tid, 'worktree missing', f"mr discover --run {s['run_id']} --repo {t['repo']}")
        if info and (info.get('replay') or {}).get('status') == 'conflict':
            add(tid, 'edit conflicts with the moved destination; checkpoint kept', info['replay']['checkpoint'])
        if r.get('pr_url') and not re.search(r'/pull(?:s|-requests)?/\d+', str(r['pr_url'])):
            add(tid, f"PR link is not a PR: {r['pr_url']}", 'record the correct --pr-url')
        if r.get('status') == 'pushed' and not r.get('pr_url'):
            add(tid, 'pushed, but no PR recorded', 'mr open-prs, or search for the PR with your SCM tool and record it')
        c = s['claims'].get(tid)
        if c and not run_state.claim_active(s, tid):
            add(tid, f"expired claim by {c['worker']}", 'nothing: it goes back in the queue by itself')
        if info and r.get('status') in run_state.DONE and git(wt, 'status', '--porcelain').strip():
            add(tid, 'uncommitted changes after the PR was recorded', f'inspect {wt}')
        if r.get('validation') == 'failed' and not c:
            add(tid, f"validation failed ({r.get('validation_attempts', 1)} attempt(s)) and nobody owns it",
                'a worker takes it again with mr take, or ask the user')
    openq = [q for q in s['questions'] if q['status'] == 'open']
    return {'ok': not out and not openq, 'problems': out, 'open_questions': len(openq),
            'exceptions': s.get('exceptions', {})}


def progress(run, workers=8):
    """Recheck every original target branch on the server now, and compare with the run's baseline.
    PR state and actual remediation are separate: a merged PR is not proof."""
    import concurrent.futures
    run = str(Path(run).resolve())
    s = run_state.load(run)
    recipe_dir, v = run_state.active_recipe(run, s)
    base = {t['id']: t for t in run_state.baseline(run, s).get('targets', [])}
    cur = {t['id']: t for t in run_state.targets(s)}
    tracked = {**base, **cur}
    urls = {r['name']: r.get('url') for r in s['scope']['repos']}
    branches = {}
    for t in tracked.values():
        branches.setdefault(t['repo'], set()).add(t['branch'])
    d = Path(run) / 'progress' / datetime.now().strftime('%Y%m%d-%H%M%S')
    d.mkdir(parents=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(scan_repo, str(d), repo, urls[repo], recipe_dir, v['sha256'], data_home() / 'clones',
                            sorted(bs)): repo for repo, bs in branches.items()}
        scanned = {futs[f]: f.result() for f in concurrent.futures.as_completed(futs)}
    rows, counts = [], {}
    for tid, t in sorted(tracked.items()):
        res = scanned.get(t['repo'], {})
        now_row = next((b for b in res.get('branches', []) if b['branch'] == t['branch']), None)
        was = base.get(tid, {}).get('disposition')
        if res.get('outcome') != 'scanned':
            state = 'inaccessible'
        elif not now_row:
            state = 'branch gone'
        elif now_row['status'] == 'compliant':
            state = 'remediated' if was == 'needs_change' else 'compliant'
        elif now_row['status'] == 'needs_change':
            state = 'regressed' if was == 'compliant' else 'still needs change'
        else:
            state = 'unresolved'
        if tid not in base:
            state += ' (added after the baseline)'
        r = s['results'].get(tid, {})
        rows.append({'target': tid, 'repo': t['repo'], 'branch': t['branch'], 'state': state,
                     'pr': r.get('pr_url'), 'pr_state': r.get('pr_state'), 'evidence': (now_row or {}).get('evidence')})
        counts[state] = counts.get(state, 0) + 1
    L = [f"# Progress: {s['run_id']} ({now()[:16]})", '', ', '.join(f'{v} {k}' for k, v in sorted(counts.items())), '',
         '| Repo | Branch | State | PR | PR state |', '|---|---|---|---|---|']
    L += [f"| {x['repo']} | {x['branch']} | {x['state']} | {md_cell(x['pr'])} | {md_cell(x['pr_state'])} |" for x in rows]
    (d / 'progress.md').write_text('\n'.join(L) + '\n')
    write_json(d / 'progress.json', rows)
    return {'counts': counts, 'report': str(d / 'progress.md')}


def clean(run=None, clones_days=None, everything=False):
    """Free disk space. With --run: remove worktrees of finished, merged, closed or aborted targets (or all
    with --all); checkpoints, logs and the ledger stay. With --clones-days N: remove clones with no worktrees
    that haven't been fetched in N days."""
    removed, kept = [], []
    if run:
        run = str(Path(run).resolve())
        s = run_state.load(run)
        for t in run_state.targets(s):
            wt = Path(t.get('worktree') or '/nonexistent')
            if not wt.is_dir():
                continue
            r = s['results'].get(t['id'], {})
            finished = (t['id'] in s['aborted'] or r.get('pr_state') in ('merged', 'closed')
                        or r.get('status') in ('merged', 'closed') or t['disposition'] != 'needs_change')
            if everything or finished:
                common_dir = Path(git(wt, 'rev-parse', '--git-common-dir').strip())
                clone = (common_dir if common_dir.is_absolute() else (wt / common_dir).resolve()).parent
                git(clone, 'worktree', 'remove', '--force', str(wt), check=False)
                git(clone, 'worktree', 'prune', check=False)
                removed.append(t['id'])
            else:
                kept.append(t['id'])
    if clones_days is not None:
        cutoff = time.time() - clones_days * 86400
        for c in sorted((data_home() / 'clones').iterdir()):
            if c.name.startswith(('.', '_')) or not (c / '.git').exists():
                continue
            if len(git(c, 'worktree', 'list', '--porcelain', check=False).split('worktree ')) > 2:
                kept.append(c.name)
                continue
            stamp = c / '.git' / 'FETCH_HEAD'
            if (stamp if stamp.exists() else c / '.git').stat().st_mtime < cutoff:
                shutil.rmtree(c, ignore_errors=True)
                removed.append(c.name)
    return {'removed': removed, 'kept': kept}


def pick_targets(run, tids, ready, everything, dry, limit):
    require(bool(tids) + bool(ready) + bool(everything) == 1, 'Give --target IDs, or --ready, or --dry --all')
    if tids:
        return tids
    s = run_state.load(run)
    if ready:
        return [i['target'] for i in run_state.take(run, None, ['deliver'], limit or 10 ** 6)]
    require(dry, '--all is for --dry (validate every target that needs it)')
    return [t['id'] for t in run_state.targets(s) if t['disposition'] == 'needs_change' and t['id'] not in s['aborted']
            and not (s['results'].get(t['id'], {}).get('validation') == 'passed'
                     and s['results'][t['id']].get('validated_patch_sha256') == t.get('preview_patch_sha256'))]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = ap.add_subparsers(dest='cmd', required=True)
    q = s.add_parser('check', help='Run the recipe check on a ref (temporary checkout) or a directory')
    q.add_argument('--recipe', required=True)
    q.add_argument('--repo')
    q.add_argument('--ref')
    q.add_argument('--dir')
    q = s.add_parser('trial', help='Try a recipe on one repo branch in a throwaway checkout; prints the diff')
    q.add_argument('--recipe', required=True)
    q.add_argument('--clone', required=True)
    q.add_argument('--branch', required=True)
    q = s.add_parser('scan', help='Read-only: answer one question across many repos (no run needed)')
    q.add_argument('--check', required=True, help='recipe folder or name, or a single check script')
    q.add_argument('--repos', help='file of "<name> <clone-url>" lines')
    q.add_argument('--project', action='append', default=[], help='list this project/org via the SCM adapter (repeatable)')
    q.add_argument('--host', help='SCM host when several are configured')
    q.add_argument('--paths', nargs='*', help='files a bare check script reads (its Check paths)')
    q.add_argument('--base', action='append')
    q.add_argument('--pattern', action='append')
    q.add_argument('--days', type=int)
    q.add_argument('--via', choices=['git', 'api'], default='git')
    q.add_argument('--workers', type=int, default=8)
    q.add_argument('--name', help='scan folder name (default: a timestamp)')
    q.add_argument('--full', action='store_true', help='check every branch on a full checkout (no sparse checks)')
    q = s.add_parser('diagnose', help='What is stuck, stranded or inconsistent in a run, with the fix for each')
    q.add_argument('--run', required=True)
    q = s.add_parser('progress', help='Recheck every original target on the server now vs. the run baseline')
    q.add_argument('--run', required=True)
    q.add_argument('--workers', type=int, default=8)
    q = s.add_parser('clean', help='Remove worktrees of finished targets (--run) and long-unused clones (--clones-days)')
    q.add_argument('--run')
    q.add_argument('--clones-days', type=int)
    q.add_argument('--all', action='store_true', help='with --run: every worktree of the run')
    q = s.add_parser('branches', help="Change a run's branch format; renames source branches not yet pushed")
    q.add_argument('--run', required=True)
    q.add_argument('--format', required=True, help="e.g. 'feature/{key}-{recipe}' ({key} {recipe} {target} {branch})")
    q = s.add_parser('discover', help='Tiered discovery of repos, concurrently: tips, fetch, check, preview')
    q.add_argument('--run', required=True)
    q.add_argument('--repo', action='append', help='repo name from the run scope (repeatable)')
    q.add_argument('--pending', action='store_true', help='every repo not yet discovered with the current recipe')
    q.add_argument('--limit', type=int, help='only the next N pending repos (preview early, keep discovering)')
    q.add_argument('--via', choices=['git', 'api'], default='git',
                   help='api: read only `Check paths` through the SCM adapter; clone only repos that need the change')
    q.add_argument('--workers', type=int, default=4)
    q.add_argument('--workspaces', default=None, help='default: ~/.multi-repo/clones')
    q.add_argument('--base', action='append', help='dev branch; default: repo-facts dev_branch, else remote default')
    q.add_argument('--pattern', action='append', help="extra branches, e.g. 'feature/*'")
    q.add_argument('--days', type=int, help='only extra branches with commits in the last N days')
    q.add_argument('--no-apply', action='store_true')
    q.add_argument('--verbose', action='store_true', help='print every repo and branch (default: a summary)')
    q.add_argument('--full', action='store_true', help='check every branch on a full checkout (no sparse checks)')
    q = s.add_parser('override', help="Correct the check's verdict for one target (user-approved; reported)")
    q.add_argument('--run', required=True)
    q.add_argument('--target', required=True)
    q.add_argument('--status', required=True, choices=['needs_change', 'compliant', 'not_applicable'])
    q.add_argument('--evidence', required=True, help="the user's words approving the override")
    q = s.add_parser('capture', help="Record a target worktree's diff after an agent edit")
    q.add_argument('--run', required=True)
    q.add_argument('--target', required=True)
    q = s.add_parser('decide', help='Record a judgement on an undecided target (maybe/unknown)')
    q.add_argument('--run', required=True)
    q.add_argument('--target', required=True)
    q.add_argument('--status', required=True, choices=['needs_change', 'compliant', 'not_applicable'])
    q.add_argument('--evidence', required=True)
    q = s.add_parser('deliver', help='Check, build, commit and push targets (stops before the PR); --dry: check and build only')
    q.add_argument('--run', required=True)
    q.add_argument('--target', nargs='+', help='target IDs; or --ready / --all')
    q.add_argument('--ready', action='store_true', help='every target ready to deliver now (within the CI window)')
    q.add_argument('--all', action='store_true', help='with --dry: every target to change that is not validated yet')
    q.add_argument('--limit', type=int, help='with --ready: at most N targets this round')
    q.add_argument('--dry', action='store_true', help='validate only, nothing committed (no approval needed)')
    q.add_argument('--worker', default='deliver', help='your worker name if you claimed these targets with mr take')
    q.add_argument('--workers', type=int, default=3)
    q.add_argument('--message', help='commit subject; default "<KEY> <recipe title>"')
    q.add_argument('--build', help='override the repo-facts build command')
    q.add_argument('--timeout', type=int, default=1800)
    q.add_argument('--allow-no-build', action='store_true', help='only when the user approved check-only validation')
    q = s.add_parser('build', help='Run a build in a worktree (--dir) or on an unchanged commit (--clone --ref). '
                                   'Exit 0 passed, 1 failed, 2 blocked')
    q.add_argument('--command', required=True, help='shell command, e.g. "./mvnw -q verify"')
    q.add_argument('--dir')
    q.add_argument('--clone')
    q.add_argument('--ref')
    q.add_argument('--timeout', type=int, default=1800)
    q.add_argument('--log')
    a = ap.parse_args()
    global FORCE_FULL
    FORCE_FULL = bool(getattr(a, 'full', False))
    try:
        if a.cmd == 'check':
            if a.dir:
                o = run_check(a.recipe, a.dir)
            else:
                require(a.repo and a.ref, 'Use --dir, or --repo with --ref')
                sha = git(a.repo, 'rev-parse', '--verify', a.ref + '^{commit}').strip()
                with temp_tree(a.repo, sha) as tree:
                    o = {**run_check(a.recipe, tree), 'sha': sha}
        elif a.cmd == 'trial':
            o = trial(a.recipe, a.clone, a.branch)
        elif a.cmd == 'scan':
            o = scan(a.check, a.repos, a.project, a.host, a.paths, a.base, a.pattern, a.days, a.via, a.workers, a.name)
        elif a.cmd == 'discover':
            require(bool(a.repo) != a.pending, 'Use --repo NAME (repeatable) or --pending')
            res = discover(a.run, a.repo, a.pending, a.workers, a.workspaces, a.base, a.pattern, a.days, a.no_apply,
                           a.limit, a.via)
            o = res if a.verbose else discover_summary(a.run, res)
        elif a.cmd == 'branches':
            o = rename_branches(a.run, a.format)
        elif a.cmd == 'diagnose':
            o = diagnose(a.run)
        elif a.cmd == 'progress':
            o = progress(a.run, a.workers)
        elif a.cmd == 'clean':
            require(a.run or a.clones_days is not None, 'Use --run and/or --clones-days')
            o = clean(a.run, a.clones_days, a.all)
        elif a.cmd == 'capture':
            o = capture(a.run, a.target)
        elif a.cmd == 'override':
            o = override(a.run, a.target, a.status, a.evidence)
        elif a.cmd == 'decide':
            o = decide(a.run, a.target, a.status, a.evidence)
        elif a.cmd == 'deliver':
            tids = pick_targets(a.run, a.target, a.ready, a.all, a.dry, a.limit)
            o = deliver(a.run, tids, a.workers, a.message, a.build, a.timeout, a.allow_no_build, a.dry, a.worker)
            crashed = any(r['status'] == 'error' for r in o)
            if len(o) > 20:  # big batches: counts and the exceptions, not every target
                counts = {}
                for r in o:
                    counts[r['status']] = counts.get(r['status'], 0) + 1
                o = {'targets': len(o), 'counts': counts,
                     'not_ok': [r for r in o if r['status'] not in ('pushed', 'validated')][:30]}
            if crashed:  # a crash, not an outcome: make it visible to the caller
                print(json.dumps(o, indent=2))
                raise SystemExit(1)
        else:
            o = build(a.command, a.dir, a.clone, a.ref, a.timeout, a.log)
            print(json.dumps(o, indent=2))
            raise SystemExit(0 if o['passed'] else 1)
        print(json.dumps(o, indent=2))
    except (ValueError, OSError) as e:
        ap.exit(2, 'BLOCKED: ' + str(e) + '\n')


if __name__ == '__main__':
    main()
