#!/usr/bin/env python3
"""dream-review vault search: read-only, bounded, for the running pass.

The scanner copies only a compact index and the core notes of each vault
source. When a candidate fact might already live somewhere else in a vault,
the pass asks this script instead of opening the vault itself, so an
unattended pass never needs a permission rule for the vault.

  Search:  find.py --terms postgres "database choice" [--source vault] [--max-results 20]
           Literal, case-insensitive. Score per term: in the path 3, in a
           heading 2, in the body 1 per hit (up to 5). Writes a results shard
           (path, sha, score, up to 2 snippets) to this run's intake folder.
  Fetch:   find.py --fetch vault:Projects/Postgres.md [...]
           Writes those notes in full (split into parts if long).

Only the pass that holds the lock can use it, and each pass has a cap on
searches and fetched notes (config max_searches / max_fetches). Nothing in
a vault is written, ever. Excluded folders and paths outside a vault can't
be searched or fetched.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as c  # noqa: E402
import _vault as v  # noqa: E402
from _shards import write_shards  # noqa: E402

USAGE_NAME = "find_usage.json"
SNIPPET_CHARS = 240
MAX_RESULTS_CAP = 50


def _fail(msg, code=1, **extra):
    print(json.dumps({"error": msg, **extra}, indent=2))
    return code


def _snippets(text, terms):
    out, low = [], text.lower()
    for t in terms:
        i = low.find(t)
        if i >= 0:
            a = max(0, i - SNIPPET_CHARS // 2)
            out.append(re.sub(r"\s+", " ", text[a:a + SNIPPET_CHARS]).strip())
        if len(out) >= 2:
            break
    return out


def _score(rel, heads, text, terms):
    score, hit_terms = 0, 0
    low_rel, low_heads, low_text = rel.lower(), " ".join(heads).lower(), text.lower()
    for t in terms:
        s = (3 if t in low_rel else 0) + (2 if t in low_heads else 0) + min(low_text.count(t), 5)
        if s:
            hit_terms += 1
        score += s
    return score, hit_terms


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    c.add_common_args(parser)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--terms", nargs="+")
    group.add_argument("--fetch", nargs="+", metavar="SOURCE:PATH")
    parser.add_argument("--source", default=None, help="search one vault source only")
    parser.add_argument("--max-results", type=int, default=20)
    args = parser.parse_args()

    project_dir = c.resolve_project_dir(args.project_dir)
    ws = c.workspace_path(c.resolve_data_dir(args.data_dir), project_dir)
    manifest = c.read_json(ws / c.MANIFEST_NAME)
    if not isinstance(manifest, dict) or not manifest.get("run_id"):
        return _fail("no scan manifest; run scan.py first")
    run_id = manifest["run_id"]
    lock = c.read_lock(ws)
    if not isinstance(lock, dict) or lock.get("run_id") != run_id:
        return _fail("this scan doesn't hold the pass lock; find.py only serves the running pass")
    intake = ws / "intake" / run_id
    if not intake.is_dir():
        return _fail(f"intake folder missing: {intake}")

    config, problems = v.read_config(ws)
    sources, more = v.resolve_sources(config, ws)
    if problems or more:
        return _fail("config problem", config_problems=problems + more)
    vaults = {s["id"]: s for s in sources if s["type"] == "vault"}
    if not vaults:
        return _fail("no vault sources configured")

    usage = c.read_json(ws / USAGE_NAME)
    if not isinstance(usage, dict) or usage.get("run_id") != run_id:
        usage = {"run_id": run_id, "searches": 0, "fetches": 0, "files": 0}

    if args.terms:
        if usage["searches"] >= config["max_searches"]:
            return _fail("search limit reached for this pass", limit=config["max_searches"])
        terms = [t.strip().lower() for t in args.terms if t.strip()]
        if not terms:
            return _fail("no search terms")
        if args.source and args.source not in vaults:
            return _fail(f"unknown vault source '{args.source}'", sources=sorted(vaults))
        targets = [vaults[args.source]] if args.source else list(vaults.values())
        limit = max(1, min(args.max_results, MAX_RESULTS_CAP))
        hits, searched, unreadable = [], 0, []
        for src in targets:
            for rel, path in v.iter_notes(src, ws):
                text, sha, err = v.read_note(path)
                if err:
                    unreadable.append(f"{src['id']}:{rel}: {err}")
                    continue
                searched += 1
                heads = v.headings(text)
                score, n_terms = _score(rel, heads, text, terms)
                if score:
                    hits.append({"source": src["id"], "path": rel, "sha": sha, "score": score,
                                 "terms_matched": n_terms, "headings": heads[:5],
                                 "snippets": _snippets(text, terms)})
        hits.sort(key=lambda h: (-h["terms_matched"], -h["score"], h["source"], h["path"]))
        shown = hits[:limit]
        usage["searches"] += 1
        usage["files"] += 1
        files = write_shards(shown, intake, f"search{usage['files']:02d}", "path")
        c.atomic_write(ws / USAGE_NAME, json.dumps(usage))
        print(json.dumps({
            "terms": terms, "sources": [s["id"] for s in targets],
            "notes_searched": searched, "matches": len(hits), "shown": len(shown),
            "results_files": files, "unreadable": unreadable,
            "searches_used": usage["searches"], "searches_limit": config["max_searches"],
            "coverage": f"literal search of {searched} notes; a paraphrase may not match",
        }, indent=2))
        return 0

    # Fetch
    wanted = []
    for item in args.fetch:
        sid, sep, rel = item.partition(":")
        if not sep or sid not in vaults:
            return _fail(f"use SOURCE:PATH with a configured vault source ({item})", sources=sorted(vaults))
        path = v.note_path(vaults[sid], rel)
        if path is None or not path.is_file():
            return _fail(f"not an included note in source '{sid}': {rel}")
        wanted.append((sid, Path(rel).as_posix(), path))
    if usage["fetches"] + len(wanted) > config["max_fetches"]:
        return _fail("fetch limit reached for this pass", limit=config["max_fetches"],
                     used=usage["fetches"])
    items, unreadable = [], []
    for sid, rel, path in wanted:
        text, sha, err = v.read_note(path)
        if err:
            unreadable.append(f"{sid}:{rel}: {err}")
            continue
        items.append({"source": sid, "path": rel, "sha": sha, "text": text})
    usage["fetches"] += len(wanted)
    usage["files"] += 1
    files = write_shards(items, intake, f"fetch{usage['files']:02d}", "text")
    c.atomic_write(ws / USAGE_NAME, json.dumps(usage))
    print(json.dumps({"fetched": [f"{i['source']}:{i['path']}" for i in items], "files": files,
                      "unreadable": unreadable, "fetches_used": usage["fetches"],
                      "fetches_limit": config["max_fetches"]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
