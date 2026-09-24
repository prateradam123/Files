#!/usr/bin/env python3
"""Per-repo facts that every run would otherwise rediscover: build command, real dev branch,
PR template, reviewers, quirks. One Markdown file per repo in ~/.multi-repo/repo-facts/.
Reuse them; refresh a fact when a check based on it fails or it is older than your team's limit.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import data_home, read_json, require, slug  # noqa: E402

START = '<!-- facts: managed by scripts/repo_facts.py; values may be edited by hand -->'
END = '<!-- /facts -->'
KNOWN = ['build', 'test', 'dev_branch', 'branch_rules', 'pr_target', 'pr_template', 'pr_title_format',
         'reviewers', 'build_tool', 'java_version', 'verified']


def facts_dir():
    return Path(os.environ.get('REPO_FACTS_DIR') or data_home() / 'repo-facts')


def path_for(repo):
    return facts_dir() / (slug(repo, 80) + '.md')


def parse(text):
    facts, inside = {}, False
    for line in text.splitlines():
        if line.startswith('<!-- facts'):
            inside = True
            continue
        if line.startswith(END):
            inside = False
            continue
        m = re.match(r'^- ([a-z_]+): (.*)$', line) if inside else None
        if m:
            facts[m.group(1)] = m.group(2).strip().strip('`')
    return facts


def get(repo):
    p = path_for(repo)
    return parse(p.read_text()) if p.is_file() else {}


def render(repo, facts, notes):
    keys = [k for k in KNOWN if k in facts] + sorted(k for k in facts if k not in KNOWN)
    lines = [f'# {repo}', '', START]
    for k in keys:
        v = facts[k]
        lines.append(f'- {k}: `{v}`' if k in {'build', 'test'} else f'- {k}: {v}')
    return '\n'.join(lines + [END, '', notes.strip() or '## Notes']) + '\n'


def merge(repo, new, run_id=None):
    require(isinstance(new, dict), 'Facts must be a JSON object of strings')
    p = path_for(repo)
    text = p.read_text() if p.is_file() else ''
    facts = parse(text)
    notes = text.split(END, 1)[1] if END in text else ''
    changed = {k: str(v) for k, v in new.items() if v not in (None, '') and facts.get(k) != str(v)}
    facts.update(changed)
    facts['verified'] = datetime.now(timezone.utc).date().isoformat() + (f' (run {run_id})' if run_id else '')
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render(repo, facts, notes))
    return {'repo': repo, 'file': str(p), 'changed': changed}


def merge_run(run):
    out = []
    manifest = read_json(Path(run) / 'manifest.json')
    for f in sorted((Path(run) / 'discovery').glob('*.json')):
        d = read_json(f)
        if d.get('facts'):
            out.append(merge(d['repo'], d['facts'], manifest['run_id']))
    return out


def listing(stale_days=None):
    rows = []
    for p in sorted(facts_dir().glob('*.md')):
        f = parse(p.read_text())
        repo = p.read_text().splitlines()[0].lstrip('# ').strip()
        m = re.match(r'(\d{4}-\d{2}-\d{2})', f.get('verified', ''))
        age = (datetime.now(timezone.utc).date() - datetime.fromisoformat(m.group(1)).date()).days if m else None
        if stale_days is None or age is None or age > stale_days:
            rows.append({'repo': repo, 'age_days': age, 'file': str(p)})
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    s = ap.add_subparsers(dest='cmd', required=True)
    s.add_parser('show', help='Print facts for a repo').add_argument('--repo', required=True)
    q = s.add_parser('merge', help='Merge a JSON object of facts into a repo file')
    q.add_argument('--repo', required=True)
    q.add_argument('--file', required=True)
    q.add_argument('--run-id')
    s.add_parser('merge-run', help='Merge facts recorded in a run\'s discovery files').add_argument('--run', required=True)
    q = s.add_parser('set', help='Set facts on one or many repos: facts set --repo A --repo B dev_branch=develop')
    q.add_argument('--repo', action='append', required=True)
    q.add_argument('pairs', nargs='+', help='key=value')
    s.add_parser('list', help='List repos (optionally only stale ones)').add_argument('--stale-days', type=int)
    a = ap.parse_args()
    try:
        if a.cmd == 'show':
            o = {'repo': a.repo, 'facts': get(a.repo), 'file': str(path_for(a.repo))}
        elif a.cmd == 'merge':
            o = merge(a.repo, read_json(a.file), a.run_id)
        elif a.cmd == 'set':
            kv = dict(x.split('=', 1) for x in a.pairs)
            require(all(kv.values()), 'Use key=value pairs')
            o = [merge(r, kv) for r in a.repo]
        elif a.cmd == 'merge-run':
            o = merge_run(a.run)
        else:
            o = listing(a.stale_days)
        print(json.dumps(o, indent=2))
    except (ValueError, OSError) as e:
        ap.exit(2, 'BLOCKED: ' + str(e) + '\n')


if __name__ == '__main__':
    main()
