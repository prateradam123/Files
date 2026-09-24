#!/usr/bin/env python3
"""Run ledger for multi-repo rollouts.

Anyone may write through these commands: every write takes a lock that waits (and is released by the OS
if a process dies), so parallel workers record their own results. The coordinator is the one who talks
to the user.

It records evidence it can't authenticate (the Jira lookup, the user's words). What it does guarantee,
together with the Git hooks: nothing is committed or pushed for a target unless that target is approved,
its approval still matches (scripted recipes: the byte-identical previewed diff; agent recipes: a diff
that passed validation and independent review), and the pilot rule and dependencies are satisfied.
"""
import argparse
import contextlib
import copy
import json
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CHECK_STATUSES, KEY, SHA, check_subject, commit_patch, digest, dir_hash,  # noqa: E402
                    git, load_target, now, own_commits, patch_hash, read_json, recipe_meta, require,
                    worktree_patch, write_json)
import limits  # noqa: E402

DONE = {'pr_opened', 'pr_updated', 'merged', 'closed'}
STOPPED = {'failed', 'blocked'}
STATUSES = DONE | STOPPED | {'validated', 'committed', 'pushed', 'observed'}
CI_STATES = {'passed', 'failed', 'pending', 'none'}
TASKS = ('decide', 'edit', 'change', 'review', 'deliver', 'open_pr')
MILESTONES = {'pr_opened', 'merged', 'published'}


# ---------------------------------------------------------------- storage

@contextlib.contextmanager
def locked(run):
    with open(Path(run) / '.ledger.lock', 'a+') as f:
        deadline = time.time() + 120
        while not limits._try_lock(f):
            require(time.time() < deadline, 'Ledger busy for 2 minutes; is a run_state command stuck?')
            time.sleep(0.05)
        try:
            yield
        finally:
            limits._unlock(f)


def load(run):
    return read_json(Path(run) / 'manifest.json')


def mutate(run, kind, fn):
    with locked(run):
        s = load(run)
        details = fn(s)
        s['revision'] += 1
        s['updated_at'] = now()
        # The event log is append-only in its own file: the manifest stays small, and every write of it cheap.
        with open(Path(run) / 'events.jsonl', 'a') as f:
            f.write(json.dumps({'sequence': s['revision'], 'at': s['updated_at'], 'kind': kind,
                                'details': details}, default=str) + '\n')
        write_json(Path(run) / 'manifest.json', s)
        return details


def active_recipe(run, s):
    v = s['recipe']['versions'][-1]
    return Path(run) / v['dir'], v


def recipe_intact(run, s):
    d, v = active_recipe(run, s)
    require(d.is_dir() and dir_hash(d) == v['sha256'],
            'The recipe snapshot inside the run was edited. Record a new version with `run_state.py revise`.')


def mode_of(run, s):
    return recipe_meta(active_recipe(run, s)[0])['mode']


def version_of(s):
    return s['recipe']['versions'][-1]['version']


def read_scope(path):
    p = Path(path)
    rows = read_json(p) if p.suffix == '.json' else [
        {'name': x.split()[0], 'url': x.split()[1] if len(x.split()) > 1 else None}
        for x in (line.strip() for line in p.read_text().splitlines()) if x and not x.startswith('#')]
    names = [r.get('name') for r in rows]
    require(rows and all(names) and len(set(names)) == len(names), 'Repo list is empty, unnamed, or has duplicates')
    return rows


def recipe_title(recipe_dir):
    for line in (Path(recipe_dir) / 'recipe.md').read_text().splitlines():
        if line.startswith('# '):
            return line[2:].strip()
    return Path(recipe_dir).name


# ---------------------------------------------------------------- run setup

def init_run(root, run_id, recipe, request, jira_key, jira_title, jira_url, jira_evidence, repos_file,
             branch_format='feature/{key}-{recipe}'):
    import re
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', run_id or ''), 'Unsafe run ID (letters, digits, . _ -)')
    require(KEY.fullmatch(jira_key or ''), 'Canonical Jira key required, e.g. ENG-123')
    require(all(x and x.strip() for x in (jira_title, jira_url, jira_evidence)),
            'Record what the Jira lookup returned: --jira-title, --jira-url, --jira-evidence')
    recipe = Path(recipe)
    require(recipe.is_dir() and (recipe / 'recipe.md').is_file(), 'Recipe must be a folder containing recipe.md')
    scope = read_scope(repos_file)
    run = Path(root) / run_id
    run.mkdir(parents=True, exist_ok=False)
    for sub in ('discovery', 'previews', 'logs'):
        (run / sub).mkdir()
    shutil.copytree(recipe, run / 'recipe.v1', ignore=shutil.ignore_patterns('__pycache__'))
    s = {'schema_version': 3, 'run_id': run_id, 'revision': 0, 'created_at': now(), 'updated_at': now(),
         'request': request, 'branch_format': branch_format,
         'jira': {'key': jira_key, 'title': jira_title, 'url': jira_url, 'evidence': jira_evidence, 'verified_at': now()},
         'scope': {'repos': scope, 'source': str(Path(repos_file).resolve())},
         'recipe': {'slug': recipe.resolve().name, 'title': recipe_title(recipe),
                    'versions': [{'version': 1, 'dir': 'recipe.v1', 'sha256': dir_hash(run / 'recipe.v1'),
                                  'source': str(recipe.resolve()), 'at': now(), 'reason': 'initial'}]},
         'plan': None, 'plan_hash': None, 'baseline': None, 'approved': {}, 'approvals': [],
         'settings': {'jira_comment': True, 'ci_waived': None}, 'claims': {}, 'results': {},
         'questions': [], 'aborted': {}, 'lessons': [], 'events': []}
    write_json(run / 'manifest.json', s)
    return {'run': str(run.resolve()), 'repos': len(scope), 'mode': recipe_meta(recipe)['mode']}


# ---------------------------------------------------------------- plan

def discovery_current(d, version):
    """Discovered = completed, and every branch previewed with the active recipe version."""
    return bool(d.get('complete')) and all(e.get('recipe_version') == version for e in d.get('targets', []))


def pending_repos(run, s):
    version, done = version_of(s), set()
    for f in (Path(run) / 'discovery').glob('*.json'):
        d = read_json(f)
        if discovery_current(d, version):
            done.add(d['repo'])
    return [r for r in s['scope']['repos'] if r['name'] not in done]


def open_question(s, tid):
    return next((q for q in s['questions'] if q['status'] == 'open' and q.get('target') == tid), None)


