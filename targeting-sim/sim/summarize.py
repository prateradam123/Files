import json, sys
from collections import defaultdict
rows = [r for f in sys.argv[1:] for r in json.load(open(f))]
prof = defaultdict(lambda: defaultdict(list))
for r in rows:
    prof[(r['profile'], r['strategy'])]['recall'].append(r['recall'])
    prof[(r['profile'], r['strategy'])]['precision'].append(r['precision'])
    prof[(r['profile'], r['strategy'])]['wall'].append(r['wall_s'])
    prof[(r['profile'], r['strategy'])]['mb'].append(r['mb'])
    prof[(r['profile'], r['strategy'])]['calls'].append(r['total_calls'])
    prof[(r['profile'], r['strategy'])]['perfect'].append(1 if r['recall'] == 1 and r['precision'] == 1 else 0)
print(f"{'profile':9} {'strategy':26} {'recall':>6} {'minRec':>6} {'prec':>5} {'perfect':>7} {'wall_s':>7} {'maxWall':>7} {'MB':>7} {'calls':>6}")
for (p, s), d in sorted(prof.items()):
    n = len(d['recall'])
    print(f"{p:9} {s:26} {sum(d['recall'])/n:6.3f} {min(d['recall']):6.2f} {sum(d['precision'])/n:5.2f} {sum(d['perfect']):3}/{n:<3} {sum(d['wall'])/n:7.1f} {max(d['wall']):7.1f} {sum(d['mb'])/n:7.1f} {sum(d['calls'])/n:6.0f}")
