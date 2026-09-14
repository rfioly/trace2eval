import json
import sys
import urllib.request

REPOS = [
    "Tanwar-12/QUORA-QUESTION-PAIR-",
    "ishritam/Quora-question-pair-similarity",
    "google-research-datasets/paws",
    "seduerr91/pawraphrase_public",
]


def api(path):
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={"User-Agent": "probe", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


for repo in REPOS:
    print(f"===== {repo}")
    try:
        info = api(f"/repos/{repo}")
        tree = api(f"/repos/{repo}/git/trees/{info['default_branch']}?recursive=1")
        blobs = [e for e in tree["tree"] if e["type"] == "blob"]
        blobs.sort(key=lambda e: -e.get("size", 0))
        for entry in blobs[:12]:
            size_kb = entry.get("size", 0) / 1024
            print(f"    {entry['path']:<66} {size_kb:9.1f} KB")
        total = sum(e.get("size", 0) for e in blobs) / 1024
        print(f"    ({len(blobs)} blobs, {total:.0f} KB total)")
    except Exception as exc:  # noqa: BLE001
        print(f"    failed: {str(exc)[:130]}")
    print()
