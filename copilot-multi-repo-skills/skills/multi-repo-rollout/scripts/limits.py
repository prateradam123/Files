#!/usr/bin/env python3
"""Shared limits, shared back-off, and timing. Used by every helper that touches the network or the CPU.

Why cross-process: the coordinator and several subagents each start their own helper processes. A thread
pool per process would multiply (3 subagents x `--workers 3` = 9 builds). These limits are file locks in
a shared folder, so the total across all processes on this machine stays at the configured size.

  slot(pool)      counting semaphore: at most N holders of `git:<host>`, `build`, `api:<host>` at once
  throttle(pool)  token bucket: sustained requests per second, shared; a 429 pauses every process
  retry(fn)       exponential back-off with jitter for transient network errors only
  timed(...)      records seconds of work and seconds waiting for a slot to <RUN>/timings.jsonl

Sizes come from `limits` in ~/.multi-repo/config.json, with safe defaults.
"""
import contextlib
import json
import os
import random
import re
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
DEFAULTS = {
    'git': 6,              # concurrent clone/fetch/ls-remote per SCM host
    'build': max(1, (os.cpu_count() or 2) // 2),
    'api': 4,              # concurrent REST calls per SCM host
    'api_rate': 4.0,       # sustained REST requests per second per host (Bitbucket DC default refill is 5)
    'api_burst': 20,
    'write_interval': 1.0,  # seconds between content-creating calls (PRs, comments) per host
}
TRANSIENT = re.compile(r'Connection reset|timed out|Timeout|early EOF|RPC failed|remote end hung up|'
                       r'\b(429|500|502|503|504)\b|Too Many Requests|Service Unavailable|temporarily|'
                       r'Could not read from remote repository\.\s*$', re.I)


def config():
    cfg = dict(DEFAULTS)
    cfg.update(common.config().get('limits', {}))
    p = os.environ.get('ROLLOUT_LIMITS')  # tests and one-off overrides
    if p and Path(p).is_file():
        cfg.update(json.loads(Path(p).read_text()))
    return cfg


def size_of(pool):
    cfg = config()
    return int(cfg.get(pool, cfg.get(pool.split(':')[0], 1)))


def lock_dir():
    d = Path(os.environ.get('ROLLOUT_LOCK_DIR') or common.data_home() / 'locks')
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pool_dir(pool):
    d = lock_dir() / re.sub(r'[^A-Za-z0-9_.-]+', '_', pool)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _try_lock(f):
    try:
        if fcntl:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            f.seek(0)  # Windows locks a byte range at the current position; lock and unlock must match
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _lock(f):
    if fcntl:
        fcntl.flock(f, fcntl.LOCK_EX)
    else:
        while not _try_lock(f):
            time.sleep(0.05)


def _unlock(f):
    if fcntl:
        fcntl.flock(f, fcntl.LOCK_UN)
    else:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


class Held:
    def __init__(self, pool):
        self.pool, self.wait = pool, 0.0


@contextlib.contextmanager
def slot(pool, size=None):
    """Hold one of `size` slots of `pool` across all processes. The OS drops locks of dead processes."""
    size = size or size_of(pool)
    d, h, start = _pool_dir(pool), Held(pool), time.monotonic()
    while True:
        for i in range(size):
            f = open(d / f'{i}.lock', 'a+')
            if _try_lock(f):
                h.wait = time.monotonic() - start
                try:
                    yield h
                finally:
                    _unlock(f)
                    f.close()
                return
            f.close()
        time.sleep(0.05 + random.random() * 0.1)


def throttle(pool, rate=None, burst=None):
    """Take one token from a shared bucket; sleep until one is available. Returns seconds waited."""
    cfg = config()
    rate = float(rate or cfg['api_rate'])
    burst = float(burst or cfg['api_burst'])
    path, waited = _pool_dir(pool) / 'bucket.json', 0.0
    while True:
        with open(path, 'a+') as f:
            _lock(f)
            try:
                f.seek(0)
                raw = f.read()
                st = json.loads(raw) if raw.strip() else {'tokens': burst, 't': time.time(), 'hold': 0}
                t = time.time()
                st['tokens'] = min(burst, st['tokens'] + (t - st['t']) * rate)
                st['t'] = t
                pause = max(st.get('hold', 0) - t, 0)
                if not pause and st['tokens'] >= 1:
                    st['tokens'] -= 1
                    pause = 0
                else:
                    pause = max(pause, (1 - st['tokens']) / rate)
                f.seek(0)
                f.truncate()
                f.write(json.dumps(st))
            finally:
                _unlock(f)
        if not pause:
            return waited
        time.sleep(pause)
        waited += pause


def hold(pool, seconds):
    """A server said slow down (429 / Retry-After): pause this pool for every process."""
    path = _pool_dir(pool) / 'bucket.json'
    with open(path, 'a+') as f:
        _lock(f)
        try:
            f.seek(0)
            raw = f.read()
            st = json.loads(raw) if raw.strip() else {'tokens': 0, 't': time.time(), 'hold': 0}
            st['hold'] = max(st.get('hold', 0), time.time() + seconds)
            st['tokens'] = 0
            f.seek(0)
            f.truncate()
            f.write(json.dumps(st))
        finally:
            _unlock(f)


def transient(message):
    return bool(TRANSIENT.search(str(message)))


def retry(fn, tries=4, base=1.0, is_transient=transient):
    """Retry only what can succeed on retry. Auth errors, missing repos and 404s fail at once."""
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - classification decides
            if i == tries - 1 or not is_transient(e):
                raise
            time.sleep(base * 2 ** i + random.uniform(0, base))


def net_env():
    """Git must never wait for a password prompt or pull LFS/submodule content during discovery:
    a hung prompt holds a slot forever. SSH gets BatchMode unless the user configured their own command."""
    global _SSH_CONFIGURED
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_LFS_SKIP_SMUDGE='1', GCM_INTERACTIVE='never')
    if _SSH_CONFIGURED is None:  # one lookup per process, not one per network call
        _SSH_CONFIGURED = bool(os.popen('git config --global --get core.sshCommand').read().strip())
    if not env.get('GIT_SSH_COMMAND') and not _SSH_CONFIGURED:
        env['GIT_SSH_COMMAND'] = 'ssh -o BatchMode=yes -o ConnectTimeout=20'
    return env


