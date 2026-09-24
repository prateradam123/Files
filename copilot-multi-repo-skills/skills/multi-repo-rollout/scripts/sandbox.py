#!/usr/bin/env python3
"""Local sandbox: fake repos, a fake Jira card, and a fake PR server, all on disk.

`make` builds repos that cover the cases that break rollouts (feature branch needs the change while
main is compliant, a different config format, a missing setting, an ambiguous file, a build that was
already broken, an already-done repo, an unreachable repo). The other commands stand in for your SCM
and Jira skills so the whole flow runs end to end without touching real systems. See docs/EVALS.md.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import now, read_json, require, write_json  # noqa: E402

BUILD_JSON = '#!/bin/sh\npython3 -c "import json; json.load(open(\'config.json\'))" && echo "build ok"\n'
REPOS = {
    'demo/direct': {'main': {'config.json': '{\n  "name": "direct",\n  "featureBuilds": true\n}\n',
                             'build.sh': BUILD_JSON}},
    'demo/feature-branch': {'main': {'config.json': '{\n  "featureBuilds": false\n}\n', 'build.sh': BUILD_JSON},
                            'feature/checkout': {'config.json': '{\n  "featureBuilds": true,\n  "x": 1\n}\n',
                                                 'build.sh': BUILD_JSON}},
    'demo/properties': {'main': {'build.properties': 'app=props\nfeatureBuilds=true\n',
                                 'build.sh': '#!/bin/sh\ngrep -q "^featureBuilds=" build.properties && echo "build ok"\n'}},
    'demo/missing': {'main': {'README.md': '# No build config here\n', 'build.sh': '#!/bin/sh\necho "build ok"\n'}},
    'demo/duplicate': {'main': {'config.json': '{\n  "featureBuilds": true,\n  "featureBuilds": false\n}\n',
                                'build.sh': BUILD_JSON}},
    'demo/broken-build': {'main': {'config.json': '{\n  "featureBuilds": true\n}\n',
                                   'build.sh': '#!/bin/sh\necho "test FooTest failed" >&2\nexit 1\n'}},
    'demo/done': {'main': {'config.json': '{\n  "featureBuilds": false\n}\n', 'build.sh': BUILD_JSON}},
}


def run(cmd, cwd=None, check=True):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode:
        raise ValueError(f"{' '.join(cmd)}: {p.stderr.strip()}")
    return p.stdout


def make(out):
    out = Path(out).resolve()
    require(not out.exists() or not any(out.iterdir()), f'{out} must be empty')
    (out / 'remotes').mkdir(parents=True)
    ident = ['-c', 'user.name=Sandbox', '-c', 'user.email=sandbox@example.invalid', '-c', 'commit.gpgsign=false']
    lines = []
    for name, branches in REPOS.items():
        bare = out / 'remotes' / (name.replace('/', '__') + '.git')
        run(['git', 'init', '--bare', '-b', 'main', str(bare)])
        # Let clients use blobless partial clones against the sandbox, like a modern server.
        run(['git', '-C', str(bare), 'config', 'uploadpack.allowFilter', 'true'])
        run(['git', '-C', str(bare), 'config', 'uploadpack.allowAnySHA1InWant', 'true'])
        with tempfile.TemporaryDirectory() as tmp:
            run(['git', 'init', '-b', 'main', tmp])
            for i, (branch, files) in enumerate(branches.items()):
                if i:
                    run(['git', 'checkout', '-b', branch, 'main'], tmp)
                for f in list(Path(tmp).iterdir()):
                    if f.name != '.git':
                        f.unlink()
                for fname, content in files.items():
                    (Path(tmp) / fname).write_text(content)
                run(['git', 'add', '-A'], tmp)
                run(['git', *ident, 'commit', '-m', f'Initial {branch}'], tmp)
            run(['git', 'push', '--all', str(bare)], tmp)
        lines.append(f'{name} file://{bare}')
    lines.append(f"demo/unreachable file://{out / 'remotes' / 'does-not-exist.git'}")
    (out / 'repos.txt').write_text('\n'.join(lines) + '\n')
    names = [l.split()[0] for l in lines]
    write_json(out / 'jira' / 'ENG-900.json', {
        'key': 'ENG-900', 'title': 'Disable feature builds in demo services',
        'url': 'sandbox://jira/ENG-900', 'project': 'ENG',
        'description': 'Feature builds waste CI minutes. Disable them in every listed repo, on main and on '
                       'active feature branches.\n\nRepos:\n' + '\n'.join('- ' + n for n in names),
        'acceptance': ['featureBuilds is false (or absent-by-design) on main and active feature/* branches',
                       'No other config changes', 'Build passes'],
        'comments': []})
    write_json(out / 'scm' / 'prs.json', [])
    write_json(out / 'scm.json', {'hosts': {'local': {'kind': 'sandbox', 'store': str(out / 'scm')}}})
    # A ready-made home for the sandbox: point MULTI_REPO_HOME at it and every command uses the fakes.
    write_json(out / 'home' / 'config.json', {
        'limits': {'partial_clone_hosts': ['local']},
        'scm': {'hosts': {'local': {'kind': 'sandbox', 'store': str(out / 'scm')}}}})
    return {'sandbox': str(out), 'repos_file': str(out / 'repos.txt'), 'jira_card': 'ENG-900',
            'scm_config': str(out / 'scm.json'),
            'home': str(out / 'home'), 'next': f'export MULTI_REPO_HOME={out / "home"}  (see the README: Sandbox)'}


def store(path):
    return Path(path) / 'prs.json'


def remote_for(out_store, repo):
    for line in (Path(out_store).parent / 'repos.txt').read_text().splitlines():
        n, url = line.split()
        if n == repo:
            return url
    raise ValueError('Unknown sandbox repo ' + repo)


def pr_create(st, repo, source, dest, title, body, draft):
    prs = read_json(store(st))
    for p in prs:
        if p['repo'] == repo and p['source'] == source and p['dest'] == dest and p['state'] == 'open':
            return {**p, 'existing': True}
    url = remote_for(st, repo)
    head = run(['git', 'ls-remote', url, 'refs/heads/' + source]).split()
    require(head, f'Source branch {source} is not on the remote; push first')
    n = len(prs) + 1
    p = {'id': n, 'url': f'sandbox://{repo}/pull/{n}', 'repo': repo, 'source': source, 'dest': dest,
         'title': title, 'body': body, 'draft': draft, 'state': 'open', 'head_sha': head[0],
         'checks': {'state': 'pending'}, 'created_at': now()}
    prs.append(p)
    write_json(store(st), prs)
    return p


def find(st, pid):
    prs = read_json(store(st))
    p = next((p for p in prs if p['id'] == pid), None)
    require(p, f'No PR {pid}')
    return prs, p


def ci(st, pid):
    prs, p = find(st, pid)
    url = remote_for(st, p['repo'])
    with tempfile.TemporaryDirectory() as tmp:
        run(['git', 'clone', '-q', '-b', p['source'], url, tmp])
        p['head_sha'] = run(['git', 'rev-parse', 'HEAD'], tmp).strip()
        # Like real CI: run the command the repo's Jenkinsfile runs, if it has one.
        jf = Path(tmp) / 'Jenkinsfile'
        m = re.search(r"sh\s+'([^']+)'", jf.read_text()) if jf.is_file() else None
        cmd = m.group(1).replace('./build.sh', 'sh build.sh') if m else 'sh build.sh'
        r = subprocess.run(cmd, shell=True, cwd=tmp, capture_output=True, text=True)
    p['checks'] = {'state': 'passed' if r.returncode == 0 else 'failed', 'sha': p['head_sha'],
                   'log': (r.stdout + r.stderr)[-400:], 'at': now()}
    write_json(store(st), prs)
    return p


def pr_close(st, pid, comment):
    prs, p = find(st, pid)
    p.update(state='closed', close_comment=comment, closed_at=now())
    write_json(store(st), prs)
    return p


def pr_merge(st, pid):
    prs, p = find(st, pid)
    require(p['state'] == 'open', 'PR not open')
    url = remote_for(st, p['repo'])
    ident = ['-c', 'user.name=Sandbox', '-c', 'user.email=sandbox@example.invalid', '-c', 'commit.gpgsign=false',
             '-c', 'core.hooksPath=/dev/null']
    with tempfile.TemporaryDirectory() as tmp:
        run(['git', 'clone', '-q', '-b', p['dest'], url, tmp])
        run(['git', 'fetch', '-q', 'origin', p['source']], tmp)
        run(['git', *ident, 'merge', '--no-ff', '-m', f"Merge PR {pid}", 'FETCH_HEAD'], tmp)
        run(['git', *ident, 'push', '-q', 'origin', p['dest']], tmp)
    p.update(state='merged', merged_at=now())
    write_json(store(st), prs)
    return p


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = ap.add_subparsers(dest='cmd', required=True)
    s.add_parser('make', help='Build a sandbox in an empty directory').add_argument('--out', required=True)
    q = s.add_parser('pr-create')
    for x in ('store', 'repo', 'source', 'dest', 'title'):
        q.add_argument('--' + x, required=True)
    q.add_argument('--body-file')
    q.add_argument('--draft', action='store_true')
    s.add_parser('repo-list', help='Repos with clone URLs (stands in for SCM inventory)').add_argument('--store', required=True)
    q = s.add_parser('pr-list')
    q.add_argument('--store', required=True)
    q.add_argument('--repo')
    q.add_argument('--source')
    for n in ('pr-close', 'ci', 'pr-merge'):
        q = s.add_parser(n)
        q.add_argument('--store', required=True)
        q.add_argument('--id', type=int, required=True)
        if n == 'pr-close':
            q.add_argument('--comment', required=True)
    q = s.add_parser('jira-get')
    q.add_argument('--dir', required=True)
    q.add_argument('--key', required=True)
    q = s.add_parser('jira-comment')
    q.add_argument('--dir', required=True)
    q.add_argument('--key', required=True)
    q.add_argument('--file', required=True)
    a = ap.parse_args()
    try:
        if a.cmd == 'make':
            o = make(a.out)
        elif a.cmd == 'pr-create':
            body = Path(a.body_file).read_text() if a.body_file else ''
            o = pr_create(a.store, a.repo, a.source, a.dest, a.title, body, a.draft)
        elif a.cmd == 'repo-list':
            o = [dict(zip(('name', 'url'), line.split())) for line in
                 (Path(a.store).parent / 'repos.txt').read_text().splitlines() if line.strip()]
        elif a.cmd == 'pr-list':
            o = [p for p in read_json(store(a.store)) if (not a.repo or p['repo'] == a.repo)
                 and (not a.source or p['source'] == a.source)]
        elif a.cmd == 'pr-close':
            o = pr_close(a.store, a.id, a.comment)
        elif a.cmd == 'ci':
            o = ci(a.store, a.id)
        elif a.cmd == 'pr-merge':
            o = pr_merge(a.store, a.id)
        elif a.cmd == 'jira-get':
            p = Path(a.dir) / 'jira' / (a.key + '.json')
            require(p.exists(), 'Issue does not exist: ' + a.key)
            o = read_json(p)
        else:
            p = Path(a.dir) / 'jira' / (a.key + '.json')
            card = read_json(p)
            card['comments'].append({'at': now(), 'body': Path(a.file).read_text()})
            write_json(p, card)
            o = {'key': a.key, 'comments': len(card['comments'])}
        print(json.dumps(o, indent=2))
    except (ValueError, OSError) as e:
        ap.exit(2, 'BLOCKED: ' + str(e) + '\n')


if __name__ == '__main__':
    main()
