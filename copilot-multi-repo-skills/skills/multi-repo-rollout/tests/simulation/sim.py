"""Realistic simulation runs: a coordinator following the skills literally, with simulated users, workers and CI.

    python3 tests/simulation/sim.py /tmp/mr-sim          (about a minute; needs only Python and Git)

Builds a 40-repo organization (Gitflow repos, branch-naming rules, CI-profile builds, broken, slow and husky-style
builds, stale and active feature branches, an unreachable repo) plus a 10-repo Spring Boot 3 upgrade, then runs:
S1 inventory scan (cold and warm), S2 a 40-repo scripted rollout with pilots and a CI window, S3 an agent
recipe with parallel workers and a reviewer, S4/S9 resume after a crash, S6 branch-rule recovery, S7 moved
destinations. Prints findings (anything that went wrong or was harder than it should be) and metrics.
"""
import json, os, re, shutil, subprocess, sys, threading, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import world

HUB = Path(os.environ.get('HUB') or Path(__file__).resolve().parents[2])
MR = [sys.executable, str(HUB / 'scripts' / 'mr.py')]
FINDINGS, METRICS = [], {}

def finding(scn, sev, text):
    FINDINGS.append({'scenario': scn, 'severity': sev, 'finding': text})

class Coord:
    def __init__(self, w, name):
        self.w, self.name, self.log = Path(w), name, []
        self.env = dict(os.environ, **world.GIT_ENV, MULTI_REPO_HOME=str(self.w / 'home'),
                        ROLLOUT_LOCK_DIR=str(self.w / 'home' / 'locks'))
    def mr(self, *args, ok=True):
        t = time.time()
        p = subprocess.run(MR + [str(a) for a in args], capture_output=True, text=True, env=self.env, cwd='/tmp')
        self.log.append({'cmd': ' '.join(str(a) for a in args[:3]), 'rc': p.returncode, 'bytes': len(p.stdout) + len(p.stderr),
                         'secs': round(time.time() - t, 2)})
        if ok and p.returncode not in (0,):
            raise RuntimeError(f"mr {' '.join(map(str, args))} -> {p.returncode}: {p.stderr[-500:]}")
        try:
            return json.loads(p.stdout) if p.stdout.strip() else {}
        except json.JSONDecodeError:
            return {'_text': p.stdout, '_err': p.stderr, '_rc': p.returncode}
    def git(self, *args):
        return subprocess.run(['git', *map(str, args)], capture_output=True, text=True, env=self.env)
    def stats(self):
        return {'commands': len(self.log), 'output_kb': round(sum(x['bytes'] for x in self.log) / 1024, 1),
                'secs': round(sum(x['secs'] for x in self.log), 1),
                'biggest': sorted(self.log, key=lambda x: -x['bytes'])[:3]}

def ci_for_open_prs(c, store):
    """The CI server: run every pending PR's build (sandbox ci honors Jenkinsfile)."""
    prs = json.loads((Path(store) / 'prs.json').read_text())
    for p in prs:
        if p['state'] == 'open' and p['checks']['state'] == 'pending':
            c.mr('sandbox', 'ci', '--store', store, '--id', p['id'])

def remote(w, repo):
    return Path(w) / 'remotes' / (repo.replace('/', '__') + '.git')

def push_to_destination(c, w, repo, branch, files, msg='someone else'):
    tmp = Path(w) / 'tmp-push' / repo.replace('/', '__')
    shutil.rmtree(tmp, ignore_errors=True)
    subprocess.run(['git', 'clone', '-q', '-b', branch, f'file://{remote(w, repo)}', str(tmp)], env=c.env, check=True)
    for k, v in files.items():
        (tmp / k).write_text(v)
    subprocess.run(['git', '-C', str(tmp), 'add', '-A'], env=c.env, check=True)
    subprocess.run(['git', '-C', str(tmp), 'commit', '-qm', msg], env=c.env, check=True)
    subprocess.run(['git', '-C', str(tmp), 'push', '-q', 'origin', branch], env=c.env, check=True)

