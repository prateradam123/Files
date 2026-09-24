#!/usr/bin/env python3
"""Narrow, idempotent true-to-false edit of one literal boolean in a JSON or .properties file.

Refuses anything it can't do exactly: duplicate keys, missing keys, non-boolean values, escaped or
continued properties. Prints the diff by default; --apply writes the file. Commits stay gated by the
rollout hooks. Recipes import `transform` (see recipes/disable-feature-builds/apply.py).
"""
import argparse
import difflib
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path


def no_duplicates(pairs):
    value = {}
    for k, v in pairs:
        if k in value:
            raise ValueError('Duplicate JSON key: ' + k)
        value[k] = v
    return value


def transform(text, key, fmt):
    """Return (new_text, changed). Raises ValueError when the layout isn't exactly supported."""
    if fmt == 'json':
        value = json.loads(text, object_pairs_hook=no_duplicates)
        if not isinstance(value, dict) or key not in value:
            raise ValueError('Missing top-level property')
        if type(value[key]) is not bool:
            raise ValueError('Expected literal boolean')
        pattern = re.compile('(' + re.escape(json.dumps(key)) + r'\s*:\s*)(true|false)\b')
        matches = list(pattern.finditer(text))
        if len(matches) != 1:
            raise ValueError('Ambiguous literal property matches')
        old = value[key]
    elif fmt == 'properties':
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', key):
            raise ValueError('Unsupported properties key')
        candidates = [line for line in text.splitlines()
                      if line.strip() and not line.lstrip().startswith(('#', '!'))
                      and re.match(r'\s*' + re.escape(key) + r'(?:\s|=|:|$)', line)]
        if len(candidates) != 1:
            raise ValueError('Missing or repeated property')
        pattern = re.compile(r'^(\s*' + re.escape(key) + r'\s*=\s*)(true|false)([ \t]*\r?)$', re.M)
        matches = list(pattern.finditer(text))
        if len(matches) != 1:
            raise ValueError('Unsupported properties value/layout')
        if '\\' in text:
            raise ValueError('Escaped properties require a format-aware implementation')
        old = matches[0].group(2) == 'true'
    else:
        raise ValueError('Unsupported format')
    if not old:
        return text, False
    m = matches[0]
    return text[:m.start(2)] + 'false' + text[m.end(2):], True


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--file', required=True)
    p.add_argument('--key', required=True)
    p.add_argument('--format', choices=['json', 'properties'], required=True)
    p.add_argument('--apply', action='store_true')
    a = p.parse_args()
    try:
        path = Path(a.file)
        raw = path.read_bytes()
        text = raw.decode('utf-8')
        new, changed = transform(text, a.key, a.format)
        diff = ''.join(difflib.unified_diff(text.splitlines(True), new.splitlines(True),
                                            fromfile=str(path), tofile=str(path)))
        if a.apply and changed:
            if path.read_bytes() != raw:
                raise ValueError('File changed during inspection')
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.config-')
            try:
                with os.fdopen(fd, 'wb') as f:
                    f.write(new.encode('utf-8'))
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp, path.stat().st_mode)
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        print(json.dumps({'changed': changed, 'applied': bool(a.apply and changed),
                          'before_sha256': hashlib.sha256(raw).hexdigest(),
                          'after_sha256': hashlib.sha256(new.encode()).hexdigest(), 'diff': diff}, indent=2))
    except (ValueError, OSError) as e:
        p.exit(2, 'BLOCKED: ' + str(e) + '\n')


if __name__ == '__main__':
    main()
