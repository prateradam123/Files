#!/usr/bin/env python3
"""Automation clones and read-only Git evidence. Installs the rollout hooks; never commits or pushes.

Clones are blobless partial clones without a checkout (`--filter=blob:none --no-checkout`): history and
trees arrive up front, file contents only when something reads them. Only the branches a run needs are
fetched, and a branch whose local tip already matches the remote is not fetched at all. Every network
operation holds a `git:<host>` slot (scripts/limits.py) and retries transient failures with back-off.
Servers that don't support filters simply send everything; nothing else changes.
"""
import argparse
import fnmatch
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (TOOLKIT, gitdir_of, check_subject, git, index_patch, load_target, own_commits, patch_hash,  # noqa: E402
                    require, worktree_patch, write_json)
import limits  # noqa: E402

HOOKS = ('commit-msg', 'pre-commit', 'pre-push')
MARKER = '# multi-repo-toolkit hook'


def norm_url(url):
    """Treat ssh/https spellings of one repo as the same origin (incl. Bitbucket Server's /scm/)."""
    u = url.strip()
    if u.startswith('file://'):
        u = u[len('file://'):]
    if not re.match(r'^[a-z+]+://', u) and not re.match(r'^[\w.-]+@[\w.-]+:', u):
        return str(Path(u).expanduser().resolve())
    u = re.sub(r'^[a-z+]+://', '', u)
    u = re.sub(r'^[^@/]+@', '', u)
    u = re.sub(r'^([^/:]+):(\d+)/', r'\1/', u)
    u = u.replace(':', '/', 1) if re.match(r'^[^/]+:', u) else u
    host, _, path = u.partition('/')
    path = re.sub(r'^scm/', '', path).rstrip('/')
    path = path[:-4] if path.endswith('.git') else path
    return (host + '/' + path).lower()


def origin_url(clone):
    m = re.search(r'\[remote "origin"\][^\[]*?\burl\s*=\s*(\S+)', (Path(clone) / '.git' / 'config').read_text()) \
        if (Path(clone) / '.git' / 'config').is_file() else None
    return m.group(1) if m else git(clone, 'remote', 'get-url', 'origin').strip()


def install_hooks(clone):
    clone = Path(clone).resolve()
    gitdir = gitdir_of(clone)
    hooks = gitdir / 'rollout-hooks'
    cfg = (gitdir / 'config').read_text() if (gitdir / 'config').is_file() else ''
    installed = (hooks / 'chain.json').is_file() and all((hooks / h).is_file() for h in HOOKS)
    if installed and re.search(r'hookspath\s*=\s*' + re.escape(str(hooks)) + r'\s*$', cfg, re.I | re.M) and \
            str(TOOLKIT / 'scripts' / 'hooks.py') in (hooks / HOOKS[0]).read_text():
        return {'hooks_path': str(hooks), 'chained_to': json.loads((hooks / 'chain.json').read_text())['previous_hooks_path']}
    hooks.mkdir(exist_ok=True)
    chain_file = hooks / 'chain.json'
    current = git(clone, 'config', '--get', 'core.hooksPath', check=False).strip() or None
    if chain_file.exists():
        chain = json.loads(chain_file.read_text())
        if current and current != str(hooks) and current != chain.get('previous_hooks_path'):
            chain['previous_hooks_path'] = current  # e.g. husky switched hooks: keep running theirs after ours
            write_json(chain_file, chain)
    else:
        chain = {'previous_hooks_path': current if current != str(hooks) else None,
                 'repo_hooks': str(gitdir / 'hooks')}
        write_json(chain_file, chain)
    script = TOOLKIT / 'scripts' / 'hooks.py'
    for name in HOOKS:
        p = hooks / name
        p.write_text(f'#!/bin/sh\n{MARKER}\nPY=$(command -v python3 || command -v python)\n'
                     f'exec "$PY" "{script}" {name} "$@"\n')
        p.chmod(0o755)
    git(clone, 'config', '--local', 'core.hooksPath', str(hooks))
    return {'hooks_path': str(hooks), 'chained_to': chain['previous_hooks_path']}


def hooks_check(repo):
    repo = Path(repo).resolve()
    cfg = gitdir_of(repo)
    cfg = (cfg.parent.parent if cfg.parent.name == 'worktrees' else cfg) / 'config'  # worktrees share the clone's config
    m = re.search(r'^\s*hookspath\s*=\s*(.+?)\s*$', cfg.read_text(), re.I | re.M) if cfg.is_file() else None
    path = m.group(1) if m else git(repo, 'config', '--get', 'core.hooksPath', check=False).strip()
    ok = path.endswith('rollout-hooks') and all(
        (Path(path) / h).is_file() and MARKER in (Path(path) / h).read_text() for h in HOOKS)
    target = load_target(repo)
    require(ok, f'Rollout hooks are not active in {repo} (core.hooksPath={path or "unset"}). A build tool '
                '(e.g. husky) may have replaced them. Re-run `git_probe.py prepare` on the clone.')
    return {'hooks_active': True, 'hooks_path': path, 'target': target and target.get('target')}


