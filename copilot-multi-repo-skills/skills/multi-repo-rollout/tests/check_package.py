#!/usr/bin/env python3
"""Package self-check (no network, no agents), for the ~/.copilot layout:
- every skill and agent has frontmatter with name/description; a skill's name matches its folder;
- every relative Markdown link resolves;
- every `mr <command> --flag` mentioned in the skills, agents and README exists;
- every example recipe has recipe.md (with Mode and Risk) and a check script.
"""
import functools
import re
import subprocess
import sys
from pathlib import Path

HUB = Path(__file__).resolve().parents[1]          # skills/multi-repo-rollout
SKILLS = HUB.parent
AGENTS = SKILLS.parent / 'agents'
MR = HUB / 'scripts' / 'mr.py'
sys.path.insert(0, str(HUB / 'scripts'))


def frontmatter(text):
    m = re.match(r'---\n(.*?)\n---\n', text, re.S)
    return dict(re.findall(r'^(\w+): (.+)$', m.group(1), re.M)) if m else None


@functools.lru_cache(maxsize=None)
def help_text(cmd):
    p = subprocess.run([sys.executable, str(MR), cmd, '--help'], capture_output=True, text=True, timeout=60)
    return p.stdout + p.stderr


def check():
    import mr
    failures, counts = [], {}
    skills = sorted(SKILLS.glob('*/SKILL.md'))
    agents = sorted(AGENTS.glob('*.agent.md')) if AGENTS.is_dir() else []
    counts.update(skills=len(skills), agents=len(agents))
    for path in skills + agents:
        fm = frontmatter(path.read_text())
        if not fm or not fm.get('name') or not fm.get('description'):
            failures.append(f'{path}: frontmatter needs name and description')
        elif path.name == 'SKILL.md' and fm['name'] != path.parent.name:
            failures.append(f'{path}: name "{fm["name"]}" does not match its folder')
    docs = [p for p in [*SKILLS.rglob('*.md'), *agents] if '__pycache__' not in p.parts]
    known = set(mr.COMMANDS) | {'setup', 'help'}
    mentions = 0
    for path in docs:
        text = path.read_text()
        for link in re.findall(r'\]\(([^)\s]+)\)', text):
            if '://' not in link and not link.startswith(('#', 'mailto:')) and not (path.parent / link.split('#')[0]).resolve().exists():
                failures.append(f'{path.relative_to(SKILLS.parent)}: broken link {link}')
        for m in re.finditer(r'(?<![\w/.-])mr(?:\.py)? ([a-z][a-z-]*)([^`\n|]*)', text):
            cmd, rest = m.group(1), m.group(2)
            mentions += 1
            if cmd not in known:
                failures.append(f'{path.relative_to(SKILLS.parent)}: unknown command `mr {cmd}`')
                continue
            if cmd in ('setup', 'help', 'facts', 'sandbox'):
                continue
            h = help_text(cmd)
            for flag in re.findall(r'(?<![\w-])(--[a-z][a-z-]*)', rest):
                if flag not in h:
                    failures.append(f'{path.relative_to(SKILLS.parent)}: `mr {cmd}` has no option {flag}')
    counts['command mentions'] = mentions
    for d in sorted(p for p in (HUB / 'examples').iterdir() if p.is_dir()):
        md = d / 'recipe.md'
        if not md.is_file() or not re.search(r'^Status: .*Mode: (scripted|agent).*Risk: (low|medium|high)', md.read_text(), re.M):
            failures.append(f'examples/{d.name}: recipe.md needs a status line with Mode and Risk')
        if not any((d / f'check{e}').is_file() for e in ('.py', '.sh')):
            failures.append(f'examples/{d.name}: missing check.py')
    return failures, counts


if __name__ == '__main__':
    errors, counts = check()
    if errors:
        print('\n'.join(errors))
        sys.exit(1)
    print('Package OK: ' + ', '.join(f'{v} {k}' for k, v in counts.items()))
