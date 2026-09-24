"""A realistic synthetic organization: 40 repos in two Bitbucket-like projects, with the messiness of real repos."""
import json, os, subprocess, sys, time
from pathlib import Path

def run(args, cwd=None, env=None):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=env)
    if p.returncode: raise RuntimeError(f'{args}: {p.stderr}')
    return p.stdout

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME='Dev', GIT_AUTHOR_EMAIL='dev@x', GIT_COMMITTER_NAME='Dev', GIT_COMMITTER_EMAIL='dev@x')
BRANCH_POLICY = '''#!/bin/sh
# Bitbucket-style branch permissions: only feature/ and bugfix/ may be created by developers
while read old new ref; do
  case "$ref" in refs/heads/main|refs/heads/develop|refs/heads/feature/*|refs/heads/bugfix/*) ;;
    *) echo "Branch name $ref does not match the branching model (feature/*, bugfix/*)" >&2; exit 1;;
  esac
done
'''

def variant(i):
    v = {'project': 'ABC' if i < 25 else 'DEF', 'base': 'develop' if i % 3 == 0 else 'main'}
    v['config'] = ('missing' if i % 11 == 0 else 'properties' if i % 7 == 0 else 'compliant' if i % 5 == 0 else 'json')
    v['build'] = ('broken' if i % 9 == 0 else 'profile' if i % 8 == 0 else 'husky' if i % 13 == 0 else
                  'slow' if i % 10 == 0 else 'ok')
    v['features'] = i % 4 == 0
    return v

BUILDS = {
    'ok': 'echo building; exit 0\n',
    'broken': 'echo "COMPILATION ERROR: Foo.java:12"; exit 1\n',
    'profile': '[ "$1" = "--profile" ] && [ "$2" = "ci" ] && { echo ok; exit 0; }\necho "Missing -Pci: cannot resolve internal artifacts"; exit 1\n',
    'husky': 'git config core.hooksPath .husky 2>/dev/null; echo "husky - Git hooks installed"; exit 0\n',
    'slow': 'sleep 1; echo slow ok; exit 0\n',
}

def make(out, n=40):
    out = Path(out).resolve(); (out / 'remotes').mkdir(parents=True); (out / 'scm').mkdir()
    lines, facts = [], {}
    old = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(time.time() - 120 * 86400))
    for i in range(n):
        v = variant(i); name = f"{v['project']}/svc-{i:02d}"
        bare = out / 'remotes' / (name.replace('/', '__') + '.git')
        run(['git', 'init', '-q', '--bare', '-b', 'main', str(bare)])
        run(['git', '-C', str(bare), 'config', 'uploadpack.allowFilter', 'true'])
        run(['git', '-C', str(bare), 'config', 'uploadpack.allowAnySHA1InWant', 'true'])
        if v['project'] == 'DEF':
            h = bare / 'hooks' / 'pre-receive'; h.write_text(BRANCH_POLICY); h.chmod(0o755)
        w = out / 'work' / name.replace('/', '__'); w.mkdir(parents=True)
        run(['git', 'init', '-q', '-b', 'main'], w)
        (w / 'build.sh').write_text(BUILDS[v['build']]); (w / 'build.sh').chmod(0o755)
        if v['build'] == 'profile':
            (w / 'Jenkinsfile').write_text("pipeline { stages { stage('build') { steps { sh './build.sh --profile ci' } } } }\n")
        if v['config'] == 'json':
            (w / 'config.json').write_text('{\n  "name": "%s",\n  "featureBuilds": true\n}\n' % name)
        elif v['config'] == 'compliant':
            (w / 'config.json').write_text('{\n  "name": "%s",\n  "featureBuilds": false\n}\n' % name)
        elif v['config'] == 'properties':
            (w / 'build.properties').write_text('app=%s\nfeatureBuilds=true\n' % name)
        (w / 'README.md').write_text(f'# {name}\n')
        run(['git', 'add', '-A'], w, GIT_ENV); run(['git', 'commit', '-qm', 'init'], w, GIT_ENV)
        run(['git', 'remote', 'add', 'origin', f'file://{bare}'], w)
        run(['git', 'push', '-q', 'origin', 'main'], w)
        if v['base'] == 'develop':
            run(['git', 'checkout', '-qb', 'develop'], w); (w / 'DEV.md').write_text('develop\n')
            run(['git', 'add', '-A'], w, GIT_ENV); run(['git', 'commit', '-qm', 'develop work'], w, GIT_ENV)
            run(['git', 'push', '-q', 'origin', 'develop'], w)
        if v['features']:
            for fb, date in (('feature/active', None), ('feature/stale', old)):
                run(['git', 'checkout', '-qb', fb, v['base']], w); (w / f'{fb.split("/")[1]}.txt').write_text(fb)
                env = dict(GIT_ENV, **({'GIT_COMMITTER_DATE': date, 'GIT_AUTHOR_DATE': date} if date else {}))
                run(['git', 'add', '-A'], w, env); run(['git', 'commit', '-qm', fb], w, env)
                run(['git', 'push', '-q', 'origin', fb], w)
            run(['git', 'checkout', '-q', v['base']], w)
        lines.append(f'{name} file://{bare}')
        facts[name] = v
    lines.append(f'DEF/svc-gone file://{out}/remotes/nope.git')
    (out / 'repos.txt').write_text('\n'.join(lines) + '\n')
    (out / 'variants.json').write_text(json.dumps(facts, indent=1))
    (out / 'scm' / 'prs.json').write_text('[]')
    home = out / 'home'; home.mkdir()
    (home / 'config.json').write_text(json.dumps({'limits': {'partial_clone_hosts': ['local']},
        'scm': {'hosts': {'local': {'kind': 'sandbox', 'store': str(out / 'scm')}}}}))
    return out

if __name__ == '__main__':
    make(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 40)