def check_plan(p):
    ids, pairs = set(), set()
    for t in p['targets']:
        require(t['id'] not in ids and (t['repo'], t['branch']) not in pairs, 'Duplicate target ' + t['id'])
        ids.add(t['id'])
        pairs.add((t['repo'], t['branch']))
    graph = {t['id']: [d['target'] for d in t.get('depends_on', [])] for t in p['targets']}
    for t in p['targets']:
        for d in t.get('depends_on', []):
            require(d.get('target') in ids and d['target'] != t['id'] and d.get('milestone') in MILESTONES,
                    f"Bad dependency on {t['id']} (milestones: {', '.join(sorted(MILESTONES))})")
    seen, stack = set(), set()

    def visit(n):
        require(n not in stack, 'Dependency cycle at ' + n)
        if n not in seen:
            stack.add(n)
            for x in graph[n]:
                visit(x)
            stack.discard(n)
            seen.add(n)
    for n in graph:
        visit(n)


def plan(run, strategy, deps_file=None, allow_missing=False):
    """Build the plan from the discovery files, merge repo facts, and write preview.md."""
    import repo_facts
    s = load(run)
    version, mode = version_of(s), mode_of(run, s)
    deps = read_json(deps_file) if deps_file else {}
    files = {read_json(f)['repo']: read_json(f) for f in sorted((Path(run) / 'discovery').glob('*.json'))}
    inventory, targets, missing = [], [], []
    for repo in s['scope']['repos']:
        d = files.get(repo['name'])
        if not d or not discovery_current(d, version):
            missing.append(repo['name'])
            inventory.append({'repo': repo['name'], 'outcome': 'pending', 'reason': 'discovery still running'})
            continue
        inventory.append({'repo': d['repo'], 'outcome': d['outcome'], 'reason': d['reason']})
        for e in d.get('targets', []):
            st = e['status']
            require(st in CHECK_STATUSES, f"{e['id']}: invalid check status {st}")
            t = {'id': e['id'], 'repo': d['repo'], 'branch': e['branch'], 'observed_sha': e.get('observed_sha'),
                 'evidence': e.get('evidence'), 'reason': e.get('evidence') or st}
            q = open_question(s, e['id'])
            if q:
                t.update(disposition='unresolved', reason='open question: ' + q['text'])
            elif st in ('maybe', 'unknown'):
                t.update(disposition='unresolved', undecided=(st == 'maybe' or mode == 'agent'))
            elif st == 'not_applicable':
                t['disposition'] = 'excluded'
            elif st == 'compliant':
                t['disposition'] = 'compliant'
            else:
                ok = bool(e.get('preview_patch_sha256')) and (e.get('post_check') or {}).get('status') == 'compliant'
                t.update(disposition='needs_change', jira_key=s['jira']['key'], worktree=e.get('worktree'),
                         source_branch=e.get('source_branch'), implementation=e.get('implementation'),
                         files=e.get('files'), depends_on=deps.get(e['id'], []),
                         preview_patch=e.get('preview_patch') if ok else None,
                         preview_patch_sha256=e.get('preview_patch_sha256') if ok else None,
                         preview_shape=e.get('preview_shape') if ok else None,
                         needs_edit=(mode == 'scripted' and not ok))
            targets.append(t)
    require(allow_missing or not missing,
            'Discovery not finished for: ' + ', '.join(missing) + ' (finish them, or pass --allow-missing)')
    p = {'scope': f"{len(s['scope']['repos'])} repos", 'strategy': strategy, 'inventory': inventory,
         'targets': targets, 'mode': mode}
    check_plan(p)
    for f in files.values():
        if f.get('facts'):
            repo_facts.merge(f['repo'], f['facts'], s['run_id'])

    def change(s):
        s['plan'] = p
        s['plan_hash'] = digest(p)
        bl = Path(run) / 'baseline.json'
        if not s.get('baseline') and not bl.exists():
            write_json(bl, p)  # the first plan, kept once for progress checks (not copied into every write)
        for repo in [r['name'] for r in s['scope']['repos']]:
            s['claims'].pop(repo, None)
        return {'plan_hash': s['plan_hash']}
    mutate(run, 'plan', change)
    s = load(run)
    counts = {k: sum(t['disposition'] == k for t in targets) for k in ('needs_change', 'compliant', 'excluded', 'unresolved')}
    return {'counts': counts, 'mode': mode, 'pending_repos': missing, 'preview': write_preview(run, s),
            'suggested_pilots': suggest_pilots(s),
            **short('needs_approval', needs_approval(s)), **short('needs_edit', [t['id'] for t in targets if t.get('needs_edit')]),
            **short('undecided', [t['id'] for t in targets if t.get('undecided')])}


def short(name, ids, limit=20):
    """Lists printed for the coordinator: complete up to `limit`, then the first ones plus the count."""
    return {name: ids[:limit], **({f'{name}_count': len(ids)} if len(ids) > limit else {})}


# ---------------------------------------------------------------- approval

def baseline(run, s):
    bl = Path(run) / 'baseline.json'
    return read_json(bl) if bl.exists() else (s.get('baseline') or {})


def targets(s):
    return (s.get('plan') or {}).get('targets', [])


def target_for(s, tid):
    t = next((t for t in targets(s) if t['id'] == tid), None)
    require(t is not None, 'Unknown target ' + str(tid))
    return t


def approval_valid(s, t):
    """One rule: an approval holds while nothing it covered has changed. Scripted: the previewed diff is
    byte-identical. Agent: same recipe version (each diff is then checked, built and reviewed before push)."""
    a = s['approved'].get(t['id'])
    if not a or t['disposition'] != 'needs_change':
        return False
    if a['patch'] is not None:  # scripted: the same bytes stay approved, even across a recipe revision
        return a['patch'] == t.get('preview_patch_sha256')
    return a['recipe_version'] == version_of(s)


def needs_approval(s):
    return [t['id'] for t in targets(s) if t['disposition'] == 'needs_change' and not t.get('needs_edit')
            and not approval_valid(s, t) and t['id'] not in s['aborted']]


def approve(run, evidence, only=None, pilots=(), pr_mode=None, no_jira=False, waive_ci=None, max_pending_ci=None):
    require(evidence.strip(), "Record the user's actual reply as evidence")

    def change(s):
        recipe_intact(run, s)
        require(s.get('plan'), 'Build the plan first')
        mode = mode_of(run, s)
        ids = list(only) if only else needs_approval(s)
        require(ids, 'Nothing needs approval')
        for tid in ids:
            t = target_for(s, tid)
            require(t['disposition'] == 'needs_change', f'{tid} is not a target to change')
            require(not t.get('needs_edit'), f'{tid} needs an edit and a preview before it can be approved')
        require(set(pilots) <= set(ids), 'Pilots must be among the approved targets')
        pm = pr_mode or ('draft' if mode == 'agent' else 'ready')
        for tid in ids:
            t = target_for(s, tid)
            s['approved'][tid] = {'at': now(), 'evidence': evidence, 'recipe_version': version_of(s),
                                  'patch': None if mode == 'agent' else t['preview_patch_sha256'],
                                  'pilot': tid in pilots, 'pr_mode': pm}
        if no_jira:
            s['settings']['jira_comment'] = False
        if waive_ci:
            s['settings']['ci_waived'] = waive_ci
        if max_pending_ci is not None:
            s['settings']['max_pending_ci'] = max_pending_ci
        s['approvals'].append({'at': now(), 'targets': ids, 'pilots': list(pilots), 'evidence': evidence})
        return {**short('approved', ids), 'approved_count': len(ids), 'pilots': list(pilots), 'pr_mode': pm,
                **short('still_needs_approval', needs_approval(s))}
    return mutate(run, 'approved', change)