FLAG_CHECK = '''import json, sys
from pathlib import Path
for f in ('config.json', 'build.properties'):
    p = Path(f)
    if p.is_file():
        t = p.read_text()
        v = json.loads(t).get('featureBuilds') if f.endswith('json') else next((l.split('=',1)[1].strip() for l in t.splitlines() if l.startswith('featureBuilds')), None)
        print(json.dumps({'status': 'compliant' if str(v).lower() == 'false' else 'needs_change', 'value': str(v).lower(), 'evidence': f})); sys.exit()
print(json.dumps({'status': 'not_applicable', 'evidence': 'no config'}))
'''

# ---------------------------------------------------------------- S1: inventory scan
def s1(w):
    c = Coord(w, 's1')
    chk = Path(w) / 'flag.py'; chk.write_text(FLAG_CHECK)
    t = time.time()
    o = c.mr('scan', '--check', chk, '--paths', 'config.json', 'build.properties', '--repos', Path(w) / 'repos.txt',
             '--days', '30', '--name', 'inv1')
    cold = time.time() - t
    t = time.time()
    o2 = c.mr('scan', '--check', chk, '--paths', 'config.json', 'build.properties', '--repos', Path(w) / 'repos.txt',
              '--days', '30', '--name', 'inv2')
    warm = time.time() - t
    METRICS['S1'] = {'repos': o['repos'], 'branches': o['branches'], 'by_value': o['by_value'], 'cold_s': round(cold, 1),
                     'warm_s': round(warm, 1), 'cached_warm': o2['cached_checks'], **c.stats()}
    rows = json.loads((Path(w) / 'home' / 'scans' / 'inv1' / 'results.json').read_text())
    stale = [b for r in rows for b in r['branches'] if b['branch'] == 'feature/stale']
    if stale: finding('S1', 'bug', f'{len(stale)} stale feature branches scanned despite --days 30')
    variants = json.loads((Path(w) / 'variants.json').read_text())
    dev_missing = [r['repo'] for r in rows if variants.get(r['repo'], {}).get('base') == 'develop'
                   and not {'main', 'develop'} <= {b['branch'] for b in r['branches']}]
    if dev_missing:
        finding('S1', 'bug', f'{len(dev_missing)} Gitflow repos were not scanned on both main and develop')
    if warm > cold * 0.7:
        finding('S1', 'medium', f'warm rescan barely faster than cold ({warm:.1f}s vs {cold:.1f}s)')