def host_of(url):
    n = norm_url(url)
    return 'local' if n.startswith('/') else n.split('/', 1)[0]


def net_git(cwd, *args, url, run=None, stage='fetch', repo=None, timeout=900):
    """A Git network operation: shared per-host slot, no prompts, transient-only retries, timed."""
    def go():
        try:
            p = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, text=True,
                               env=limits.net_env(), timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ValueError(f'git {args[0]} timed out after {timeout}s')
        if p.returncode:
            raise ValueError((p.stderr.strip() or 'git failed')[-600:] + f' [git {args[0]}]')
        return p.stdout + p.stderr
    with limits.timed(run, stage, 'git:' + host_of(url), repo=repo):
        return limits.retry(go)


def ls_remote(url, run=None, repo=None):
    """Branch tips and default branch in one cheap call, without a clone."""
    out = net_git(TOOLKIT, 'ls-remote', '--symref', url, 'HEAD', 'refs/heads/*', url=url, run=run,
                  stage='ls-remote', repo=repo, timeout=120)
    heads, default = {}, None
    for line in out.splitlines():
        if line.startswith('ref: refs/heads/') and line.endswith('\tHEAD'):
            default = line[len('ref: refs/heads/'):].split('\t')[0]
        elif '\trefs/heads/' in line:
            sha, ref = line.split('\t')
            heads[ref[len('refs/heads/'):]] = sha
    return {'default': default, 'heads': heads}


def is_partial(clone):
    return git(clone, 'config', '--get', 'remote.origin.promisor', check=False).strip() == 'true'


def prepare(url, path, branches=None, tips=None, run=None, repo=None):
    """Create or refresh an automation clone. With `branches`, fetch only those; with `tips` (from
    ls_remote), skip branches whose local remote-tracking ref already has that SHA."""
    p = Path(path).resolve()
    fetched, skipped = [], []
    if p.exists():
        require((p / '.git').exists(), 'Existing directory is not an automation clone: ' + str(p))
        origin = origin_url(p)
        require(norm_url(origin) == norm_url(url), f'Origin mismatch: {origin} vs {url}')
        if (p / '.git' / 'index').exists():  # someone checked something out here: look properly
            dirty = [x for x in git(p, 'status', '--porcelain').splitlines() if not x.startswith('D ')]
        else:  # a no-checkout clone holds nothing but .git; anything else is someone's work
            dirty = [x.name for x in p.iterdir() if x.name != '.git']
        require(not dirty, 'Dirty automation clone: preserve and reconcile before reuse')
        todo = list(branches) if branches else None
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        # Blobless partial clones save transfer but can cost the server more (they skip Bitbucket's pack
        # cache), so they are opt-in per host: {"partial_clone_hosts": ["bitbucket.example.com"]} in limits.json.
        partial = host_of(url) in limits.config().get('partial_clone_hosts', [])
        args = ['clone', *(['--filter=blob:none'] if partial else []), '--no-checkout', '--no-tags']
        if branches:
            args += ['--single-branch', '--branch', branches[0]]
        net_git(p.parent, *args, '--', url, str(p), url=url, run=run, stage='clone', repo=repo)
        if branches:
            git(p, 'update-ref', f'refs/remotes/origin/{branches[0]}', f'refs/heads/{branches[0]}')
            fetched.append(branches[0])
        todo = list(branches[1:]) if branches else []
    if todo is None:
        refspecs = ['+refs/heads/*:refs/remotes/origin/*']
    else:
        refspecs = []
        local_tips = {
            line.split(' ', 1)[0]: line.split(' ', 1)[1] for line in git(
                p, 'for-each-ref', '--format=%(refname:strip=3) %(objectname)', 'refs/remotes/origin/').splitlines()
            if ' ' in line} if tips else {}
        for b in todo:
            local = local_tips.get(b)
            if tips and tips.get(b) and local == tips[b]:
                skipped.append(b)
            else:
                refspecs.append(f'+refs/heads/{b}:refs/remotes/origin/{b}')
                fetched.append(b)
    if refspecs:
        net_git(p, 'fetch', '--no-tags', *(['--prune'] if todo is None else []),
                *(['--filter=blob:none'] if is_partial(p) else []), 'origin', *refspecs,
                url=url, run=run, stage='fetch', repo=repo)
    return {'path': str(p), 'origin': url, 'fetched': fetched if todo is not None else 'all', 'up_to_date': skipped,
            **install_hooks(p)}


def refs(repo, patterns=None, days=None, bases=None, exclude=None):
    raw = git(repo, 'for-each-ref', '--format=%(refname)%09%(objectname)%09%(committerdate:iso-strict)',
              'refs/remotes/origin/')
    cutoff = datetime.now(timezone.utc) - timedelta(days=days) if days is not None else None
    rows = []
    for line in raw.splitlines():
        ref, sha, stamp = line.split('\t')
        branch = ref.removeprefix('refs/remotes/origin/')
        if branch == 'HEAD' or any(fnmatch.fnmatchcase(branch, x) for x in (exclude or [])):
            continue
        is_base = branch in (bases or [])
        if not is_base:
            if patterns and not any(fnmatch.fnmatchcase(branch, x) for x in patterns):
                continue
            if cutoff and datetime.fromisoformat(stamp) < cutoff:
                continue
        rows.append({'branch': branch, 'sha': sha, 'committed_at': stamp, 'base_candidate': is_base})
    return rows


