import json
import pathlib
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from trace2eval.select import normalise  # noqa: E402

DATA = pathlib.Path(sys.argv[1])

payload = json.loads((DATA / "paraphrasing-squad__dev_orig.json").read_text(encoding="utf-8"))
texts = []
for article in payload["data"]:
    for paragraph in article["paragraphs"]:
        for qa in paragraph["qas"]:
            texts.append(qa["question"])

keys = [normalise(t) for t in texts]
counts = Counter(keys)
dupes = {k: v for k, v in counts.items() if v > 1}

print(f"rows                : {len(texts)}")
print(f"distinct normalised : {len(counts)}")
print(f"keys appearing >1   : {len(dupes)}")
print(f"extra rows (len-distinct): {len(texts) - len(counts)}")
print()
target = "by the opening of the 2008 general conference, what was the total umc membership?"
hits = [i for i, t in enumerate(texts) if t == target]
print(f"exact matches for the umc question: {hits}")
for i in hits:
    print(f"  index {i}: {texts[i]!r}")
    print(f"    normalised: {keys[i]!r}  len={len(keys[i])}")
if len(hits) >= 2:
    print(f"  equal?  keys equal = {keys[hits[0]] == keys[hits[1]]}")
    print(f"  text equal? {texts[hits[0]] == texts[hits[1]]}")