# ---------------------------------------------------------------- S2..S5: scripted rollout at scale
def s2(w):
    c = Coord(w, 's2'); store = str(Path(w) / 'scm'); variants = json.loads((Path(w) / 'variants.json').read_text())
    run = 'ENG-500-flip'
    c.mr('init', '--id', run, '--recipe', 'disable-feature-builds', '--jira', 'ENG-500', '--jira-title', 'Disable feature builds',
         '--jira-url', 'https://jira/ENG-500', '--jira-evidence', 'jira tool', '--repos', Path(w) / 'repos.txt', '--request', 'flip it')
    d = c.mr('discover', '--run', run, '--pending', '--workers', '8', '--days', '30')
    METRICS['S2_discover'] = {'summary_bytes': len(json.dumps(d)), 'outcomes': d.get('outcomes'), 'branches': d.get('branches')}
    p = c.mr('plan', '--run', run, '--strategy', 'deliver in waves')
    METRICS['S2_plan'] = {k: (len(v) if isinstance(v, list) else v) for k, v in p.items() if k != 'preview'}
    # Every branch must be found: main, develop (Gitflow repos) and active feature branches; stale ones excluded by age.
    cov = d.get('coverage') or {}
    exp_scanned = sum(1 + (v['base'] == 'develop') + bool(v['features']) for v in variants.values())
    exp_old = sum(bool(v['features']) for v in variants.values())
    METRICS['S2_coverage'] = {k: cov.get(k) for k in ('branches', 'scanned', 'excluded_by_type', 'excluded_by_age',
                                                      'own_rollout_branches')}
    if (cov.get('scanned'), cov.get('excluded_by_age')) != (exp_scanned, exp_old):
        finding('S2', 'bug', f"coverage {cov.get('scanned')} scanned / {cov.get('excluded_by_age')} too old, "
                             f"expected {exp_scanned} / {exp_old}")
    p = c.mr('plan', '--run', run, '--strategy', 'deliver in waves')
    m = json.loads((Path(w) / 'home' / 'runs' / run / 'manifest.json').read_text())
    # Validate ahead (user said yes)
    needs = p['needs_approval']
    t = time.time()
    dry = c.mr('deliver', '--run', run, '--dry', '--target', *needs, '--workers', '3')
    METRICS['S2_validate'] = {'targets': len(needs), 'secs': round(time.time() - t, 1),
                              'outcomes': {k: sum(1 for r in dry if r['status'] == k) for k in {r['status'] for r in dry}}}
    qg = c.mr('questions', '--run', run)
    METRICS['S2_question_groups'] = [(g['count'], g['text'][:70]) for g in qg]
    q = c.mr('questions', '--run', run, '--all')
    prof = [x for x in q if variants.get(m and next((t['repo'] for t in m['plan']['targets'] if t['id'] == x.get('target')), ''), {}).get('build') == 'profile']
    if prof:
        finding('S2', 'high', f'{len(prof)} repos whose build needs the CI profile (visible in their Jenkinsfile) were reported as '
                '"already fails without our change". It is the wrong build command, not a broken branch: the user gets a misleading '
                'draft/skip/retry question per repo.')
    # Pilots: coordinator must pick one per diff kind by reading preview.md
    p2 = c.mr('plan', '--run', run, '--strategy', 'deliver in waves')
    pilots = p2.get('suggested_pilots') or []
    METRICS['S2_pilots'] = pilots
    bad_pilots = [x for x in pilots if any(r['target'] == x and r['status'] != 'validated' for r in dry)]
    if bad_pilots: finding('S2', 'medium', f'suggested pilots include targets that failed validation: {bad_pilots}')
    for g in c.mr('questions', '--run', run):
        if 'choices' in g:
            c.mr('answer', '--run', run, '--question', *g['ids'], '--answer', 'skip these', '--choice', 'skip', '--evidence', 'User: skip broken ones')
    c.mr('approve', '--run', run, '--pilot', *pilots, '--max-pending-ci', '6', '--evidence', 'User: pilots first, then go')
    # Delivery rounds
    rounds, stuck_rounds = 0, 0
    while rounds < 25:
        rounds += 1
        items = c.mr('take', '--run', run, '--worker', 'coordinator', '--task', 'deliver', '--limit', '50')
        ids = [i['target'] for i in items]
        if ids:
            c.mr('deliver', '--run', run, '--worker', 'coordinator', '--target', *ids, '--workers', '3')
        c.mr('open-prs', '--run', run, ok=False)
        ci_for_open_prs(c, store)
        c.mr('pr-status', '--run', run)
        rep = c.mr('report', '--run', run)
        if not ids:
            stuck_rounds += 1
            if stuck_rounds >= 2:
                break
        else:
            stuck_rounds = 0
    m = json.loads((Path(w) / 'home' / 'runs' / run / 'manifest.json').read_text())
    res = m['results']
    by = {}
    for tid, r in res.items():
        by[r.get('status')] = by.get(r.get('status'), 0) + 1
    METRICS['S2_delivery'] = {'rounds': rounds, 'status': by, 'prs': sum(1 for r in res.values() if r.get('pr_url')),
                              'open_questions': rep.get('open_questions'), 'stopped_groups': rep.get('stopped'),
                              'aborted': len(m['aborted'])}
    reasons = [g['reason'] for g in rep.get('stopped', [])]
    pol = [r for r in reasons if 'branching model' in r or 'branch name' in (r or '').lower()]
    if pol:
        finding('S2', 'high', f'{len(pol)} targets in project DEF were rejected by the server branch policy (feature/*|bugfix/* only). '
                'Branch format is fixed at init; there is no way to change it for the run, and the error is only a raw push log.')
    husky = [t for t in m['plan']['targets'] if variants.get(t['repo'], {}).get('build') == 'husky' and t['disposition'] == 'needs_change']
    hstat = [(t['id'], res.get(t['id'], {}).get('status'), (res.get(t['id'], {}).get('evidence') or '')[:80]) for t in husky]
    if any('hook' in (e or '').lower() for _, _, e in hstat):
        finding('S2', 'high', 'A build that installs its own Git hooks (husky-style `git config core.hooksPath`) disables the rollout '
                f'hooks for that clone; delivery then stops with "hooks are not active". Seen: {hstat[:2]}')
    diag = c.mr('diagnose', '--run', run)
    METRICS['S2_diagnose'] = {'problems': len(diag['problems']), 'kinds': sorted({p['problem'][:40] for p in diag['problems']})}
    jt = (Path(w) / 'home' / 'runs' / run / 'jira-table.md').read_text()
    METRICS['S2_jira_table_lines'] = jt.count('\n')
    METRICS['S2_totals'] = c.stats()
    if c.stats()['output_kb'] > 60:
        finding('S2', 'medium', f"The coordinator read {c.stats()['output_kb']} KB of command output for 40 repos; biggest: {c.stats()['biggest']}")
    return c, run

