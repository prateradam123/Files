"""Performance benchmark: 150 repos (10 large, 8 with 25 branches), every phase timed, Git processes counted.

    python3 tests/simulation/perf.py /tmp/mr-perf
"""
import collections, json, os, shutil, subprocess, sys, tempfile, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import perfworld
HUB = Path(os.environ.get('HUB') or HERE.parents[1])
MR = [sys.executable, str(HUB / 'scripts' / 'mr.py')]
w = Path(sys.argv[1]).resolve(); shutil.rmtree(w, ignore_errors=True); perfworld.make(w)
SHIM = Path(tempfile.mkdtemp()) / 'bin'; SHIM.mkdir()
(SHIM / 'git').write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "${GIT_CALL_LOG:-/dev/null}"\nexec ' + shutil.which('git') + ' "$@"\n'); (SHIM / 'git').chmod(0o755)
LOG = w / 'git-calls.log'
env = dict(os.environ, PATH=str(SHIM) + ':' + os.environ['PATH'], GIT_CALL_LOG=str(LOG), MULTI_REPO_HOME=str(w / 'home'),
           ROLLOUT_LOCK_DIR=str(w / 'home' / 'locks'), GIT_AUTHOR_NAME='B', GIT_AUTHOR_EMAIL='b@x', GIT_COMMITTER_NAME='B', GIT_COMMITTER_EMAIL='b@x')
M = {}
def git_calls():
    if not LOG.exists(): return collections.Counter()
    c = collections.Counter()
    for line in LOG.read_text().splitlines():
        a = line.split()
        while a and (a[0].startswith('-') or (len(a) > 1 and a[0] == '-C')):
            a = a[2:] if a[0] in ('-C', '-c') else a[1:]
        c[a[0] if a else '?'] += 1
    return c
def phase(name, *args, parse=True):
    before = git_calls(); t = time.time()
    p = subprocess.run(MR + [str(x) for x in args], capture_output=True, text=True, env=env, cwd='/tmp')
    dt = time.time() - t; after = git_calls()
    diff = {k: after[k] - before[k] for k in after if after[k] - before[k]}
    M[name] = {'secs': round(dt, 2), 'git_calls': sum(diff.values()), 'top_git': dict(sorted(diff.items(), key=lambda kv: -kv[1])[:6]),
               'rc': p.returncode, 'out_bytes': len(p.stdout)}
    if p.returncode not in (0, 1): print(name, p.stderr[-800:], file=sys.stderr)
    return json.loads(p.stdout) if parse and p.stdout.strip().startswith(('{', '[')) else p.stdout
chk = w / 'flag.py'; chk.write_text("import json\nfrom pathlib import Path\nv=json.loads(Path('config.json').read_text()).get('featureBuilds')\nprint(json.dumps({'status':'compliant' if v is False else 'needs_change','value':str(v),'evidence':'config.json'}))\n")
phase('scan_cold', 'scan', '--check', chk, '--paths', 'config.json', '--repos', w / 'repos.txt', '--days', '60', '--name', 'a')
phase('scan_warm', 'scan', '--check', chk, '--paths', 'config.json', '--repos', w / 'repos.txt', '--days', '60', '--name', 'b')
run = 'PERF-1'
phase('init', 'init', '--id', run, '--recipe', 'disable-feature-builds', '--jira', 'PERF-1', '--jira-title', 't', '--jira-url', 'u', '--jira-evidence', 'e', '--repos', w / 'repos.txt', '--request', 'x')
phase('discover', 'discover', '--run', run, '--pending', '--workers', '8', '--days', '60')
p = phase('plan', 'plan', '--run', run, '--strategy', 's')
ids = p['needs_approval']
M['targets'] = p.get('needs_approval_count', len(ids))
phase('validate_dry', 'deliver', '--run', run, '--dry', '--all', '--workers', '3')
phase('approve', 'approve', '--run', run, '--evidence', 'go')
phase('take_peek', 'take', '--run', run, '--task', 'deliver', '--limit', '500')
phase('deliver', 'deliver', '--run', run, '--ready', '--workers', '3')
phase('open_prs', 'open-prs', '--run', run)
phase('report', 'report', '--run', run)
phase('take_after', 'take', '--run', run, '--limit', '500')
man = w / 'home' / 'runs' / run / 'manifest.json'
d = json.loads(man.read_text())
M['ledger'] = {'kb': round(man.stat().st_size / 1024, 1), 'events': len(d['events']), 'revision': d['revision'],
               'events_kb': round(len(json.dumps(d['events'])) / 1024, 1)}
tl = [json.loads(x) for x in (w / 'home' / 'runs' / run / 'timings.jsonl').read_text().splitlines()]
st = collections.defaultdict(lambda: [0, 0.0, 0.0])
for r in tl:
    st[r['stage']][0] += 1; st[r['stage']][1] += r['seconds']; st[r['stage']][2] += r.get('wait', 0)
M['stages'] = {k: {'n': v[0], 'work_s': round(v[1], 1), 'wait_s': round(v[2], 1)} for k, v in sorted(st.items(), key=lambda kv: -kv[1][1])}
M['disk_mb'] = round(sum(f.stat().st_size for f in (w / 'home' / 'clones').rglob('*') if f.is_file()) / 1e6, 1)
print(json.dumps(M, indent=1))
