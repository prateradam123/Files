"""Replace rows for re-run strategies: python -m sim.merge base.json patch.json -> overwrites base.json"""
import json, sys
base, patch = sys.argv[1], sys.argv[2]
b, p = json.load(open(base)), json.load(open(patch))
keys = {(r["scenario"], r["profile"], r["strategy"]) for r in p}
rows = [r for r in b if (r["scenario"], r["profile"], r["strategy"]) not in keys] + p
json.dump(rows, open(base, "w"), indent=1)
print(base, len(rows))