def s4_interrupt_and_resume(w, c, run):
    """A second coordinator (fresh chat) resumes after a crash between push and PR creation."""
    m = json.loads((Path(w) / 'home' / 'runs' / run / 'manifest.json').read_text())
    pushed = [t for t, r in m['results'].items() if r.get('status') == 'pushed']
    c2 = Coord(w, 's4')
    rep = c2.mr('report', '--run', run)
    diag = c2.mr('diagnose', '--run', run)
    METRICS['S4'] = {'pushed_without_pr_before': len(pushed), 'diagnose_problems': len(diag['problems']),
                     'resume_commands': 2, 'report_bytes': len(json.dumps(rep))}

def s5_moved(w, c, run):
    """Destinations move under targets that are approved but not delivered yet."""
    m = json.loads((Path(w) / 'home' / 'runs' / run / 'manifest.json').read_text())
    waiting = [t for t in m['plan']['targets'] if t['disposition'] == 'needs_change' and t['id'] not in m['results']][:4]
    for i, t in enumerate(waiting):
        files = {'README.md': 'moved\n'} if i % 2 == 0 else {'config.json': '{\n  "name": "x",\n  "featureBuilds": true,\n  "extra": 1\n}\n'}
        push_to_destination(c, w, t['repo'], t['branch'], files)
    METRICS['S5_moved'] = [t['id'] for t in waiting]

# ---------------------------------------------------------------- S3: agent recipe with parallel workers
BOOT_CHECK = '''import json, re, sys
from pathlib import Path
p = Path('pom.xml')
if not p.is_file(): print(json.dumps({'status':'not_applicable','evidence':'no pom.xml'})); sys.exit()
m = re.search(r'<spring-boot.version>([^<]+)<', p.read_text())
if not m: print(json.dumps({'status':'maybe','evidence':'pom.xml without a spring-boot.version property (inherited from a parent?)'})); sys.exit()
v = m.group(1)
print(json.dumps({'status':'compliant' if v.startswith('3') else 'needs_change','value':v,'evidence':'pom.xml spring-boot.version'}))
'''
BOOT_BUILD = '''v=$(grep -o '<spring-boot.version>[^<]*' pom.xml | cut -d'>' -f2)
case "$v" in 3*) if grep -rq 'javax.servlet' src; then echo "error: package javax.servlet does not exist"; exit 1; fi;; esac
echo ok
'''
def make_boot_world(w):
    w = Path(w)
    lines = []
    for i in range(10):
        name = f'GHI/api-{i:02d}'
        bare = w / 'remotes' / (name.replace('/', '__') + '.git'); world.run(['git', 'init', '-q', '--bare', '-b', 'main', str(bare)])
        for k in ('allowFilter', 'allowAnySHA1InWant'): world.run(['git', '-C', str(bare), 'config', f'uploadpack.{k}', 'true'])
        wd = w / 'work' / name.replace('/', '__'); (wd / 'src').mkdir(parents=True)
        ver = '3.2.1' if i >= 8 else '2.7.18'
        pom = f'<project>\n  <properties>\n    <spring-boot.version>{ver}</spring-boot.version>\n  </properties>\n</project>\n' if i != 7 else '<project><parent>platform-parent</parent></project>\n'
        (wd / 'pom.xml').write_text(pom); (wd / 'build.sh').write_text(BOOT_BUILD)
        (wd / 'src' / 'Main.java').write_text(('import javax.servlet.Filter;\n' if i % 2 == 0 else 'import java.util.List;\n') + 'class Main {}\n')
        world.run(['git', 'init', '-q', '-b', 'main'], wd); world.run(['git', 'add', '-A'], wd, world.GIT_ENV)
        world.run(['git', 'commit', '-qm', 'init'], wd, world.GIT_ENV); world.run(['git', 'remote', 'add', 'origin', f'file://{bare}'], wd)
        world.run(['git', 'push', '-q', 'origin', 'main'], wd); lines.append(f'{name} file://{bare}')
    (w / 'boot-repos.txt').write_text('\n'.join(lines) + '\n')
    with open(w / 'repos.txt', 'a') as fh:  # the fake PR server knows repos from repos.txt
        fh.write('\n'.join(lines) + '\n')
    rd = w / 'home' / 'recipes' / 'boot3'; rd.mkdir(parents=True)
    (rd / 'recipe.md').write_text('# Upgrade Spring Boot to 3\n\nStatus: trialed · Mode: agent · Risk: high · Validation: full · Toolkit: 3.x\n'
                                  'Check paths: pom.xml\n\n## How the change is made\n1. Set spring-boot.version to 3.2.1.\n2. Migrate javax.* imports to jakarta.*.\n')
    (rd / 'check.py').write_text(BOOT_CHECK)

