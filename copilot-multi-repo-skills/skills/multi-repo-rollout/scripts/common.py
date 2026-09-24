#!/usr/bin/env python3
"""Shared helpers: naming, hashing, Git plumbing. Standard library only."""
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

TOOLKIT = Path(__file__).resolve().parents[1]   # the multi-repo-rollout skill folder
VERSION = '4.2.0'
DEFAULT_CONFIG = {  # optional: ~/.multi-repo/config.json only needs keys you want to change
    'recipes_dir': None,        # default: <home>/recipes. Point at a shared Git checkout to share recipes.
    'branch_format': 'feature/{key}-{recipe}',
    'limits': {'git': 6, 'build': max(1, (os.cpu_count() or 2) // 2), 'api': 4, 'api_rate': 4.0, 'api_burst': 20,
               'write_interval': 1.0, 'partial_clone_hosts': []},
    'scm': {'hosts': {}},       # optional adapter, e.g. {"bitbucket.example.com": {"kind": "bitbucket", "api": "...", "token_env": "BITBUCKET_TOKEN"}}
}


def data_home():
    """Everything that isn't the skill itself: config, recipes, repo facts, runs, clones, cache, locks.
    Created on first use, like ~/.m2. Override with MULTI_REPO_HOME."""
    p = Path(os.environ.get('MULTI_REPO_HOME') or Path.home() / '.multi-repo').expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def config():
    p = data_home() / 'config.json'
    return json.loads(p.read_text(encoding='utf-8')) if p.is_file() else {}


def recipes_dir():
    d = Path(config().get('recipes_dir') or data_home() / 'recipes').expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d
KEY = re.compile(r'^[A-Z][A-Z0-9_]*-[1-9][0-9]*$')
SHA = re.compile(r'^[0-9a-f]{7,64}$')
ZERO = '0' * 40
CHECK_STATUSES = {'compliant', 'needs_change', 'not_applicable', 'unknown', 'maybe'}


def now():
    return datetime.now(timezone.utc).isoformat()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    """Atomic write so an interrupted run never leaves half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, indent=2, ensure_ascii=False)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def slug(text, limit=48):
    s = re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')
    return (s[:limit].rstrip('-')) or 'x'


def target_id(repo, branch):
    """Stable, readable, collision-resistant ID for one repo+destination branch."""
    h = hashlib.sha1((repo + '\0' + branch).encode()).hexdigest()[:5]
    return f'{slug(repo, 40)}--{slug(branch, 30)}-{h}'


DEV_BRANCHES = ('main', 'master', 'develop', 'development', 'dev', 'trunk')


def branch_name(fmt, key, recipe, tid, branch=None, dev=True):
    """Source branch for a target. The default `feature/{key}-{recipe}` passes typical branch permissions
    (feature/*, bugfix/*). Targets on a non-dev branch get the branch as a suffix so names stay unique.
    Placeholders: {key} {recipe} {target} {branch}."""
    name = fmt.format(key=key, recipe=slug(recipe, 30), target=tid, branch=slug(branch or '', 40))
    if not dev and '{target}' not in fmt and '{branch}' not in fmt:
        name += '-' + slug(branch or tid, 40)
    require(re.fullmatch(r'[A-Za-z0-9._/-]+', name) and '..' not in name and not name.endswith('.lock'),
            'Branch format produced an invalid Git branch name: ' + name)
    return name


def dir_hash(path):
    """Hash every file in a recipe folder so any edit is detected."""
    h = hashlib.sha256()
    root = Path(path)
    for f in sorted(p for p in root.rglob('*') if p.is_file()):
        if '__pycache__' in f.parts or f.name == '.DS_Store':
            continue
        h.update(f.relative_to(root).as_posix().encode() + b'\0')
        h.update(f.read_bytes() + b'\0')
    return h.hexdigest()


def git(repo, *args, env=None, check=True, input=None):
    p = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True,
                       env=env, input=input)
    if check and p.returncode:
        raise ValueError((p.stderr.strip() or 'git failed') + ' [git ' + ' '.join(args[:3]) + ']')
    return p.stdout


DIFF_ARGS = ['diff', '--binary', '--no-color', '--no-ext-diff', '--no-renames', '--full-index']


def patch_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def shape_hash(text):
    """Same edit in different repos gets the same shape: only file names and changed lines count."""
    kept = [line for line in text.splitlines()
            if line.startswith(('+', '-')) and not line.startswith(('index ',))]
    return hashlib.sha256('\n'.join(kept).encode()).hexdigest()[:12]


def gitdir_of(worktree):
    """The .git directory of a clone or worktree, read from disk (no git process)."""
    g = Path(worktree) / '.git'
    if g.is_dir():
        return g.resolve()
    if g.is_file():
        text = g.read_text().strip()
        if text.startswith('gitdir:'):
            p = Path(text.split(':', 1)[1].strip())
            return (p if p.is_absolute() else (Path(worktree) / p)).resolve()
    return Path(git(worktree, 'rev-parse', '--absolute-git-dir').strip())


def worktree_patch(worktree, base):
    """Everything in the worktree (committed + uncommitted + untracked, minus ignored) vs base.
    Uses a throwaway copy of the worktree's own index: its stat data is current, so `add -A` only looks at
    files that changed (a full read-tree + add re-hashed every file, slow on large repos)."""
    import shutil
    with tempfile.TemporaryDirectory() as tmp:
        idx = Path(tmp) / 'index'
        env = dict(os.environ, GIT_INDEX_FILE=str(idx))
        real = gitdir_of(worktree) / 'index'
        if real.is_file():
            shutil.copyfile(real, idx)
        else:
            git(worktree, 'read-tree', 'HEAD', env=env)
        git(worktree, 'add', '-A', env=env)
        return git(worktree, *DIFF_ARGS, '--cached', base, env=env)


def index_patch(worktree, base):
    """What the next commit would contain, cumulatively, vs base (used by pre-commit)."""
    return git(worktree, *DIFF_ARGS, '--cached', base)


def commit_patch(worktree, base, head):
    """What a pushed commit contains, cumulatively, vs base (used by pre-push and ingest)."""
    return git(worktree, *DIFF_ARGS, base, head)


def own_commits(repo, base, head, destination_ref=None):
    """Non-merge commits we added: base..head, minus anything already on the destination (e.g. commits
    brought in by merging the destination into the PR branch)."""
    args = ['log', '--no-merges', '--format=%H%x09%s', f'{base}..{head}']
    if destination_ref and git(repo, 'rev-parse', '--verify', '--quiet', destination_ref + '^{commit}',
                               check=False).strip():
        args += ['--not', destination_ref]
    return [tuple(row.split('\t', 1)) for row in git(repo, *args).splitlines()]


def target_file(worktree):
    return gitdir_of(worktree) / 'rollout-target.json'


def load_target(worktree):
    p = target_file(worktree)
    if not p.is_file():
        return None
    return read_json(p)


def check_subject(key, subject):
    require(KEY.fullmatch(key or ''), 'Invalid canonical Jira key')
    require('\n' not in subject and subject.startswith(key + ' ') and subject[len(key) + 1:].strip(),
            f'Commit subject must start with "{key} " followed by a message. Got: {subject!r}')
    return True


def interpreter_for(script):
    """Run recipe scripts by extension so exec bits and shebangs don't matter (Windows-safe)."""
    import shutil
    import sys
    s = str(script)
    if s.endswith('.py'):
        return [sys.executable, s]
    if s.endswith('.sh'):
        sh = shutil.which('bash') or shutil.which('sh')
        require(sh, 'No bash/sh available to run ' + s)
        return [sh, s]
    return [s]


def find_script(recipe_dir, name):
    for ext in ('.py', '.sh', ''):
        p = Path(recipe_dir) / (name + ext)
        if p.is_file():
            return p.resolve()  # scripts run with cwd = a checkout, so relative paths would break
    return None


def recipe_meta(recipe_dir):
    md = Path(recipe_dir) / 'recipe.md'
    script = find_script(recipe_dir, 'check')
    key = (str(Path(recipe_dir).resolve()), md.stat().st_mtime_ns, script and script.stat().st_mtime_ns)
    if key not in _META:
        _META[key] = _recipe_meta(recipe_dir)
    return _META[key]


_META = {}


def _recipe_meta(recipe_dir):
    """What the recipe declares on its status line and `Check paths:` line:
      Mode: scripted | agent        agent = the change needs judgement per repo (see AGENTS.md rule 2)
      Validation: check | compile | full   (agent mode always uses full)
      Check paths: files check.py reads (globs allowed); discovery downloads and caches only these."""
    text = (Path(recipe_dir) / 'recipe.md').read_text()
    status = next((line for line in text.splitlines() if line.startswith('Status:')), '')
    mode = re.search(r'Mode:\s*(scripted|agent)', status, re.I)
    level = re.search(r'Validation:\s*(check|compile|full)', status, re.I)
    paths = re.search(r'^Check paths:\s*(.+)$', text, re.M)
    script = find_script(recipe_dir, 'check')
    code = script.read_text(errors='ignore') if script else ''
    mode = mode.group(1).lower() if mode else 'scripted'
    patterns, days = branch_rule(text)
    return {'mode': mode, 'branch_patterns': patterns, 'active_days': days,
            'validation': 'full' if mode == 'agent' else (level.group(1).lower() if level else 'full'),
            'check_paths': [x.strip().strip('`') for x in paths.group(1).split(',') if x.strip()] if paths else [],
            'check_uses_target': 'TARGET_REPO' in code or 'TARGET_BRANCH' in code}


def branch_rule(recipe_text):
    """The recipe's `Branches:` line: which branch types to scan and how recent they must be.
      Branches: all                                        every branch (the default when there's no line)
      Branches: all; active within 180 days                every branch with a commit in the last 180 days
      Branches: develop*, release/*, main, master; active within 90 days
    Patterns are globs matched case-insensitively against the full branch name (`*develop*` matches anywhere).
    Returns (patterns or None for all, days or None for no cutoff)."""
    m = re.search(r'^Branches:\s*(.+)$', recipe_text, re.M | re.I)
    if not m:
        return None, None
    text = m.group(1)
    d = re.search(r'active\s+within\s+(\d+)\s+days?', text, re.I)
    rest = re.sub(r'[;,]?\s*active\s+within\s+\d+\s+days?', '', text, flags=re.I)
    pats = [x.strip().strip('`') for x in re.split(r'[,;]', rest) if x.strip()]
    pats = None if not pats or [x.lower() for x in pats] == ['all'] else pats
    return pats, int(d.group(1)) if d else None


def branch_matches(name, patterns):
    import fnmatch
    return patterns is None or any(fnmatch.fnmatchcase(name.lower(), p.lower()) for p in patterns)


def toolkit_version():
    return VERSION
