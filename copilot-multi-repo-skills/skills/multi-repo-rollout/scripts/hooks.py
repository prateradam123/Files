#!/usr/bin/env python3
"""Git hooks installed into automation clones by `git_probe.py prepare`.

They make the rules hold no matter who runs git (Copilot's tools, a terminal, a person):
  commit-msg  every commit subject starts with the target's Jira key and a space
  pre-commit  the run approves this target now, and the staged change equals the approved preview
  pre-push    only the target's own source branch, no force-push, destination not moved, content approved
After our checks pass, any hook the repo or your global config already had is run too.
`--no-verify` skips hooks; ingest re-verifies pushed commits independently, so that gets caught.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ZERO, check_subject, commit_patch, git, index_patch, load_target, own_commits, patch_hash  # noqa: E402
# The ledger module is imported only by the hooks that consult it: commit-msg (every commit) stays light.


def fail(msg):
    sys.stderr.write('\n[rollout hook] BLOCKED: ' + msg + '\n\n')
    sys.exit(1)


def target_or_fail():
    t = load_target('.')
    if not t:
        fail('This is an automation clone. Commit and push only inside a target worktree '
             'created by `recipe_run.py preview`.')
    return t


def chain(name, args, stdin=None):
    gitdir = Path(git('.', 'rev-parse', '--git-common-dir').strip()).resolve()
    info = gitdir / 'rollout-hooks' / 'chain.json'
    if not info.exists():
        return
    c = json.loads(info.read_text())
    for d in (c.get('previous_hooks_path'), c.get('repo_hooks')):
        if d:
            h = Path(d).expanduser() / name
            if h.is_file() and h.stat().st_mode & 0o111 and 'multi-repo-toolkit hook' not in h.read_text(errors='ignore'):
                r = subprocess.run([str(h), *args], input=stdin)
                if r.returncode:
                    sys.exit(r.returncode)
                return


def gate(t, action, sha, patch):
    try:
        import run_state
        return run_state.check_gate(t['run'], t['target'], action, sha, patch)
    except ValueError as e:
        fail(str(e))


def commit_msg(args):
    t = target_or_fail()
    lines = [x for x in Path(args[0]).read_text().splitlines() if x.strip() and not x.startswith('#')]
    try:
        check_subject(t['jira_key'], lines[0] if lines else '')
    except ValueError as e:
        fail(str(e))
    chain('commit-msg', args)


def pre_commit(args):
    t = target_or_fail()
    gate(t, 'commit', t['base_sha'], patch_hash(index_patch('.', t['base_sha'])))
    chain('pre-commit', args)


def pre_push(args):
    t = target_or_fail()
    data = sys.stdin.buffer.read()
    remote = args[0]
    for line in data.decode().splitlines():
        local_ref, local_sha, remote_ref, remote_sha = line.split()
        if remote_ref != 'refs/heads/' + t['source_branch']:
            fail(f"Only {t['source_branch']} may be pushed from this worktree (attempted {remote_ref}).")
        if local_sha == ZERO:
            gate(t, 'delete_branch', None, None)
            continue
        if remote_sha != ZERO:
            ok = subprocess.run(['git', 'merge-base', '--is-ancestor', remote_sha, local_sha]).returncode == 0
            if not ok:
                fail('Force-push is not allowed. Fetch, integrate the remote commits, and push again.')
        live = git('.', 'ls-remote', remote, 'refs/heads/' + t['destination']).split()
        if not live:
            fail(f"Could not read destination {t['destination']} from {remote}.")
        gate(t, 'push', live[0], patch_hash(commit_patch('.', t['base_sha'], local_sha)))
        for sha, subj in own_commits('.', t['base_sha'], local_sha, live[0]):  # skips merged-in destination commits
            try:
                check_subject(t['jira_key'], subj)
            except ValueError:
                fail(f"Commit {sha[:10]} does not start with \"{t['jira_key']} \". Reword it before pushing.")
    chain('pre-push', args, data)


if __name__ == '__main__':
    name, rest = sys.argv[1], sys.argv[2:]
    {'commit-msg': commit_msg, 'pre-commit': pre_commit, 'pre-push': pre_push}[name](rest)