_SSH_CONFIGURED = None


# ------------------------------------------------------------------ timing

def record(run, stage, seconds, wait=0.0, **fields):
    if not run:
        return
    line = json.dumps({'at': time.time(), 'stage': stage, 'seconds': round(seconds, 3),
                       'wait': round(wait, 3), **fields}) + '\n'
    fd = os.open(Path(run) / 'timings.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode())  # one small O_APPEND write per line: safe across processes
    finally:
        os.close(fd)


@contextlib.contextmanager
def timed(run, stage, pool=None, **fields):
    """Time a stage. With `pool`, also hold a slot and record the wait separately from the work."""
    info = {'wait': 0.0}
    t0, failed = time.monotonic(), False
    try:
        if pool:
            with slot(pool) as h:
                info['wait'] = h.wait
                yield info
        else:
            yield info
    except BaseException:
        failed = True  # failures cost time too; the report should show them
        raise
    finally:
        record(run, stage, time.monotonic() - t0 - info['wait'], info['wait'], pool=pool,
               **({'failed': True} if failed else {}), **fields)


def pct(values, p):
    if not values:
        return 0.0
    v = sorted(values)
    return v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))]


def summary(run):
    """Per stage: count, p50, p95, total. Per pool: total wait. The bottleneck is the pool with the most
    waiting; with no waiting, it's the stage with the most total time."""
    p = Path(run) / 'timings.jsonl'
    rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.is_file() else []
    stages, pools = {}, {}
    for r in rows:
        stages.setdefault(r['stage'], []).append(r)
        if r.get('pool'):
            pools[r['pool'].split(':')[0]] = pools.get(r['pool'].split(':')[0], 0) + r.get('wait', 0)
    table = {k: {'n': len(v), 'failed': sum(bool(x.get('failed')) for x in v), 'p50': pct([x['seconds'] for x in v], 50), 'p95': pct([x['seconds'] for x in v], 95),
                 'total': round(sum(x['seconds'] for x in v), 1),
                 'cached': sum(bool(x.get('cached')) for x in v)} for k, v in stages.items()}
    busiest_pool = max(pools.items(), key=lambda kv: kv[1], default=(None, 0))
    busiest_stage = max(table.items(), key=lambda kv: kv[1]['total'], default=(None, {}))[0]
    bottleneck = (f'waiting for `{busiest_pool[0]}` slots ({busiest_pool[1]:.0f}s total): raise that limit if the '
                  'server/CPU has headroom' if busiest_pool[1] >= 1 else
                  f'`{busiest_stage}` work itself (no meaningful slot waits)' if busiest_stage else None)
    return {'stages': table, 'pool_wait': {k: round(v, 1) for k, v in pools.items()}, 'bottleneck': bottleneck}
