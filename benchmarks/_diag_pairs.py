import base64
import json
import random
import sys
import urllib.parse
import urllib.request
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from trace2eval.select import shingle_overlap  # noqa: E402

DATA = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "_data")


def api(path):
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={"User-Agent": "probe", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def readme(repo):
    try:
        blob = api(f"/repos/{repo}/readme")
        return base64.b64decode(blob["content"]).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return f"(failed: {exc})"


print("=" * 70)
print("README of nusnlp/paraphrasing-squad")
print("=" * 70)
text = readme("nusnlp/paraphrasing-squad")
print(text[:2600])
print()


def flatten(path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for article in payload["data"]:
        for paragraph in article["paragraphs"]:
            for qa in paragraph["qas"]:
                rows.append((qa["id"], qa["question"], article.get("title", "")))
    return rows


orig = flatten(DATA / "paraphrasing-squad__dev_orig.json")
para = flatten(DATA / "paraphrasing-squad__dev_para.json")
print(f"orig rows={len(orig)}  para rows={len(para)}")

print()
print("=== pairing by POSITION instead of by id ===")
rng = random.Random(11)
for k in rng.sample(range(min(len(orig), len(para))), 6):
    a, b = orig[k][1], para[k][1]
    print(f"  sim={shingle_overlap(a, b):.3f}  same_id={orig[k][0] == para[k][0]}")
    print(f"    orig[{k}]: {a}")
    print(f"    para[{k}]: {b}")

scores = [shingle_overlap(orig[k][1], para[k][1]) for k in range(min(len(orig), len(para)))]
scores.sort()
n = len(scores)
print(f"\n  positional pairing similarity: median={scores[n//2]:.3f}  p75={scores[3*n//4]:.3f}  max={scores[-1]:.3f}")

print()
print("=" * 70)
print("searching GitHub for labelled duplicate-question datasets")
print("=" * 70)
for query in ("quora question pairs labelled", "QQP dataset", "paws paraphrase dataset"):
    try:
        data = api(f"/search/repositories?q={urllib.parse.quote(query)}&per_page=5&sort=stars")
        print(f"--- {query}: {data.get('total_count')} hits")
        for item in data.get("items", []):
            print(f"    {item['full_name']:<50} stars={item['stargazers_count']:<5} size={item['size']}KB")
    except Exception as exc:  # noqa: BLE001
        print(f"--- {query}: failed {str(exc)[:80]}")
