"""Build step: turn map.json.gz into a pickle with integer line keys (fast to load)."""
import gzip
import json
import pickle
import sys

src, dst = sys.argv[1:3]
with gzip.open(src, "rt") as f:
    d = json.load(f)
d["lines"] = {f: {int(k): v for k, v in m.items()} for f, m in d["lines"].items()}
with open(dst, "wb") as f:
    pickle.dump(d, f, protocol=pickle.HIGHEST_PROTOCOL)