def pilot_state(s):
    pilots = [tid for tid, a in s['approved'].items() if a['pilot'] and tid not in s['aborted']
              and approval_valid(s, target_for(s, tid))]
    waiting = [p for p in pilots if not (s['results'].get(p, {}).get('pr_url') and (
        (s['results'][p].get('ci') or {}).get('state') == 'passed' or s['settings']['ci_waived']))]
    return pilots, waiting


def dependency_met(s, d):
    r = s['results'].get(d['target'], {})
    return {'pr_opened': bool(r.get('pr_url')), 'merged': r.get('pr_state') == 'merged',
            'published': bool((r.get('published') or {}).get('version'))}[d['milestone']]


def check_gate(run, tid, action, current_sha=None, patch_sha=None):
    """Read-only. Hooks call it for commit/push/delete_branch; skills and scripts for create_pr, close_pr
    and jira_comment (run-level, no target)."""
    s = load(run)
    recipe_intact(run, s)
    if action == 'jira_comment':
        require(s['approved'], 'Nothing has been approved yet')
        require(s['settings']['jira_comment'], 'Posting to Jira was not approved for this run')
        return {'allowed': True, 'action': action}
    t = target_for(s, tid)
    if action in {'close_pr', 'delete_branch'}:
        require(tid in s['aborted'], f'{action} needs a recorded abort for {tid} (run_state.py abort)')
        return {'allowed': True, 'target': tid, 'action': action}
    require(action in {'commit', 'push', 'create_pr'}, 'Unknown action ' + action)
    require(tid not in s['aborted'], f'{tid} was aborted; only close_pr and delete_branch are allowed')
    require(s['approved'].get(tid), f'{tid} is not approved')
    require(approval_valid(s, t), f'{tid} changed since it was approved (new diff or recipe version); it needs approval again')
    q = open_question(s, tid)
    require(not q, f"{tid} has open question {q and q['id']}; answer it first (run_state.py answer)")
    a = s['approved'][tid]
    if not a['pilot']:
        pilots, waiting = pilot_state(s)
        require(not waiting, f"Waiting for pilot(s) {', '.join(waiting)}: PR open with green CI (or approve --waive-ci)")
    for d in t.get('depends_on', []):
        require(dependency_met(s, d), f"Dependency not met: {d['target']} must reach {d['milestone']}")
    r = s['results'].get(tid, {})
    has_pr = bool(r.get('pr_url')) and r.get('pr_state') != 'closed'
    if not has_pr:  # after the PR exists, the PR is the review surface: content and freshness aren't re-gated
        require(current_sha and SHA.fullmatch(current_sha), 'Supply the destination SHA')
        require(current_sha == t['observed_sha'],
                f"Destination {t['branch']} moved ({t['observed_sha'][:10]} -> {current_sha[:10]}). Rediscover this "
                'repo (recipe_run.py discover --repo) and re-plan: an identical diff keeps its approval.')
        if action in {'commit', 'push'}:
            require(patch_sha, 'Patch hash required for commit/push')
            if a['patch'] is not None:
                require(patch_sha == a['patch'], f'The change for {tid} differs from the approved preview. Stage the '
                        'whole previewed change (`git add -A`); if it must differ, re-preview and get it approved.')
            else:
                base = (load_target(t['worktree']) or {}).get('base_sha') if t.get('worktree') else None
                require(r.get('validated_patch_sha256') == patch_sha and r.get('validated_base') == base,
                        f'{tid}: this exact change on this base has not passed validation (mr deliver --dry)')
                require(r.get('reviewed_patch_sha256') == patch_sha and r.get('reviewed_base') == base,
                        f'{tid}: this exact change on this base has not been reviewed by change-verifier')
    exception = s.get('exceptions', {}).get(tid)
    return {'allowed': True, 'target': tid, 'jira_key': t['jira_key'],
            'pr_mode': 'draft' if exception else a['pr_mode'], 'content_checked': not has_pr,
            **({'exception': exception['kind']} if exception else {})}


# ---------------------------------------------------------------- work queue

def is_done(s, tid):
    r = s['results'].get(tid, {})
    if r.get('status') == 'closed':
        return True
    if r.get('status') not in DONE:
        return False
    a = s['approved'].get(tid) or {}
    if a.get('patch') is not None:
        return r.get('delivered_patch') == a['patch']
    return r.get('recipe_version') == version_of(s)


def in_flight(s, exclude=None):
    """Everything that is, or is about to be, waiting on CI: PRs with pending CI, pushes awaiting a PR,
    and active delivery claims (reservations)."""
    busy = set(pending_ci(s))
    busy |= {t for t, r in s['results'].items() if r.get('status') == 'pushed' and t not in s['aborted']}
    busy |= {t for t, c in s['claims'].items() if c.get('task') in ('deliver', 'open_pr') and claim_active(s, t)}
    busy.discard(exclude)
    return busy


def ci_limit(s, override=None):
    return override if override is not None else s['settings'].get('max_pending_ci')