def s3(w):
    make_boot_world(w)
    c = Coord(w, 's3'); run = 'ENG-600-boot3'; store = str(Path(w) / 'scm')
    c.mr('init', '--id', run, '--recipe', 'boot3', '--jira', 'ENG-600', '--jira-title', 'Boot 3', '--jira-url', 'https://jira/ENG-600',
         '--jira-evidence', 'jira tool', '--repos', Path(w) / 'boot-repos.txt', '--request', 'upgrade to boot 3')
    c.mr('discover', '--run', run, '--pending', '--workers', '4')
    p = c.mr('plan', '--run', run, '--strategy', 'agents')
    METRICS['S3_plan'] = {'counts': p['counts'], 'undecided': len(p['undecided'])}
    # decide the maybe (inherited parent) -> not_applicable, like a worker would after reading the parent
    for tid in p['undecided']:
        c.mr('decide', '--run', run, '--target', tid, '--status', 'not_applicable', '--evidence', 'version comes from platform-parent; handled there')
    p = c.mr('plan', '--run', run, '--strategy', 'agents')
    targets = p['needs_approval']
    pilots = targets[:2]
    c.mr('approve', '--run', run, '--targets', *pilots, '--pilot', *pilots, '--evidence', 'User: pilot two')
    attempts = {}
    lock = threading.Lock()
    def worker(name):
        wc = Coord(w, name)
        for _ in range(6):
            items = wc.mr('take', '--run', run, '--worker', name, '--task', 'change', '--limit', '1')
            if not items:
                return
            it = items[0]; wt = Path(it['worktree'])
            pom = wt / 'pom.xml'; pom.write_text(re.sub(r'<spring-boot.version>[^<]+', '<spring-boot.version>3.2.1', pom.read_text()))
            for attempt in range(1, 4):
                r = wc.mr('deliver', '--run', run, '--target', it['target'], '--worker', name, '--dry')[0]
                with lock: attempts[it['target']] = attempt
                if r['status'] == 'validated':
                    break
                if r['status'] == 'validation_failed':  # agent reads the log, migrates javax -> jakarta
                    for f in (wt / 'src').rglob('*.java'):
                        f.write_text(f.read_text().replace('javax.servlet', 'jakarta.servlet'))
                else:
                    break
    def verifier(name):
        vc = Coord(w, name)
        for _ in range(20):
            items = vc.mr('take', '--run', run, '--worker', name, '--task', 'review', '--limit', '1')
            if not items:
                time.sleep(0.5)
                if not vc.mr('take', '--run', run, '--task', 'change', '--limit', '1') and not vc.mr('take', '--run', run, '--task', 'review', '--limit', '1'):
                    return
                continue
            vc.mr('record', '--run', run, '--target', items[0]['target'], '--status', 'observed', '--reviewed', '--evidence', 'diff matches recipe')
    def phase():
        ts = [threading.Thread(target=worker, args=(f'w{i}',)) for i in range(3)] + [threading.Thread(target=verifier, args=('v1',))]
        [t.start() for t in ts]; [t.join() for t in ts]
        items = c.mr('take', '--run', run, '--worker', 'coordinator', '--task', 'deliver', '--limit', '20')
        if items:
            c.mr('deliver', '--run', run, '--worker', 'coordinator', '--target', *[i['target'] for i in items])
        c.mr('open-prs', '--run', run, ok=False); ci_for_open_prs(c, store); c.mr('pr-status', '--run', run)
    phase()
    m = json.loads((Path(w) / 'home' / 'runs' / run / 'manifest.json').read_text())
    pil = {t: m['results'].get(t, {}).get('status') for t in pilots}
    prs = json.loads((Path(store) / 'prs.json').read_text())
    drafts = [p for p in prs if p['repo'].startswith('GHI/') and p.get('draft')]
    METRICS['S3_pilots'] = {'status': pil, 'drafts': len(drafts), 'attempts': dict(attempts)}
    c.mr('approve', '--run', run, '--evidence', 'User: pilots look good, do the rest')
    phase()
    m = json.loads((Path(w) / 'home' / 'runs' / run / 'manifest.json').read_text())
    st = {}
    for t in targets:
        st[m['results'].get(t, {}).get('status')] = st.get(m['results'].get(t, {}).get('status'), 0) + 1
    METRICS['S3_final'] = {'targets': len(targets), 'status': st, 'attempts': dict(attempts), **c.stats()}
    unreviewed = [t for t in targets if m['results'].get(t, {}).get('status') in ('pushed', 'pr_opened') and not m['results'][t].get('reviewed_patch_sha256')]
    if unreviewed: finding('S3', 'bug', f'pushed without review: {unreviewed}')
    if st.get('pr_opened', 0) + st.get('pr_updated', 0) < len(targets):
        finding('S3', 'high', f'agent rollout did not finish: {st}')

