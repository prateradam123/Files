#!/usr/bin/env python3
"""Recipe check: is featureBuilds literally false everywhere it is set? Prints one JSON line."""
import json
import re
from pathlib import Path

KEY = 'featureBuilds'


class Duplicate(Exception):
    pass


def no_dupes(pairs):
    seen = {}
    for k, v in pairs:
        if k in seen:
            raise Duplicate(k)
        seen[k] = v
    return seen


def verdict(value, where):
    if value is True or value == 'true':
        return 'needs_change', f'{where}: {KEY} is true'
    if value is False or value == 'false':
        return 'compliant', f'{where}: {KEY} is false'
    return 'unknown', f'{where}: {KEY} is not a literal boolean ({value!r})'


findings = []
cfg = Path('config.json')
if cfg.is_file():
    try:
        data = json.loads(cfg.read_text(), object_pairs_hook=no_dupes)
        if isinstance(data, dict) and KEY in data:
            findings.append(verdict(data[KEY], 'config.json'))
    except Duplicate as e:
        findings.append(('unknown', f'config.json has duplicate key {e}; ambiguous'))
    except json.JSONDecodeError as e:
        findings.append(('unknown', f'config.json does not parse: {e}'))
props = Path('build.properties')
if props.is_file():
    rows = [l for l in props.read_text().splitlines() if re.match(r'\s*' + KEY + r'\s*[=:]', l)]
    if len(rows) > 1:
        findings.append(('unknown', f'build.properties sets {KEY} {len(rows)} times'))
    elif rows:
        findings.append(verdict(re.split(r'[=:]', rows[0], 1)[1].strip(), 'build.properties'))

if not findings:
    status, evidence = 'unknown', f'no {KEY} in config.json or build.properties; may be inherited or named differently'
elif any(s == 'unknown' for s, _ in findings):
    status, evidence = 'unknown', '; '.join(e for s, e in findings if s == 'unknown')
elif any(s == 'needs_change' for s, _ in findings):
    status, evidence = 'needs_change', '; '.join(e for _, e in findings)
else:
    status, evidence = 'compliant', '; '.join(e for _, e in findings)
print(json.dumps({'status': status, 'evidence': evidence}))
