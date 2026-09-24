#!/usr/bin/env python3
"""mr: one command for the multi-repo skills.

    python3 $HOME/.copilot/skills/multi-repo-rollout/scripts/mr.py <command> [options]

`mr help` lists the commands; `mr <command> --help` shows each command's options. Everything this writes
(config, recipes, repo facts, runs, clones, cache) lives in ~/.multi-repo (or $MULTI_REPO_HOME).
"""
import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import VERSION, data_home, recipes_dir  # noqa: E402

GROUPS = [
    ('Setup (optional)', [('doctor', None, None, 'Check Python, Git and API tokens; show where data lives')]),
    ('Scan (read-only, no run needed)', [
        ('scan', 'recipe_run', 'scan', 'Answer one question across many repos: --check <recipe|script> --repos F | --project K'),
        ('check', 'recipe_run', 'check', 'Run a recipe check on one commit or folder'),
        ('trial', 'recipe_run', 'trial', 'Try a recipe on one branch in a throwaway checkout; print the diff'),
    ]),
    ('Rollout', [
        ('init', 'run_state', 'init', 'Start a run (recipe, repos, Jira lookup)'),
        ('discover', 'recipe_run', 'discover', 'Scan the run\'s repos and prepare worktrees/diffs where a change is needed'),
        ('plan', 'run_state', 'plan', 'Build the plan and preview.md'),
        ('approve', 'run_state', 'approve', 'Record the user\'s approval (pilots with --pilot)'),
        ('take', 'run_state', 'take', 'Next ready items; --worker claims them'),
        ('decide', 'recipe_run', 'decide', 'Record a judgement on an undecided target'),
        ('capture', 'recipe_run', 'capture', 'Record a worktree diff after an agent edit'),
        ('deliver', 'recipe_run', 'deliver', 'Check, build, commit, push (--dry: check and build only)'),
        ('build', 'recipe_run', 'build', 'Run a build in a worktree or on an unchanged commit'),
        ('record', 'run_state', 'record', 'Record a step for a target'),
        ('gate', 'run_state', 'gate', 'May this action happen now? (read-only)'),
        ('ask', 'run_state', 'ask', 'Record a question for the user'),
        ('answer', 'run_state', 'answer', 'Record the user\'s answer'),
        ('questions', 'run_state', 'questions', 'Open questions'),
        ('retry', 'run_state', 'retry', 'Put a stopped target back in the queue'),
        ('revise', 'run_state', 'revise', 'Record a corrected recipe mid-run'),
        ('abort', 'run_state', 'abort', 'Stop targets'),
        ('report', 'run_state', 'report', 'Write report.md and jira-table.md'),
        ('diagnose', 'recipe_run', 'diagnose', 'What is stuck or inconsistent in a run, with fixes'),
        ('branches', 'recipe_run', 'branches', 'Change a run\'s branch format (renames unpushed branches)'),
        ('progress', 'recipe_run', 'progress', 'Recheck all targets on the server vs. the baseline'),
        ('clean', 'recipe_run', 'clean', 'Remove finished worktrees and unused clones'),
        ('show', 'run_state', 'show', 'Print a run\'s ledger'),
        ('runs', 'run_state', 'list', 'List runs'),
    ]),
    ('SCM API (optional: set BITBUCKET_TOKEN or GITHUB_TOKEN)', [
        ('list-repos', 'scm', 'list-repos', 'Inventory a project into a repos file'),
        ('open-prs', 'scm', 'open-prs', 'Open or update PRs for pushed targets'),
        ('pr-status', 'scm', 'pr-status', 'Record state and CI of every PR in a run'),
        ('close-prs', 'scm', 'close-prs', 'Close PRs of aborted targets'),
    ]),
    ('Git, facts and testing', [
        ('prepare', 'git_probe', 'prepare', 'Create or refresh an automation clone'),
        ('read', 'git_probe', 'read', 'A file at a commit'),
        ('facts', 'repo_facts', None, 'Repo facts: facts show|set|merge|list'),
        ('sandbox', 'sandbox', None, 'Fake repos, Jira and PR server for trying things safely'),
    ]),
]
COMMANDS = {c: (m, s) for _, rows in GROUPS for c, m, s, _ in rows}


def resolve(argv):
    """Let commands say `--run ENG-1-x` and `--recipe my-recipe` instead of full paths."""
    out, i = [], 0
    while i < len(argv):
        a = argv[i]
        out.append(a)
        if a in ('--run', '--recipe') and i + 1 < len(argv):
            v = argv[i + 1]
            if not Path(v).expanduser().exists():
                bases = [data_home() / 'runs'] if a == '--run' else [recipes_dir(), HERE.parent / 'examples']
                v = next((str(b / v) for b in bases if (b / v).is_dir()), v)
            out.append(v)
            i += 2
            continue
        i += 1
    return out


def doctor(argv):
    import os
    home = data_home()
    git = shutil.which('git')
    checks = [{'check': 'python 3.10+', 'ok': sys.version_info >= (3, 10), 'found': sys.version.split()[0]},
              {'check': 'git', 'ok': bool(git),
               'found': subprocess.run(['git', '--version'], capture_output=True, text=True).stdout.strip() if git else None}]
    tokens = [k for k in ('BITBUCKET_TOKEN', 'GITHUB_TOKEN', 'GH_TOKEN') if os.environ.get(k)]
    print(json.dumps({'version': VERSION, 'ok': all(c['ok'] for c in checks), 'checks': checks, 'data': str(home),
                      'api_tokens': tokens or 'none (optional: set BITBUCKET_TOKEN or GITHUB_TOKEN for faster PR work)'},
                     indent=2))
    return 0 if all(c['ok'] for c in checks) else 2


def usage():
    print(__doc__.strip() + f'\n\nVersion {VERSION}. Data: {data_home()}\n')
    for title, rows in GROUPS:
        print(title)
        for c, _, _, text in rows:
            print(f'  {c:<15} {text}')
        print()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('help', '-h', '--help'):
        usage()
        return 0
    cmd = argv[0]
    if cmd == 'doctor':
        return doctor(argv[1:])
    if cmd not in COMMANDS:
        print(f'BLOCKED: unknown command "{cmd}". Run `mr help`.', file=sys.stderr)
        return 2
    module, sub = COMMANDS[cmd]
    if module == 'sandbox' and not (HERE / 'sandbox.py').is_file():
        print('BLOCKED: the sandbox is part of the developer kit (see README: Developer kit).', file=sys.stderr)
        return 2
    sys.argv = [f'mr {cmd}'] + ([sub] if sub else []) + resolve(argv[1:])
    importlib.import_module(module).main()
    return 0


if __name__ == '__main__':
    sys.exit(main())
