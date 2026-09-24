#!/usr/bin/env python3
"""Recipe apply: flip a literal featureBuilds=true to false, idempotently.

Runs with cwd = the target worktree. Prints one JSON line. Exit 0 = done (changed or already false),
exit 1 = this layout needs an agent or a person (the reason is in "notes").
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ.get('RECIPE_DIR') or str(Path(__file__).resolve().parent))
from config_edit import transform  # noqa: E402

changed, already = [], []
for name, fmt in (('config.json', 'json'), ('build.properties', 'properties')):
    p = Path(name)
    if not p.is_file():
        continue
    text = p.read_text(encoding='utf-8')
    try:
        new, did = transform(text, 'featureBuilds', fmt)
    except ValueError as e:
        absent = str(e) == 'Missing top-level property' or (
            str(e) == 'Missing or repeated property' and 'featureBuilds' not in text)
        if absent:
            continue
        print(json.dumps({'changed': False, 'notes': f'{name}: {e}'}))
        sys.exit(1)
    if did:
        p.write_text(new, encoding='utf-8')
        changed.append(name)
    else:
        already.append(name)
if changed:
    print(json.dumps({'changed': True, 'notes': 'set featureBuilds=false in ' + ', '.join(changed)}))
elif already:
    print(json.dumps({'changed': False, 'notes': 'already false in ' + ', '.join(already)}))
else:
    print(json.dumps({'changed': False, 'notes': 'featureBuilds not found; needs an agent or a person'}))
    sys.exit(1)
