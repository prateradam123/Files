"""Performance world: many repos, some large (thousands of files), some with many feature branches."""
import json, os, subprocess, sys, time
from pathlib import Path
GE = dict(os.environ, GIT_AUTHOR_NAME='Dev', GIT_AUTHOR_EMAIL='d@x', GIT_COMMITTER_NAME='Dev', GIT_COMMITTER_EMAIL='d@x')
def run(a, cwd=None, env=GE):
    p = subprocess.run(a, cwd=cwd, capture_output=True, text=True, env=env)
    if p.returncode: raise RuntimeError(f'{a}: {p.stderr}')
    return p.stdout
def make(out, n=150, big=10, big_files=3000, branchy=8, branches=25):
    out = Path(out).resolve(); (out / 'remotes').mkdir(parents=True); (out / 'scm').mkdir()
    lines = []
    old = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(time.time() - 200 * 86400))
    for i in range(n):
        name = f"P{i % 3}/svc-{i:03d}"
        bare = out / 'remotes' / (name.replace('/', '__') + '.git')
        run(['git', 'init', '-q', '--bare', '-b', 'main', str(bare)])
        for k in ('allowFilter', 'allowAnySHA1InWant'): run(['git', '-C', str(bare), 'config', f'uploadpack.{k}', 'true'])
        w = out / 'work' / name.replace('/', '__'); w.mkdir(parents=True)
        run(['git', 'init', '-q', '-b', 'main'], w)
        flag = 'false' if i % 6 == 0 else 'true'
        (w / 'config.json').write_text('{\n  "name": "%s",\n  "featureBuilds": %s\n}\n' % (name, flag))
        (w / 'build.sh').write_text('exit 0\n'); (w / 'build.sh').chmod(0o755)
        if i < big:  # large repo: many files and a few MB
            src = w / 'src'
            for d in range(big_files // 100):
                dd = src / f'm{d:03d}'; dd.mkdir(parents=True)
                for f in range(100):
                    (dd / f'F{f:03d}.java').write_text(f'class F{d}_{f} {{ /* {"x" * 400} */ }}\n')
        run(['git', 'add', '-A'], w); run(['git', 'commit', '-qm', 'init'], w)
        run(['git', 'remote', 'add', 'origin', f'file://{bare}'], w); run(['git', 'push', '-q', 'origin', 'main'], w)
        if big <= i < big + branchy:  # many feature branches, half stale
            for b in range(branches):
                env = dict(GE, **({'GIT_COMMITTER_DATE': old, 'GIT_AUTHOR_DATE': old} if b % 2 else {}))
                run(['git', 'checkout', '-qb', f'feature/f{b:02d}', 'main'], w)
                (w / f'f{b}.txt').write_text(str(b)); run(['git', 'add', '-A'], w, env); run(['git', 'commit', '-qm', f'f{b}'], w, env)
            run(['git', 'push', '-q', 'origin', '--all'], w); run(['git', 'checkout', '-q', 'main'], w)
        lines.append(f'{name} file://{bare}')
    (out / 'repos.txt').write_text('\n'.join(lines) + '\n'); (out / 'scm' / 'prs.json').write_text('[]')
    home = out / 'home'; home.mkdir()
    (home / 'config.json').write_text(json.dumps({'limits': {'partial_clone_hosts': ['local']},
                                                  'scm': {'hosts': {'local': {'kind': 'sandbox', 'store': str(out / 'scm')}}}}))
if __name__ == '__main__':
    make(sys.argv[1])