def reserve(run, tid, worker='deliver', ttl=7200):
    """Atomically admit one delivery: respects other workers' claims and the CI window."""
    def change(s):
        c = s['claims'].get(tid)
        if c and claim_active(s, tid) and c['worker'] != worker:
            return {'ok': False, 'reason': f"claimed by {c['worker']}"}
        limit = ci_limit(s)
        if limit is not None and not (c and c['worker'] == worker) and len(in_flight(s, tid)) >= limit:
            return {'ok': False, 'reason': f'CI window full ({len(in_flight(s, tid))} in flight, limit {limit})'}
        s['claims'][tid] = {'worker': worker, 'task': 'deliver', 'at': now(),
                            'expires_at': (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()}
        return {'ok': True}
    return mutate(run, 'reserve', change)


def release(run, tid, worker=None):
    def change(s):
        c = s['claims'].get(tid)
        if c and (worker is None or c['worker'] == worker):
            s['claims'].pop(tid)
        return {'target': tid}
    return mutate(run, 'release', change)


def pending_ci(s):
    return [t for t, r in s['results'].items() if r.get('pr_url') and t not in s['aborted']
            and r.get('pr_state') not in {'merged', 'closed'} and (r.get('ci') or {}).get('state', 'pending') == 'pending']


def task_for(s, t):
    """What this target needs next, if anything someone can do now."""
    tid, r = t['id'], s['results'].get(t['id'], {})
    if tid in s['aborted'] or open_question(s, tid) or r.get('status') in STOPPED:
        return None
    if t.get('undecided'):
        return 'decide'
    if t['disposition'] != 'needs_change':
        return None
    if not approval_valid(s, t):
        return 'edit' if t.get('needs_edit') else None
    if is_done(s, tid):
        return None
    if not s['approved'][tid]['pilot'] and pilot_state(s)[1]:
        return None
    if not all(dependency_met(s, d) for d in t.get('depends_on', [])):
        return None
    if r.get('status') == 'pushed':
        return 'open_pr'
    if s['approved'][tid]['patch'] is None:  # agent recipe: edit -> validate -> review -> deliver
        if not (r.get('validated_patch_sha256') and r.get('validated_version') == version_of(s)):
            return 'change'
        if r.get('reviewed_patch_sha256') != r['validated_patch_sha256']:
            return 'review'
    return 'deliver'


def claim_active(s, tid):
    c = s['claims'].get(tid)
    return bool(c) and datetime.fromisoformat(c['expires_at']) > datetime.now(timezone.utc)


def take(run, worker=None, tasks=None, limit=1, ttl=7200, max_pending_ci=None, renew=False):
    """Hand out the next items. With --worker, claim them (a claim ends when the worker records a result,
    or expires). Without, just show what's ready. --renew extends this worker's claims (long upgrades)."""
    if renew:
        require(worker, '--renew needs --worker')

        def extend(s):
            exp = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()
            mine = [t for t, c in s['claims'].items() if c['worker'] == worker]
            for t in mine:
                s['claims'][t]['expires_at'] = exp
            return {'renewed': mine, 'expires_at': exp}
        return mutate(run, 'renew', extend)

    def pick(s):
        items, cap = [], None
        lim = ci_limit(s, max_pending_ci)
        if lim is not None:
            cap = max(0, lim - len(in_flight(s)))
        run_dir = str(active_recipe(run, s)[0].resolve())
        for t in targets(s):
            task = task_for(s, t)
            if not task or claim_active(s, t['id']) or (tasks and task not in tasks):
                continue
            if task == 'deliver' and cap is not None:
                if cap <= 0:
                    continue
                cap -= 1
            a, r = s['approved'].get(t['id'], {}), s['results'].get(t['id'], {})
            item = {'target': t['id'], 'task': task, 'repo': t['repo'], 'branch': t['branch']}
            if task in ('decide', 'edit', 'change', 'review'):  # judgement work needs the context
                item.update(worktree=t.get('worktree'), recipe_dir=run_dir, evidence=(t.get('evidence') or '')[:200])
                ans = [{'q': q['text'][:200], 'a': q['answer']} for q in s['questions']
                       if q.get('target') == t['id'] and q['status'] == 'answered']
                if ans:
                    item['answers'] = ans
            if task == 'open_pr':
                item.update(worktree=t.get('worktree'), source_branch=t.get('source_branch'), pr_mode=a.get('pr_mode'))
            items.append(item)
            if len(items) >= limit:
                break
        if worker:
            exp = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()
            for i in items:
                s['claims'][i['target']] = {'worker': worker, 'task': i['task'], 'at': now(), 'expires_at': exp}
        return items
    return mutate(run, 'take', pick) if worker else pick(load(run))


# ---------------------------------------------------------------- results

def verify_delivery(s, t, r, had_pr):
    """Independent of the hooks (which --no-verify skips): re-read what was pushed."""
    wt = Path(t['worktree'] or '/nonexistent')
    if not wt.is_dir():
        return ['worktree missing; cannot verify pushed commits']
    info = load_target(wt) or {}
    base, problems = info.get('base_sha') or t['observed_sha'], []
    try:
        for sha, subj in own_commits(wt, base, r['source_sha'], f"origin/{t['branch']}"):
            try:
                check_subject(t['jira_key'], subj)
            except ValueError:
                problems.append(f'commit {sha[:10]} lacks the "{t["jira_key"]} " prefix')
        r['delivered_patch'] = patch_hash(commit_patch(wt, base, r['source_sha']))
        a = s['approved'].get(t['id']) or {}
        if not had_pr:
            want = a.get('patch') or s['results'].get(t['id'], {}).get('reviewed_patch_sha256')
            if r['delivered_patch'] != want:
                problems.append('pushed change differs from what was approved/reviewed')
    except ValueError as e:
        problems.append('could not verify: ' + str(e))
    return problems


def record(run, tid, status, evidence, validation=None, pr_url=None, pr_state=None, ci=None, checks=(),
           questions=(), lessons=(), published=None, reviewed=False, extra=None, keep_claim=False, patch_sha=None):
    """Record one step for one target. SHAs and hashes are read from the worktree, never typed. Recording
    ends the recorder's claim. PR statuses are re-verified against the pushed commits."""
    run = str(Path(run).resolve())
    s = load(run)
    t = target_for(s, tid)
    require(status in STATUSES, 'status must be one of ' + ', '.join(sorted(STATUSES)))
    require(evidence and evidence.strip(), 'evidence required')
    require(ci is None or ci in CI_STATES, 'ci must be one of ' + '|'.join(sorted(CI_STATES)))
    require(validation in (None, 'passed', 'failed', 'blocked', 'exception'), 'validation: passed|failed|blocked|exception')
    require(pr_state in (None, 'open', 'draft', 'merged', 'closed'), 'pr_state: open|draft|merged|closed')
    if pr_url:
        import re
        require(re.search(r'/pull(?:s|-requests)?/\d+', pr_url), f'--pr-url does not look like a PR link: {pr_url}')
    wt = Path(t.get('worktree') or '/nonexistent')
    info = load_target(wt) if wt.is_dir() else None
    needs_hash = info and (validation in ('passed', 'exception') or reviewed)  # hash only what gets stored
    current = (patch_sha or patch_hash(worktree_patch(wt, info['base_sha']))) if needs_hash else None
    r = {'status': status, 'evidence': evidence, 'at': now(), 'recipe_version': version_of(s), **(extra or {})}
    if validation:
        r['validation'] = validation
    if validation in ('passed', 'exception'):
        require(current, f'{tid}: no worktree to hash')
        r.update(validated_patch_sha256=current, validated_version=version_of(s), validated_base=info['base_sha'],
                 validated_recipe=s['recipe']['versions'][-1]['sha256'])
    if reviewed:
        require(current, f'{tid}: no worktree to hash')
        r.update(reviewed_patch_sha256=current, reviewed_base=info['base_sha'])
    if info and status in {'committed', 'pushed'} | (DONE - {'closed'}):
        r.update(source_sha=git(wt, 'rev-parse', 'HEAD').strip(), source_branch=info['source_branch'])
    for k, v in (('pr_url', pr_url), ('pr_state', pr_state), ('checks', list(checks) or None)):
        if v:
            r[k] = v
    if ci:
        r['ci'] = {'state': ci, 'at': now()}
    if published:
        r['published'] = {'version': published}
    if status in DONE - {'closed'}:
        require(r.get('pr_url') or s['results'].get(tid, {}).get('pr_url'), 'PR statuses need --pr-url')
        require(r.get('source_sha'), 'PR statuses need the target worktree')
    had_pr = bool(s['results'].get(tid, {}).get('pr_url'))
    problems = verify_delivery(s, t, r, had_pr) if status in DONE - {'closed'} else []

    def change(s):
        prev = s['results'].get(tid, {})
        new = {**prev, **r}
        if status == 'observed':
            new['status'] = prev.get('status', 'observed')
        if problems:
            new.update(status='blocked', violations=problems)
            s['questions'].append({'id': f"q{len(s['questions']) + 1}", 'status': 'open', 'at': now(), 'target': tid,
                                   'text': 'Policy check failed: ' + '; '.join(problems),
                                   'recommendation': 'Inspect the PR; close it, or fix and retry.'})
        for q in questions:
            q = q if isinstance(q, dict) else {'text': q}
            s['questions'].append({'id': f"q{len(s['questions']) + 1}", 'status': 'open', 'at': now(),
                                   'target': tid, **q})
        s['lessons'] += [{'at': now(), 'target': tid, 'text': x} for x in lessons]
        s['results'][tid] = new
        if not keep_claim:
            s['claims'].pop(tid, None)
        return {'target': tid, 'status': new['status'], 'violations': problems}
    return mutate(run, 'record', change)


# ---------------------------------------------------------------- questions and change of course

CHOICES = {'draft': 'open the PR as a draft despite the recorded failure (only this target, only this failure)',
           'skip': 'leave this target out of the rollout', 'retry': 'try again (after a fix or an investigation)'}


def ask(run, text, recommendation=None, target=None, repo=None, choices=None, exception=None):
    require(text.strip() and (target or repo), 'text and --target or --repo required')

    def change(s):
        q = {'id': f"q{len(s['questions']) + 1}", 'status': 'open', 'at': now(), 'target': target, 'repo': repo,
             'text': text, 'recommendation': recommendation, **({'choices': choices} if choices else {}),
             **({'exception': exception} if exception else {})}
        s['questions'].append(q)
        if target:
            s['claims'].pop(target, None)
        return q
    return mutate(run, 'asked', change)


def answer(run, qids, text, evidence, choice=None):
    """Record the user's answer to one or several questions (a group from `mr questions`). Questions that offer
    choices need one; each choice has a defined effect."""
    require(text.strip() and evidence.strip(), 'Answer and evidence required')
    if isinstance(qids, str):
        qids = [qids]
    return [_answer_one(run, q, text, evidence, choice) for q in qids]


def _answer_one(run, qid, text, evidence, choice):
    def change(s):
        q = next((q for q in s['questions'] if q['id'] == qid), None)
        require(q and q['status'] == 'open', 'No open question ' + qid)
        tid = q.get('target')
        if q.get('choices'):
            require(choice in q['choices'], f"{qid} needs --choice {'|'.join(q['choices'])}: "
                    + '; '.join(f'{c} = {CHOICES.get(c, c)}' for c in q['choices']))
        q.update(status='answered', answer=text, choice=choice, answer_evidence=evidence, answered_at=now())
        if choice == 'skip':
            s['aborted'][tid] = {'at': now(), 'evidence': evidence, 'via': qid}
        elif choice == 'draft':
            require(q.get('exception'), f'{qid} has no failure evidence to make an exception for')
            s.setdefault('exceptions', {})[tid] = {**q['exception'], 'question': qid, 'evidence': evidence, 'at': now()}
        r = s['results'].get(tid or '', {})
        if r.get('status') in STOPPED and choice != 'skip':
            r['status'] = 'retry'  # an answered question puts its target back in the queue
        return q
    return mutate(run, 'answered', change)


def retry(run, tid, evidence):
    require(evidence.strip(), 'Say why a retry will work now')

    def change(s):
        target_for(s, tid)
        r = s['results'].setdefault(tid, {})
        require(r.get('status') in STOPPED, f'{tid} is not failed/blocked')
        r.update(status='retry', retry_evidence=evidence)
        return {'target': tid}
    return mutate(run, 'retry', change)


def add_scope(run, repos_file, evidence):
    """Add repos to a running rollout. They're discovered with `discover --pending`; their targets need
    approval like any other."""
    require(evidence.strip(), "Record the user's words asking for these repos")
    rows = read_scope(repos_file)

    def change(s):
        have = {r['name'] for r in s['scope']['repos']}
        new = [r for r in rows if r['name'] not in have]
        s['scope']['repos'] += new
        s.setdefault('scope_changes', []).append({'at': now(), 'added': [r['name'] for r in new], 'evidence': evidence})
        return {'added': [r['name'] for r in new], 'already_in_scope': len(rows) - len(new),
                'next': f"mr discover --run {s['run_id']} --pending, then mr plan"}
    return mutate(run, 'scope_added', change)


def add_override(run, tid, status, evidence):
    def change(s):
        target_for(s, tid)
        s.setdefault('overrides', []).append({'at': now(), 'target': tid, 'status': status, 'evidence': evidence})
        return {'target': tid, 'status': status}
    return mutate(run, 'override', change)


def set_branch_format(run, fmt):
    def change(s):
        s['branch_format'] = fmt
        return {'branch_format': fmt}
    return mutate(run, 'branch_format', change)


def signature(text):
    """Group similar messages: drop paths, SHAs, target IDs and numbers."""
    import re
    t = re.sub(r'/\S+', '<path>', str(text or ''))
    t = re.sub(r'\b[0-9a-f]{7,40}\b', '<sha>', t)
    t = re.sub(r'`[^`]*`', '<x>', t)
    t = re.sub(r'\b[a-z0-9-]+--[a-z0-9-]+-[0-9a-f]{5}\b', '<target>', t)
    return re.sub(r'\d+', 'N', t)[:160]


def grouped(items, text_of):
    groups = {}
    for x in items:
        groups.setdefault(signature(text_of(x)), []).append(x)
    return sorted(groups.values(), key=len, reverse=True)


def questions_view(s, full=False):
    openq = [q for q in s['questions'] if q['status'] == 'open']
    if full:
        return openq
    return [{'ids': [q['id'] for q in g], 'count': len(g), 'targets': [q.get('target') or q.get('repo') for q in g],
             'text': g[0]['text'], 'recommendation': g[0].get('recommendation'),
             **({'choices': g[0]['choices']} if g[0].get('choices') else {})} for g in grouped(openq, lambda q: q['text'])]


def revise(run, recipe, evidence):
    """A corrected recipe mid-run. Approvals stop being valid (new version); open PRs get follow-ups."""
    require(evidence.strip(), 'Explain why the recipe changed')
    recipe = Path(recipe)
    require((recipe / 'recipe.md').is_file(), 'Recipe folder with recipe.md required')

    def change(s):
        n = version_of(s) + 1
        dest = Path(run) / f'recipe.v{n}'
        shutil.copytree(recipe, dest, ignore=shutil.ignore_patterns('__pycache__'))
        v = {'version': n, 'dir': dest.name, 'sha256': dir_hash(dest), 'source': str(recipe.resolve()),
             'at': now(), 'reason': evidence}
        s['recipe']['versions'].append(v)
        return v
    return mutate(run, 'recipe_revised', change)


def abort(run, tids, evidence, everything=False):
    require(evidence.strip(), 'Record the actual abort instruction')

    def change(s):
        ids = [t['id'] for t in targets(s) if t['disposition'] == 'needs_change'] if everything else list(tids)
        require(ids, 'Name targets or pass --all')
        for tid in ids:
            target_for(s, tid)
            s['aborted'][tid] = {'at': now(), 'evidence': evidence}
            s['claims'].pop(tid, None)
        return {'aborted': ids, 'close_these_prs': [
            {'target': tid, 'pr_url': s['results'][tid]['pr_url'], 'source_branch': target_for(s, tid)['source_branch']}
            for tid in ids if s['results'].get(tid, {}).get('pr_url') and s['results'][tid].get('status') != 'closed']}
    return mutate(run, 'aborted', change)


# ---------------------------------------------------------------- reports

def md(v):
    return str(v if v not in (None, '') else '—').replace('|', '\\|').replace('\n', ' ')


def status_of(s, t):
    tid, r = t['id'], s['results'].get(t['id'], {})
    if tid in s['aborted']:
        return 'aborted' + (' (PR closed)' if r.get('status') == 'closed' else ' — PR still open' if r.get('pr_url') else '')
    if open_question(s, tid):
        return 'waiting on an answer'
    if t['disposition'] == 'needs_change' and not approval_valid(s, t):
        return 'needs an edit' if t.get('needs_edit') else 'needs approval'
    st = r.get('status') or task_for(s, t) and f"ready: {task_for(s, t)}" or 'waiting'
    if tid in s.get('exceptions', {}):
        st += ' (draft: baseline build already failing)'
    return st


def suggest_pilots(s):
    """One pilot per kind of diff (scripted), or two (agent); prefer targets already validated, never ones with
    open questions or a failed validation."""
    ok = [t for t in targets(s) if t['disposition'] == 'needs_change' and not t.get('needs_edit')
          and not open_question(s, t['id']) and s['results'].get(t['id'], {}).get('validation') not in ('failed', 'blocked')
          and t['id'] not in s['aborted']]
    rank = lambda t: (s['results'].get(t['id'], {}).get('validation') != 'passed', t['id'])
    if s['plan']['mode'] == 'agent':
        return [t['id'] for t in sorted(ok, key=rank)[:2]]
    groups = {}
    for t in ok:
        groups.setdefault(t.get('preview_shape') or t['id'], []).append(t)
    return [sorted(g, key=rank)[0]['id'] for g in sorted(groups.values(), key=len, reverse=True)]


def write_preview(run, s):
    p, mode = s['plan'], s['plan']['mode']
    groups = {}
    for t in p['targets']:
        if t['disposition'] == 'needs_change' and t.get('preview_patch'):
            groups.setdefault(t.get('preview_shape') or t['id'], []).append(t)
    counts = {k: sum(t['disposition'] == k for t in p['targets']) for k in ('needs_change', 'compliant', 'excluded', 'unresolved')}
    L = [f"# Preview: {s['recipe']['title']} ({s['run_id']})", '',
         '**Local preview: nothing has been pushed and no PR exists.**', '',
         f"Jira {s['jira']['key']} · recipe v{version_of(s)} · mode **{mode}** · "
         f"validation {recipe_meta(active_recipe(run, s)[0])['validation']}", '',
         f"**{counts['needs_change']} to change** · {counts['compliant']} already compliant · "
         f"{counts['excluded']} not applicable · {counts['unresolved']} unresolved", '']
    if mode == 'agent':
        L += ['This is an **agent** recipe: approve the targets and the approach. Each diff is then made by a worker, '
              'must pass the recipe check, the full build and an independent review, and goes out as a PR '
              '(draft by default). Reviewing each PR is where each diff is approved. Pilot first.', '']
    else:
        L += ['Approving approves these exact diffs. A diff that later changes, and any new target, needs approval again.', '']
    for i, (shape, ts) in enumerate(sorted(groups.items(), key=lambda g: -len(g[1])), 1):
        L += [f'## Diff {i}: {len(ts)} target(s)', '']
        L += [f"- `{t['id']}` — {t['repo']} → `{t['branch']}`" + ('' if approval_valid(s, t) else ' *(needs approval)*')
              for t in ts]
        body = Path(ts[0]['preview_patch']).read_text() if Path(ts[0]['preview_patch'] or '').is_file() else '(missing)'
        lines = body.splitlines()
        body = '\n'.join(lines[:300]) + (f"\n... truncated; full patch: {ts[0]['preview_patch']}" if len(lines) > 300 else '')
        L += ['', '```diff', body.rstrip(), '```', '']
    no_diff = [t for t in p['targets'] if t['disposition'] == 'needs_change' and not t.get('preview_patch')]
    if no_diff:
        L += ['## To change, diff made after approval (agent)' if mode == 'agent' else '## Need an edit before approval', '']
        L += [f"- `{t['id']}` — {t['repo']} → `{t['branch']}`: {md(t['reason'])}" for t in no_diff] + ['']
    for disp, title in (('unresolved', 'Unresolved — not changed'), ('excluded', 'Not applicable'), ('compliant', 'Already compliant')):
        rows = [t for t in p['targets'] if t['disposition'] == disp]
        if rows:
            L += [f'## {title}', ''] + [f"- {t['repo']} `{t['branch']}` — {md(t['reason'])}" for t in rows] + ['']
    other = [r for r in p['inventory'] if r['outcome'] != 'scanned']
    if other:
        L += ['## Repos not scanned', ''] + [f"- {r['repo']}: {r['outcome']} — {r['reason']}" for r in other] + ['']
    openq = [q for q in s['questions'] if q['status'] == 'open']
    if openq:
        L += ['## Open questions', ''] + [f"- **{q['id']}** ({q.get('target') or q.get('repo')}): {md(q['text'])}"
                                           + (f" Recommended: {md(q['recommendation'])}" if q.get('recommendation') else '')
                                           for q in openq] + ['']
    out = Path(run) / 'preview.md'
    out.write_text('\n'.join(L) + '\n')
    return str(out.resolve())


def jira_table(s):
    rows = [t for t in targets(s) if t['disposition'] == 'needs_change']
    live = [t for t in rows if t['id'] not in s['aborted']]
    res = [s['results'].get(t['id'], {}) for t in live]
    prs = sum(bool(r.get('pr_url')) and r.get('pr_state') not in {'merged', 'closed'} for r in res)
    merged = sum(r.get('pr_state') == 'merged' for r in res)
    green = sum((r.get('ci') or {}).get('state') == 'passed' and r.get('pr_state') != 'merged' for r in res)
    L = [f"*{s['recipe']['title']}* — {len(live)} to change: {merged} merged, {prs} PRs open ({green} green CI)"
         + (f", {len(rows) - len(live)} withdrawn" if len(rows) > len(live) else '')
         + f" (run {s['run_id']}, updated {now()[:16].replace('T', ' ')} UTC)", '',
         '| Repo | Branch | PR | CI | Status |', '|---|---|---|---|---|']
    L += ['| ' + ' | '.join(md(x) for x in (t['repo'], t['branch'], s['results'].get(t['id'], {}).get('pr_url'),
                                            (s['results'].get(t['id'], {}).get('ci') or {}).get('state'),
                                            status_of(s, t))) + ' |' for t in rows]
    other = [t for t in targets(s) if t['disposition'] == 'unresolved']
    if other:
        L += ['', 'Not changed (unresolved): ' + ', '.join(f"{t['repo']} `{t['branch']}`" for t in other)]
    return '\n'.join(L) + '\n'


def report(run):
    s = load(run)
    created = datetime.fromisoformat(s['created_at'])
    L = ['# Rollout report: ' + s['run_id'], '', 'As of: ' + now(), '', '**Request:** ' + s['request'], '',
         f"Jira {s['jira']['key']} · recipe v{version_of(s)}", '']
    stopped, needs = [], []
    if not s['plan']:
        L += ['Discovery in progress; no plan yet.']
    else:
        needs = needs_approval(s)
        pilots, waiting = pilot_state(s)
        L += [f"**Mode:** {s['plan']['mode']} · **Needs approval:** {len(needs)} · **Pilots:** "
              + (', '.join(pilots) or 'none') + (f" (waiting: {', '.join(waiting)})" if waiting else ''), '',
              '| Target | Repo | Destination | Plan | Status | Validation | CI | PR |', '|---|---|---|---|---|---|---|---|']
        for t in targets(s):
            r = s['results'].get(t['id'], {})
            L.append('| ' + ' | '.join(md(x) for x in (t['id'], t['repo'], t['branch'], t['disposition'],
                                                       status_of(s, t) if t['disposition'] == 'needs_change' or t.get('undecided') else '—',
                                                       r.get('validation'), (r.get('ci') or {}).get('state'), r.get('pr_url'))) + ' |')
            if r.get('status') in STOPPED:
                stopped.append({'target': t['id'], 'status': r['status'], 'reason': r.get('evidence')})
        L += ['', '## Inventory', '', '| Repo | Outcome | Reason |', '|---|---|---|']
        L += ['| ' + ' | '.join(md(r.get(k)) for k in ('repo', 'outcome', 'reason')) + ' |' for r in s['plan']['inventory']]
    if stopped:
        L += ['', '## Stopped (grouped by reason)', '']
        for g in grouped(stopped, lambda x: x['reason']):
            L += [f"- **{len(g)}×** {md(g[0]['reason'])}", '  ' + ', '.join(f"`{x['target']}`" for x in g)]
    openq = [q for q in s['questions'] if q['status'] == 'open']
    if openq:
        L += ['', '## Open questions', ''] + [f"- **{q['id']}** ({q.get('target') or q.get('repo')}): {md(q['text'])}"
                                              + (f" Recommended: {md(q.get('recommendation'))}" if q.get('recommendation') else '')
                                              for q in openq]
    perf = limits.summary(run)
    if perf['stages']:
        L += ['', '## Performance (seconds of work per item; slot waits counted separately)', '',
              '| Stage | Count | Failed | Cached | p50 | p95 | Total |', '|---|---|---|---|---|---|---|']
        L += [f"| {k} | {v['n']} | {v['failed']} | {v['cached']} | {v['p50']:.1f} | {v['p95']:.1f} | {v['total']:.0f} |"
              for k, v in sorted(perf['stages'].items(), key=lambda kv: -kv[1]['total'])]
        L += ['', 'Waiting for slots: ' + (', '.join(f'{k} {v:.0f}s' for k, v in perf['pool_wait'].items()) or 'none'),
              '', f"**Bottleneck:** {perf['bottleneck']}"]
    L += ['', f"Elapsed since start: {(datetime.now(timezone.utc) - created).total_seconds() / 60:.1f} min "
              '(includes waiting on people and CI)']
    if s.get('overrides'):
        L += ['', '## Overrides of the check (user-approved)', '']
        L += [f"- `{o['target']}` → {o['status']}: {md(o['evidence'])} ({o['at'][:16]})" for o in s['overrides']]
    if s['lessons']:
        L += ['', '## Lessons', ''] + [f"- {md(n.get('target'))}: {md(n['text'])}" for n in s['lessons']]
    (Path(run) / 'report.md').write_text('\n'.join(L) + '\n')
    table = jira_table(s) if s['plan'] else ''
    (Path(run) / 'jira-table.md').write_text(table)
    done = sum(1 for t in targets(s) if is_done(s, t['id']))
    changing = [t for t in targets(s) if t['disposition'] == 'needs_change' and t['id'] not in s['aborted']]
    return {'report': str((Path(run) / 'report.md').resolve()), 'jira_table': str((Path(run) / 'jira-table.md').resolve()),
            'targets': len(changing), 'done': done, 'in_flight': len(in_flight(s)) if s['plan'] else 0,
            'pushed_without_pr': [t for t, r in s['results'].items() if r.get('status') == 'pushed' and t not in s['aborted']],
            'needs_approval': {'count': len(needs), 'first': needs[:10]},
            'stopped': [{'count': len(g), 'reason': g[0]['reason'][:220], 'targets': [x['target'] for x in g][:8]}
                        for g in grouped(stopped, lambda x: x['reason'])],
            'open_questions': len(openq), 'overrides': len(s.get('overrides', []))}


# ---------------------------------------------------------------- CLI

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)

    def cmd(name, text):
        q = sub.add_parser(name, help=text)
        q.add_argument('--run', required=True)
        return q
    q = sub.add_parser('init', help='Start a run: snapshot the recipe, the repo list and the Jira lookup')
    for x in ('id', 'recipe', 'request', 'jira', 'jira-title', 'jira-url', 'jira-evidence', 'repos'):
        q.add_argument('--' + x, required=True)
    q.add_argument('--root', help='default: ~/.multi-repo/runs')
    q.add_argument('--branch-format')
    q = cmd('plan', 'Build the plan from discovery, merge repo facts, write preview.md')
    q.add_argument('--strategy', required=True)
    q.add_argument('--deps')
    q.add_argument('--allow-missing', action='store_true')
    q = cmd('approve', "Record the user's approval (default: every target that needs it)")
    q.add_argument('--evidence', required=True)
    q.add_argument('--targets', nargs='*')
    q.add_argument('--pilot', nargs='*', default=[])
    q.add_argument('--pr-mode', choices=['ready', 'draft'])
    q.add_argument('--no-jira', action='store_true', help='the user does not want the Jira status comment')
    q.add_argument('--waive-ci', help='pilots count as passed without green CI (say why)')
    q.add_argument('--max-pending-ci', type=int, help='for this run: at most N targets in flight toward CI at once')
    q = cmd('gate', 'Check whether an action is allowed right now')
    q.add_argument('--target')
    q.add_argument('--action', required=True)
    q.add_argument('--current-sha')
    q.add_argument('--patch-sha')
    q = cmd('take', 'Next ready items; with --worker, claim them')
    q.add_argument('--worker')
    q.add_argument('--task', nargs='*', choices=TASKS)
    q.add_argument('--limit', type=int, default=1)
    q.add_argument('--ttl', type=int, default=7200)
    q.add_argument('--max-pending-ci', type=int, help='override the run\'s CI window for this call')
    q.add_argument('--renew', action='store_true', help='extend this worker\'s claims (long upgrades)')
    q = cmd('record', 'Record a step for a target (ends your claim)')
    q.add_argument('--target', required=True)
    q.add_argument('--status', required=True, choices=sorted(STATUSES))
    q.add_argument('--evidence', required=True)
    q.add_argument('--validation', choices=['passed', 'failed', 'blocked', 'exception'])
    q.add_argument('--reviewed', action='store_true', help='change-verifier approves the current worktree diff')
    q.add_argument('--pr-url')
    q.add_argument('--pr-state', choices=['open', 'draft', 'merged', 'closed'])
    q.add_argument('--ci', choices=sorted(CI_STATES))
    q.add_argument('--check', action='append', default=[])
    q.add_argument('--question', action='append', default=[])
    q.add_argument('--lesson', action='append', default=[])
    q.add_argument('--published')
    q = cmd('ask', 'Record a question for the user (stops that target)')
    q.add_argument('--text', required=True)
    q.add_argument('--recommendation')
    q.add_argument('--target')
    q.add_argument('--repo')
    q = cmd('answer', "Record the user's answer (re-queues a stopped target)")
    q.add_argument('--question', required=True, nargs='+', help='one or more question IDs (a group from mr questions)')
    q.add_argument('--choice', choices=sorted(CHOICES), help='required when the question offers choices')
    q.add_argument('--answer', required=True)
    q.add_argument('--evidence', required=True)
    cmd('questions', 'Open questions, grouped (similar questions answered together)').add_argument(
        '--all', action='store_true', help='every question in full')
    q = cmd('retry', 'Put a failed/blocked target back in the queue')
    q.add_argument('--target', required=True)
    q.add_argument('--evidence', required=True)
    q = cmd('revise', 'Record a corrected recipe version mid-run')
    q.add_argument('--recipe', required=True)
    q.add_argument('--evidence', required=True)
    q = cmd('abort', 'Stop targets; allows closing their PRs and deleting their branches')
    q.add_argument('--targets', nargs='*', default=[])
    q.add_argument('--all', action='store_true')
    q.add_argument('--evidence', required=True)
    cmd('report', 'Write report.md and jira-table.md; print what needs attention')
    q = cmd('scope', 'Add repos to a running rollout')
    q.add_argument('--add', required=True, help='file of "<name> <clone-url>" lines')
    q.add_argument('--evidence', required=True, help="the user's words")
    cmd('show', 'Print the manifest')
    sub.add_parser('list', help='List runs').add_argument('--root')
    a = p.parse_args()
    try:
        c = a.cmd
        if c == 'init':
            from common import DEFAULT_CONFIG, config, data_home
            o = init_run(a.root or data_home() / 'runs', a.id, a.recipe, a.request, a.jira, a.jira_title, a.jira_url,
                         a.jira_evidence, a.repos,
                         a.branch_format or config().get('branch_format') or DEFAULT_CONFIG['branch_format'])
        elif c == 'plan':
            o = plan(a.run, a.strategy, a.deps, a.allow_missing)
        elif c == 'approve':
            o = approve(a.run, a.evidence, a.targets, a.pilot, a.pr_mode, a.no_jira, a.waive_ci, a.max_pending_ci)
        elif c == 'gate':
            o = check_gate(a.run, a.target, a.action, a.current_sha, a.patch_sha)
        elif c == 'take':
            o = take(a.run, a.worker, a.task, a.limit, a.ttl, a.max_pending_ci, a.renew)
        elif c == 'record':
            o = record(a.run, a.target, a.status, a.evidence, a.validation, a.pr_url, a.pr_state, a.ci, a.check,
                       a.question, a.lesson, a.published, a.reviewed)
        elif c == 'ask':
            o = ask(a.run, a.text, a.recommendation, a.target, a.repo)
        elif c == 'answer':
            done = answer(a.run, a.question, a.answer, a.evidence, a.choice)
            o = {'answered': [q['id'] for q in done], 'choice': a.choice,
                 'effect': {'skip': 'left out of the rollout', 'draft': 'will open as a draft with the recorded failure',
                            'retry': 'back in the queue'}.get(a.choice, 'recorded; stopped targets are back in the queue')}
        elif c == 'questions':
            o = questions_view(load(a.run), a.all)
        elif c == 'retry':
            o = retry(a.run, a.target, a.evidence)
        elif c == 'revise':
            o = revise(a.run, a.recipe, a.evidence)
        elif c == 'abort':
            o = abort(a.run, a.targets, a.evidence, a.all)
        elif c == 'report':
            o = report(a.run)
        elif c == 'scope':
            o = add_scope(a.run, a.add, a.evidence)
        elif c == 'show':
            o = load(a.run)
        else:
            o = [{'run_id': d['run_id'], 'updated_at': d['updated_at'], 'path': str(f.parent.resolve())}
                 for f in sorted(Path(a.root or __import__('common').data_home() / 'runs').glob('*/manifest.json'))
                 for d in [read_json(f)]]
        print(json.dumps(o, indent=2))
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as e:
        p.exit(2, 'BLOCKED: ' + str(e) + '\n')


if __name__ == '__main__':
    main()