def s6_branch_policy(w):
    c = Coord(w, 's6'); run = 'ENG-700-policy'
    lines = [l for l in (Path(w) / 'repos.txt').read_text().splitlines() if l.startswith('DEF/svc-3')][:3]
    (Path(w) / 'def-repos.txt').write_text('\n'.join(lines) + '\n')
    c.mr('init', '--id', run, '--recipe', 'disable-feature-builds', '--jira', 'ENG-700', '--jira-title', 't', '--jira-url', 'https://jira/ENG-700',
         '--jira-evidence', 'e', '--repos', Path(w) / 'def-repos.txt', '--request', 'x', '--branch-format', '{key}/{recipe}-{target}')
    c.mr('discover', '--run', run, '--pending')
    p = c.mr('plan', '--run', run, '--strategy', 's')
    c.mr('approve', '--run', run, '--evidence', 'go')
    first = c.mr('deliver', '--run', run, '--target', *p['needs_approval'])
    refused = [r for r in first if 'server refused' in (r.get('reason') or '')]
    rb = c.mr('branches', '--run', run, '--format', 'feature/{key}-{recipe}')
    c.mr('plan', '--run', run, '--strategy', 's')
    for r in refused:
        c.mr('retry', '--run', run, '--target', r['target'], '--evidence', 'renamed branch')
    second = c.mr('deliver', '--run', run, '--target', *[r['target'] for r in refused])
    METRICS['S6'] = {'refused_first': len(refused), 'renamed': rb.get('renamed'), 'second': [r['status'] for r in second],
                     'reason_example': (refused[0]['reason'][:160] if refused else None), **c.stats()}
    if not refused or any(r['status'] != 'pushed' for r in second):
        finding('S6', 'high', f"branch-policy recovery did not work: {METRICS['S6']}")