def read_file(repo, ref, path):
    require(not path.startswith('/') and '..' not in Path(path).parts, 'Expected a repo-relative path')
    sha = git(repo, 'rev-parse', '--verify', ref + '^{commit}').strip()
    return {'sha': sha, 'path': path, 'content': git(repo, 'show', f'{sha}:{path}')}


def stale(repo, ref, expected):
    actual = git(repo, 'rev-parse', '--verify', ref + '^{commit}').strip()
    return {'expected': expected, 'actual': actual, 'stale': expected != actual,
            'note': 'Fetch first; this reads the local remote-tracking ref.'}


def check_commits(repo, base, head, key, destination=None):
    base = git(repo, 'rev-parse', '--verify', base + '^{commit}').strip()
    head = git(repo, 'rev-parse', '--verify', head + '^{commit}').strip()
    rows = own_commits(repo, base, head, destination)
    for _, subject in rows:
        check_subject(key, subject)
    merges = len(git(repo, 'rev-list', '--merges', f'{base}..{head}').split())
    return {'valid': True, 'count': len(rows), 'merges_skipped': merges, 'base': base, 'head': head}


def patch_info(worktree, staged=False):
    t = load_target(worktree)
    require(t, 'Not a target worktree (no rollout-target.json)')
    text = index_patch(worktree, t['base_sha']) if staged else worktree_patch(worktree, t['base_sha'])
    return {'target': t['target'], 'base_sha': t['base_sha'], 'patch_sha256': patch_hash(text),
            'staged_only': staged, 'lines': len(text.splitlines())}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    s = ap.add_subparsers(dest='cmd', required=True)
    q = s.add_parser('prepare', help='Blobless clone or refresh (only --branch refs if given); installs rollout hooks')
    q.add_argument('--url', required=True)
    q.add_argument('--path', required=True)
    q.add_argument('--branch', action='append', help='fetch only these branches (repeatable)')
    s.add_parser('ls-remote', help='Branch tips and default branch, without a clone').add_argument('--url', required=True)
    q = s.add_parser('refs', help='List remote branches (base names always included)')
    q.add_argument('--repo', required=True)
    q.add_argument('--pattern', action='append')
    q.add_argument('--base', action='append')
    q.add_argument('--exclude', action='append')
    q.add_argument('--days', type=int)
    q = s.add_parser('read', help='Read a file at a ref without checkout')
    q.add_argument('--repo', required=True)
    q.add_argument('--ref', required=True)
    q.add_argument('--path', required=True)
    q = s.add_parser('stale', help='Compare a fetched ref to an expected SHA')
    q.add_argument('--repo', required=True)
    q.add_argument('--ref', required=True)
    q.add_argument('--expected', required=True)
    q = s.add_parser('check-commits', help='Check every non-merge commit subject in base..head')
    q.add_argument('--repo', required=True)
    q.add_argument('--base', required=True)
    q.add_argument('--head', default='HEAD')
    q.add_argument('--key', required=True)
    q.add_argument('--destination', help='e.g. origin/main: skip commits already on it (merged in)')
    q = s.add_parser('check-subject', help='Check one commit subject')
    q.add_argument('--key', required=True)
    q.add_argument('--subject', required=True)
    s.add_parser('hooks-check', help='Confirm rollout hooks are active in a clone/worktree').add_argument('--repo', required=True)
    q = s.add_parser('patch-hash', help='Hash of the target worktree change vs its base')
    q.add_argument('--repo', required=True)
    q.add_argument('--staged', action='store_true')
    a = ap.parse_args()
    try:
        if a.cmd == 'prepare':
            o = prepare(a.url, a.path, a.branch)
        elif a.cmd == 'ls-remote':
            o = ls_remote(a.url)
        elif a.cmd == 'refs':
            o = refs(a.repo, a.pattern, a.days, a.base, a.exclude)
        elif a.cmd == 'read':
            o = read_file(a.repo, a.ref, a.path)
        elif a.cmd == 'stale':
            o = stale(a.repo, a.ref, a.expected)
        elif a.cmd == 'check-subject':
            check_subject(a.key, a.subject)
            o = {'valid': True}
        elif a.cmd == 'hooks-check':
            o = hooks_check(a.repo)
        elif a.cmd == 'patch-hash':
            o = patch_info(a.repo, a.staged)
        else:
            o = check_commits(a.repo, a.base, a.head, a.key, a.destination)
        print(json.dumps(o, indent=2))
    except (ValueError, OSError) as e:
        ap.exit(2, 'BLOCKED: ' + str(e) + '\n')


if __name__ == '__main__':
    main()
