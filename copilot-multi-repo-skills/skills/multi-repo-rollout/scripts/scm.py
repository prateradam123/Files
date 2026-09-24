#!/usr/bin/env python3
"""Optional speed-up: repo inventory, file reads and PR operations as scripts instead of one agent step each.

No setup. The kind of server and its API address come from the repo's clone URL; the token comes from the
environment (never from files):
  BITBUCKET_TOKEN   Bitbucket Data Center, API at https://<host>/rest/api/1.0
  GITHUB_TOKEN      github.com, or GitHub Enterprise at https://<host>/api/v3 (GH_TOKEN also works)
Unusual setups can override a host under `scm.hosts` in ~/.multi-repo/config.json.

Every request takes a token from the shared per-host bucket (limits.py); content-creating calls are spaced;
a 429/503 pauses that host for every process. Writes are never retried after an unclear failure, and
`open-prs` looks the PR up first, so a rerun can't create a duplicate. Without a token, the skills use
Copilot's own Jira/SCM tools instead, and nothing else depends on this file.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import config, data_home, git, read_json, require, write_json  # noqa: E402
import git_probe  # noqa: E402
import limits  # noqa: E402
import run_state  # noqa: E402

FAILED = {'failure', 'failed', 'error', 'cancelled', 'timed_out', 'action_required', 'startup_failure'}
PENDING = {'pending', 'queued', 'in_progress', 'inprogress', 'waiting', 'requested', 'expected'}
PASSED = {'success', 'successful', 'neutral', 'skipped'}


def ci_state(states):
    states = [str(x).lower() for x in states if x]
    if not states:
        return 'none'
    if any(x in FAILED for x in states):
        return 'failed'
    if any(x in PENDING for x in states):
        return 'pending'
    return 'passed' if all(x in PASSED for x in states) else 'pending'


def load_config(optional=False):
    p = os.environ.get('SCM_CONFIG')
    c = read_json(p) if p and Path(p).is_file() else config().get('scm', {})
    require(optional or c.get('hosts'), 'No SCM host configured')
    return c


def probe(host):
    """Identify an unknown host without sending credentials: Bitbucket Data Center and GitHub Enterprise both
    answer an anonymous request that says what they are. The answer is cached in ~/.multi-repo/cache."""
    cache = data_home() / 'cache' / 'scm-hosts.json'
    known = read_json(cache) if cache.is_file() else {}
    if host in known:
        return known[host]
    scheme = os.environ.get('MR_PROBE_SCHEME', 'https')
    for kind, path, api in (('bitbucket', '/rest/api/1.0/application-properties', '/rest/api/1.0'),
                            ('github', '/api/v3/meta', '/api/v3')):
        try:
            with urllib.request.urlopen(f'{scheme}://{host}{path}', timeout=10) as r:
                body = json.loads(r.read() or b'{}')
        except (OSError, ValueError):
            continue
        if (kind == 'bitbucket' and 'version' in body and 'Bitbucket' in str(body.get('displayName', ''))) or \
                (kind == 'github' and ('installed_version' in body or 'verifiable_password_authentication' in body)):
            known[host] = {'kind': kind, 'api': f'{scheme}://{host}{api}'}
            cache.parent.mkdir(parents=True, exist_ok=True)
            write_json(cache, known)
            return known[host]
    return None


def detect(host):
    """Which server a host is, where its API lives, and which token belongs to it. A token is only ever sent
    to a host that identified itself as that kind of server. `scm.hosts` in the config overrides."""
    c = (load_config(optional=True).get('hosts') or {}).get(host)
    if c:
        return c
    gh_env = 'GITHUB_TOKEN' if os.environ.get('GITHUB_TOKEN') else 'GH_TOKEN'
    if host == 'github.com':
        return {'kind': 'github', 'token_env': gh_env}
    found = probe(host)
    require(found, f"Can't tell what {host} is: no Bitbucket or GitHub API answered at https://{host}. If its web "
                   f"address differs from the clone host, add it once under scm.hosts in ~/.multi-repo/config.json; "
                   f"otherwise use your SCM tool for this step.")
    env = 'BITBUCKET_TOKEN' if found['kind'] == 'bitbucket' else gh_env
    require(os.environ.get(env), f"{host} is {found['kind']}: set {env} to let scripts use its API "
                                 f"(or use your SCM tool for this step)")
    return {**found, 'token_env': env}


def repo_path(url):
    """<project>/<repo> as the API knows it, from a clone URL (ssh, scp-style, https, Bitbucket /scm/)."""
    u = url.strip()
    scp = '://' not in u
    u = re.sub(r'^[a-z+]+://', '', u).split('@', 1)[-1]
    if scp and re.match(r'^[^/:]+:(?!\d+/)', u):
        path = u.split(':', 1)[1]
    else:
        path = u.partition('/')[2]
    path = re.sub(r'^scm/', '', path.strip('/'))
    return re.sub(r'\.git$', '', path)


def address(adapter_obj, url, name):
    """What to call the repo in API requests: the sandbox knows repos by list name; real servers by URL path."""
    return name if isinstance(adapter_obj, Sandbox) else repo_path(url)


def adapter(host):
    c = detect(host)
    return {'github': GitHub, 'bitbucket': Bitbucket, 'sandbox': Sandbox}[c['kind']](host, c)


def for_url(url):
    return adapter(git_probe.host_of(url))


def pr_number(url):
    m = re.search(r'/pull(?:s|-requests)?/(\d+)', url or '')
    require(m, 'Cannot read a PR number from ' + str(url))
    return int(m.group(1))


class Http:
    def __init__(self, host, c):
        self.host, self.c = host, c
        self.api = c['api'].rstrip('/')
        self.token = os.environ.get(c.get('token_env', ''), '')
        require(self.token, f"Set ${c.get('token_env')} in the environment (repo read + PR write). "
                            'Tokens are never stored in files.')

    def req(self, method, path, body=None, accept='application/json', write=False, ok404=False, raw=False):
        url = path if path.startswith('http') else self.api + path
        pool = 'api:' + self.host

        def go():
            limits.throttle(pool, self.c.get('rate'), self.c.get('burst'))
            if write:
                gap = float(self.c.get('write_interval', limits.config()['write_interval']))
                limits.throttle('write:' + self.host, 1 / gap, 1)
            data = json.dumps(body).encode() if body is not None else None
            headers = {'Authorization': 'Bearer ' + self.token, 'Accept': accept, 'User-Agent': 'multi-repo-toolkit'}
            if data:
                headers['Content-Type'] = 'application/json'
            request = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with limits.slot(pool, self.c.get('concurrency')), urllib.request.urlopen(request, timeout=30) as resp:
                    payload = resp.read()
                    return payload.decode() if raw else (json.loads(payload) if payload.strip() else {})
            except urllib.error.HTTPError as e:
                if e.code == 404 and ok404:
                    return None
                limited = e.code in (429, 502, 503, 504) or (
                    e.code == 403 and (e.headers.get('Retry-After') or e.headers.get('X-RateLimit-Remaining') == '0'))
                if limited:
                    wait = float(e.headers.get('Retry-After') or 0)
                    if not wait and e.headers.get('X-RateLimit-Reset'):
                        wait = max(float(e.headers['X-RateLimit-Reset']) - time.time(), 1)
                    limits.hold(pool, min(wait or 5, 900))
                    raise ValueError(f'{method} {url}: HTTP {e.code} Too Many Requests / unavailable; host paused')
                raise ValueError(f'{method} {url}: HTTP {e.code} {e.read()[:300]!r}')
        # A refused request is safe to repeat; a write that timed out might have happened, so don't.
        retryable = (lambda e: 'Too Many Requests' in str(e)) if write else limits.transient
        return limits.retry(go, tries=5, is_transient=retryable)

    def pages(self, path, key=None):
        raise NotImplementedError


class GitHub(Http):
    def __init__(self, host, c):
        # GitHub's guidance is serial REST requests to avoid secondary limits: one at a time by default.
        c = {'api': 'https://api.github.com', 'token_env': 'GITHUB_TOKEN', 'concurrency': 1, **c}
        super().__init__(host, c)

    @staticmethod
    def _pr(d):
        state = 'merged' if d.get('merged_at') else d['state']
        if state == 'open' and d.get('draft'):
            state = 'draft'
        return {'id': d['number'], 'url': d['html_url'], 'state': state, 'head_sha': d['head']['sha']}

    def read_file(self, repo, ref, path):
        return self.req('GET', f'/repos/{repo}/contents/{urllib.parse.quote(path)}?ref={ref}',
                        accept='application/vnd.github.raw+json', ok404=True, raw=True)

    def find_pr(self, repo, source, dest):
        owner = repo.split('/')[0]
        d = self.req('GET', f'/repos/{repo}/pulls?state=open&head={owner}:{urllib.parse.quote(source)}'
                            f'&base={urllib.parse.quote(dest)}')
        return self._pr(d[0]) if d else None

    def create_pr(self, repo, source, dest, title, body, draft):
        return self._pr(self.req('POST', f'/repos/{repo}/pulls', {'title': title, 'head': source, 'base': dest,
                                                                  'body': body, 'draft': draft}, write=True))

    def pr_status(self, repo, number):
        pr = self._pr(self.req('GET', f'/repos/{repo}/pulls/{number}'))
        runs = self.req('GET', f"/repos/{repo}/commits/{pr['head_sha']}/check-runs?per_page=100")
        st = self.req('GET', f"/repos/{repo}/commits/{pr['head_sha']}/status")
        pr['ci'] = ci_state([r.get('conclusion') or r.get('status') for r in runs.get('check_runs', [])]
                            + [x.get('state') for x in st.get('statuses', [])])
        return pr

    def close_pr(self, repo, number, comment):
        self.req('POST', f'/repos/{repo}/issues/{number}/comments', {'body': comment}, write=True)
        self.req('PATCH', f'/repos/{repo}/pulls/{number}', {'state': 'closed'}, write=True)

    def list_repos(self, org):
        out, page = [], 1
        while True:
            d = self.req('GET', f'/orgs/{org}/repos?per_page=100&page={page}')
            if not d:
                return out
            out += [{'name': r['full_name'], 'ssh': r['ssh_url'], 'https': r['clone_url'], 'archived': r['archived'],
                     'fork': r['fork'], 'empty': r.get('size') == 0} for r in d]
            page += 1


class Bitbucket(Http):
    """Bitbucket Data Center / Server REST 1.0."""

    def __init__(self, host, c):
        super().__init__(host, {'token_env': 'BITBUCKET_TOKEN', **c})

    @staticmethod
    def _p(repo):
        project, slug = repo.split('/', 1)
        return f'/projects/{project}/repos/{slug}'

    @staticmethod
    def _pr(d):
        state = {'OPEN': 'open', 'MERGED': 'merged', 'DECLINED': 'closed'}.get(d['state'], d['state'].lower())
        if state == 'open' and d.get('draft'):
            state = 'draft'
        return {'id': d['id'], 'url': d['links']['self'][0]['href'], 'state': state, 'version': d.get('version'),
                'head_sha': d['fromRef'].get('latestCommit')}

    def read_file(self, repo, ref, path):
        return self.req('GET', f'{self._p(repo)}/raw/{urllib.parse.quote(path)}?at={ref}',
                        accept='*/*', ok404=True, raw=True)

    def find_pr(self, repo, source, dest):
        d = self.req('GET', f'{self._p(repo)}/pull-requests?state=OPEN&direction=OUTGOING'
                            f'&at=refs/heads/{urllib.parse.quote(source)}&limit=50')
        hit = [x for x in d.get('values', []) if x['toRef']['displayId'] == dest]
        return self._pr(hit[0]) if hit else None

    def create_pr(self, repo, source, dest, title, body, draft):
        body = {'title': title, 'description': body, 'fromRef': {'id': 'refs/heads/' + source},
                'toRef': {'id': 'refs/heads/' + dest}, **({'draft': True} if draft else {})}
        return self._pr(self.req('POST', f'{self._p(repo)}/pull-requests', body, write=True))

    def pr_status(self, repo, number):
        pr = self._pr(self.req('GET', f'{self._p(repo)}/pull-requests/{number}'))
        base = re.sub(r'/rest/api/(1\.0|latest)$', r'/rest/build-status/\1', self.api)
        builds = self.req('GET', f"{base}/commits/{pr['head_sha']}") if pr['head_sha'] else {}
        pr['ci'] = ci_state([b.get('state') for b in builds.get('values', [])])
        return pr

    def close_pr(self, repo, number, comment):
        self.req('POST', f'{self._p(repo)}/pull-requests/{number}/comments', {'text': comment}, write=True)
        v = self._pr(self.req('GET', f'{self._p(repo)}/pull-requests/{number}'))['version']
        self.req('POST', f'{self._p(repo)}/pull-requests/{number}/decline?version={v}', {}, write=True)

    def list_repos(self, project):
        out, start = [], 0
        while True:
            d = self.req('GET', f'/projects/{project}/repos?limit=100&start={start}')
            for r in d.get('values', []):
                links = {x['name']: x['href'] for x in r.get('links', {}).get('clone', [])}
                out.append({'name': f"{project}/{r['slug']}", 'ssh': links.get('ssh'), 'https': links.get('http'),
                            'archived': r.get('archived', False), 'fork': 'origin' in r, 'empty': False})
            if d.get('isLastPage', True):
                return out
            start = d['nextPageStart']


class Sandbox:
    """The file-based stand-in from scripts/sandbox.py, behind the same interface (and the same limits)."""

    def __init__(self, host, c):
        import sandbox
        self.sb, self.store, self.host = sandbox, c['store'], host

    def _t(self):
        limits.throttle('api:' + self.host, 50, 50)

    def read_file(self, repo, ref, path):
        self._t()
        p = subprocess.run(['git', '--git-dir', self.sb.remote_for(self.store, repo).removeprefix('file://'),
                            'show', f'{ref}:{path}'], capture_output=True, text=True)
        return p.stdout if p.returncode == 0 else None

    @staticmethod
    def _pr(p):
        return {'id': p['id'], 'url': p['url'], 'state': 'draft' if p['state'] == 'open' and p.get('draft')
                else p['state'], 'head_sha': p.get('head_sha')}

    def find_pr(self, repo, source, dest):
        self._t()
        hit = [p for p in read_json(Path(self.store) / 'prs.json')
               if (p['repo'], p['source'], p['dest'], p['state']) == (repo, source, dest, 'open')]
        return self._pr(hit[0]) if hit else None

    def create_pr(self, repo, source, dest, title, body, draft):
        self._t()
        limits.throttle('write:' + self.host, 20, 1)
        return self._pr(self.sb.pr_create(self.store, repo, source, dest, title, body, draft))

    def pr_status(self, repo, number):
        self._t()
        _, p = self.sb.find(self.store, number)
        return {**self._pr(p), 'ci': {'passed': 'passed', 'failed': 'failed'}.get(p['checks']['state'], 'pending')}

    def close_pr(self, repo, number, comment):
        self._t()
        self.sb.pr_close(self.store, number, comment)

    def list_repos(self, project):
        rows = [line.split() for line in (Path(self.store).parent / 'repos.txt').read_text().splitlines() if line]
        return [{'name': n, 'ssh': u, 'https': u, 'archived': False, 'fork': False, 'empty': False}
                for n, u in rows if n.startswith(project + '/') or project == '*']


# ------------------------------------------------------------------ run-level commands

def _scope_url(s, repo):
    return next(r['url'] for r in s['scope']['repos'] if r['name'] == repo)


def pr_body(s, t):
    files = ', '.join(f'`{f}`' for f in t.get('files') or []) or 'see diff'
    n = sum(x['disposition'] == 'needs_change' for x in s['plan']['targets'])
    return (f"## What\n{s['recipe']['title']}.\n\n## Why\n{t['jira_key']}: {s['jira']['title']} ({s['jira']['url']})\n\n"
            f"## Change\nFiles: {files}. The same change is going to {n} repositories; this diff matches the "
            f"reviewed preview exactly.\n\n## Verified\n- Recipe check on this branch: compliant\n"
            f"- {t.get('validation', '')}\n\n<sub>Opened by the multi-repo toolkit, run `{s['run_id']}`, "
            f"recipe v{s['recipe']['versions'][-1]['version']}.</sub>\n")


def open_prs(run, targets=None):
    """For pushed targets: find the PR (follow-up push) or open it (gated, spaced), and write results."""
    import recipe_run
    run = str(Path(run).resolve())
    s = run_state.load(run)
    todo = targets or [tid for tid, r in s['results'].items() if r.get('status') == 'pushed']
    out = []
    for tid in todo:
        t = run_state.target_for(s, tid)
        try:
            url = _scope_url(s, t['repo'])
            adapter = for_url(url)
            rp = address(adapter, url, t['repo'])
            with limits.timed(run, 'pr-find', repo=t['repo']):
                pr = adapter.find_pr(rp, t['source_branch'], t['branch'])
            if pr:
                recipe_run.write_result(run, tid, 'pr_updated', f"pushed to existing PR {pr['url']}",
                                        pr_url=pr['url'], pr_state=pr['state'], ci='pending')
                out.append({'target': tid, 'status': 'pr_updated', 'pr_url': pr['url']})
                continue
            current = git(t['worktree'], 'rev-parse', f"origin/{t['branch']}").strip()
            gate = run_state.check_gate(run, tid, 'create_pr', current)
            with limits.timed(run, 'pr-create', repo=t['repo']):
                pr = adapter.create_pr(rp, t['source_branch'], t['branch'],
                                       f"{t['jira_key']} {s['recipe']['title']}", pr_body(s, t),
                                       gate.get('pr_mode') == 'draft')
            recipe_run.write_result(run, tid, 'pr_opened', f"mr open-prs created {pr['url']}",
                                    pr_url=pr['url'], pr_state=pr['state'], ci='pending')
            out.append({'target': tid, 'status': 'pr_opened', 'pr_url': pr['url']})
        except ValueError as e:
            out.append({'target': tid, 'status': 'error', 'reason': str(e)[:400]})
    return out


def pr_status(run):
    """Observe every open PR of the run in one pass; writes `observed` (or `merged`) results to ingest."""
    import recipe_run
    run = str(Path(run).resolve())
    s = run_state.load(run)
    out = []
    for tid, r in s['results'].items():
        if not r.get('pr_url') or r.get('pr_state') in {'merged', 'closed'} or r.get('status') == 'closed':
            continue
        t = run_state.target_for(s, tid)
        try:
            with limits.timed(run, 'pr-status', repo=t['repo']):
                url = _scope_url(s, t['repo'])
                a = for_url(url)
                p = a.pr_status(address(a, url, t['repo']), pr_number(r['pr_url']))
            status = 'merged' if p['state'] == 'merged' else 'observed'
            before = (r.get('pr_state'), (r.get('ci') or {}).get('state'))
            recipe_run.write_result(run, tid, status, f"mr pr-status: {p['state']}, CI {p['ci']}",
                                    pr_url=r['pr_url'], pr_state=p['state'], ci=p['ci'])
            out.append({'target': tid, 'pr_state': p['state'], 'ci': p['ci'], 'changed': before != (p['state'], p['ci'])})
        except ValueError as e:
            out.append({'target': tid, 'error': str(e)[:300]})
    return out


def close_prs(run, targets, comment):
    import recipe_run
    run = str(Path(run).resolve())
    s = run_state.load(run)
    out = []
    for tid in targets:
        t = run_state.target_for(s, tid)
        r = s['results'].get(tid, {})
        try:
            run_state.check_gate(run, tid, 'close_pr')
            if r.get('pr_url'):
                url = _scope_url(s, t['repo'])
                a = for_url(url)
                a.close_pr(address(a, url, t['repo']), pr_number(r['pr_url']), comment)
            if git(t['worktree'], 'ls-remote', 'origin', 'refs/heads/' + t['source_branch']).strip():
                git_probe.net_git(t['worktree'], 'push', '-q', 'origin', '--delete', t['source_branch'],
                                  url=_scope_url(s, t['repo']), run=run, stage='push', repo=t['repo'])
            recipe_run.write_result(run, tid, 'closed', f'closed PR and deleted branch: {comment}',
                                    pr_url=r.get('pr_url'), pr_state='closed' if r.get('pr_url') else None)
            out.append({'target': tid, 'status': 'closed'})
        except ValueError as e:
            out.append({'target': tid, 'status': 'error', 'reason': str(e)[:300]})
    return out


def list_repos(host, project, https=False, archived=False, forks=False, out_file=None):
    rows, skipped = [], []
    for r in adapter(host).list_repos(project):
        why = ('archived' if r['archived'] and not archived else 'fork' if r['fork'] and not forks
               else 'empty' if r['empty'] else None)
        (skipped.append({'repo': r['name'], 'reason': why}) if why else
         rows.append(f"{r['name']} {r['https'] if https else r['ssh'] or r['https']}"))
    if out_file:
        Path(out_file).parent.mkdir(parents=True, exist_ok=True)
        Path(out_file).write_text('\n'.join(rows) + '\n')
    return {'repos': len(rows), 'skipped': skipped, 'file': out_file}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = ap.add_subparsers(dest='cmd', required=True)
    q = s.add_parser('list-repos', help='Inventory a project/org; skips archived, forks, empty (writes <name> <url>)')
    q.add_argument('--host', required=True)
    q.add_argument('--project', required=True)
    q.add_argument('--out')
    q.add_argument('--https', action='store_true', help='HTTPS clone URLs (default SSH: not counted by Bitbucket rate limits)')
    q.add_argument('--include-archived', action='store_true')
    q.add_argument('--include-forks', action='store_true')
    q = s.add_parser('read', help='Read one file at a ref through the API')
    q.add_argument('--url', required=True)
    q.add_argument('--repo', required=True)
    q.add_argument('--ref', required=True)
    q.add_argument('--path', required=True)
    q = s.add_parser('open-prs', help='Open or update PRs for pushed targets (gated, rate limited)')
    q.add_argument('--run', required=True)
    q.add_argument('--target', nargs='*')
    s.add_parser('pr-status', help="Record state and CI of every open PR in the run").add_argument('--run', required=True)
    q = s.add_parser('close-prs', help='Close PRs and delete branches of aborted targets')
    q.add_argument('--run', required=True)
    q.add_argument('--target', nargs='+', required=True)
    q.add_argument('--comment', required=True)
    a = ap.parse_args()
    try:
        if a.cmd == 'list-repos':
            o = list_repos(a.host, a.project, a.https, a.include_archived, a.include_forks, a.out)
        elif a.cmd == 'read':
            ad = for_url(a.url)
            o = {'content': ad.read_file(address(ad, a.url, a.repo), a.ref, a.path)}
        elif a.cmd == 'open-prs':
            o = open_prs(a.run, a.target)
        elif a.cmd == 'pr-status':
            rows = pr_status(a.run)
            count = {}
            for x in rows:
                k = x.get('error') and 'error' or f"{x['pr_state']}/CI {x['ci']}"
                count[k] = count.get(k, 0) + 1
            o = {'prs': count, 'changed': [x for x in rows if x.get('changed')][:30],
                 'errors': [x for x in rows if x.get('error')][:10]}
        else:
            o = close_prs(a.run, a.target, a.comment)
        failed = a.cmd in ('open-prs', 'close-prs') and any(x.get('status') == 'error' for x in o)
        if a.cmd in ('open-prs', 'close-prs') and len(o) > 20:
            counts = {}
            for x in o:
                counts[x['status']] = counts.get(x['status'], 0) + 1
            o = {'targets': len(o), 'counts': counts, 'errors': [x for x in o if x['status'] == 'error'][:20]}
        print(json.dumps(o, indent=2))
        if failed:
            sys.exit(1)  # pushed branches without a PR must not go unnoticed
    except (ValueError, OSError, KeyError) as e:
        ap.exit(2, 'BLOCKED: ' + str(e) + '\n')


if __name__ == '__main__':
    main()