def s7_moved_and_s9_crash(w):
    """Destinations move after approval (unrelated vs same-file changes); then a crash between push and PR."""
    c = Coord(w, 's7'); run = 'ENG-800-moves'; store = str(Path(w) / 'scm')
    lines = [l for l in (Path(w) / 'repos.txt').read_text().splitlines() if l.split()[0] in
             ('ABC/svc-01', 'ABC/svc-02', 'ABC/svc-04', 'ABC/svc-11', 'ABC/svc-16', 'ABC/svc-17')]
    (Path(w) / 'move-repos.txt').write_text('\n'.join(lines) + '\n')
    c.mr('init', '--id', run, '--recipe', 'disable-feature-builds', '--jira', 'ENG-800', '--jira-title', 't', '--jira-url', 'https://jira/ENG-800',
         '--jira-evidence', 'e', '--repos', Path(w) / 'move-repos.txt', '--request', 'x')
    c.mr('discover', '--run', run, '--pending')
    p = c.mr('plan', '--run', run, '--strategy', 's')
    c.mr('approve', '--run', run, '--evidence', 'go')
    m = json.loads((Path(w) / 'home' / 'runs' / run / 'manifest.json').read_text())
    tg = [t for t in m['plan']['targets'] if t['disposition'] == 'needs_change' and t['branch'] in ('main', 'develop')][:4]
    for i, t in enumerate(tg):  # two unrelated moves, two that rewrite the file we change
        files = {'README.md': 'moved\n'} if i < 2 else {'config.json': '{\n  "name": "renamed",\n  "featureBuilds": true\n}\n'}
        push_to_destination(c, w, t['repo'], t['branch'], files)
    ids = [t['id'] for t in tg]
    first = {r['target']: r['status'] for r in c.mr('deliver', '--run', run, '--target', *ids)}
    rep = c.mr('report', '--run', run)
    moved = [x for g in rep['stopped'] for x in g['targets'] if 'moved' in g['reason']]
    c.mr('discover', '--run', run, *sum([['--repo', t['repo']] for t in tg], []))
    p = c.mr('plan', '--run', run, '--strategy', 's')
    for t in moved:
        c.mr('retry', '--run', run, '--target', t, '--evidence', 'rediscovered')
    reapprove = p['needs_approval']
    if reapprove:
        c.mr('approve', '--run', run, '--targets', *reapprove, '--evidence', 'User: new diffs look right')
    second = {r['target']: r['status'] for r in c.mr('deliver', '--run', run, '--target', *ids)}
    METRICS['S7'] = {'first': first, 'moved_reported': len(moved), 'needed_reapproval': len(reapprove),
                     'second': second, 'commands': len(c.log)}
    if any(v != 'pushed' for v in second.values()):
        finding('S7', 'high', f'moved destinations did not recover: {second}')
    if len(reapprove) != 2:
        finding('S7', 'medium', f'expected exactly the 2 same-file moves to need re-approval, got {reapprove}')
    # S9: crash after push, before PR; a fresh coordinator resumes
    c2 = Coord(w, 's9')
    rep = c2.mr('report', '--run', run)
    diag = c2.mr('diagnose', '--run', run)
    stranded = rep.get('pushed_without_pr', [])
    op = c2.mr('open-prs', '--run', run, ok=False)
    after = c2.mr('report', '--run', run)
    METRICS['S9'] = {'stranded_seen_in_report': len(stranded), 'diagnose_flags': sum('no PR' in p['problem'] for p in diag['problems']),
                     'opened_on_resume': sum(1 for x in op if x.get('status') == 'pr_opened'),
                     'stranded_after': len(after.get('pushed_without_pr', [])), 'resume_commands': len(c2.log),
                     'resume_output_kb': round(sum(x['bytes'] for x in c2.log) / 1024, 1)}
    if after.get('pushed_without_pr'):
        finding('S9', 'high', f"resume left pushed targets without PRs: {after['pushed_without_pr']}")

if __name__ == '__main__':
    w = sys.argv[1]
    if not (Path(w) / 'repos.txt').exists():
        world.make(w, 40)
    s1(w)
    c, run = s2(w)
    s4_interrupt_and_resume(w, c, run)
    s3(w)
    s6_branch_policy(w)
    s7_moved_and_s9_crash(w)
    Path(w, 'findings.json').write_text(json.dumps({'findings': FINDINGS, 'metrics': METRICS}, indent=1, default=str))
    print(json.dumps({'findings': FINDINGS, 'metrics': METRICS}, indent=1, default=str))
    sys.exit(1 if any(f['severity'] in ('bug', 'high') for f in FINDINGS) else 0)
