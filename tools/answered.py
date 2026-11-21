"""Copy the fake commits that have truth answers into <out> so tools/evaluate.py can score them.

usage: python3 tools/answered.py <mut dir> [<mut dir> ...] <out dir>
"""
import json
import os
import shutil
import sys

*srcs, out = sys.argv[1:]
os.makedirs(os.path.join(out, "commits"), exist_ok=True)
for f in os.listdir(os.path.join(out, "commits")):
    os.remove(os.path.join(out, "commits", f))
answers = {}
for src in srcs:
    p = os.path.join(src, "answers.json")
    if not os.path.exists(p):
        continue
    for cid, v in json.load(open(p))["commits"].items():
        if v.get("timeout"):  # truth run hit its wall-clock cap: label unknown, leave it out
            continue
        answers[cid] = v
        shutil.copy(os.path.join(src, "commits", f"{cid}.patch"), os.path.join(out, "commits"))
json.dump({"commits": answers}, open(os.path.join(out, "answers.json"), "w"), indent=1)
shutil.copy(os.path.join(srcs[0], "tests.json"), os.path.join(out, "tests.json"))
print(len(answers), "answered")
