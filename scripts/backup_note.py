#!/usr/bin/env python3
"""Back up one note before the review edits it, and check it hasn't changed.

Used only in the user-started review, once per file, right before Claude
edits (or creates) it:

    backup_note.py --source vault --path "Projects/Postgres.md" [--expect-sha 1a2b...]
    backup_note.py --source native --path feedback_testing.md

It copies the file's exact bytes to
<workspace>/backups/<UTC date>/<source>/<path> (the first copy of the day
is kept, so a day's backup is the note as it was before that day's edits),
appends a line to backups/log.jsonl, and prints the note's current sha. If
the note has changed since that first copy, this version is kept too, under
a time-stamped name.

With --expect-sha (the sha the pass saw), a different current sha means the
note changed since the pass: it exits 2 so the proposal is refreshed and
approved again. A note that doesn't exist yet is recorded as new (restoring
it means deleting it, which stays the user's call).

Backups live in dream-review's own workspace, outside every source, so a
vault sync tool or Obsidian never picks them up and the scanner never reads
them back. Nothing is ever deleted or renamed here.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as c  # noqa: E402
import _vault as v  # noqa: E402


def _fail(msg, code=1, **extra):
    print(json.dumps({"error": msg, **extra}, indent=2))
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    c.add_common_args(parser)
    parser.add_argument("--source", required=True)
    parser.add_argument("--path", required=True, help="path relative to the source")
    parser.add_argument("--expect-sha", default=None)
    args = parser.parse_args()

    project_dir = c.resolve_project_dir(args.project_dir)
    ws = c.workspace(c.resolve_data_dir(args.data_dir), project_dir)
    config, problems = v.read_config(ws)
    sources, more = v.resolve_sources(config, ws)
    if problems or more:
        return _fail("config problem", config_problems=problems + more)
    src = next((s for s in sources if s["id"] == args.source), None)
    if src is None:
        return _fail(f"unknown source '{args.source}'", sources=[s["id"] for s in sources])

    if src["type"] == "native":
        mem_dir = c.memory_dir(project_dir)
        rel = Path(args.path)
        # Native memory: top-level .md files only, as the scanner reads them.
        if len(rel.parts) != 1 or not rel.name.lower().endswith(".md"):
            return _fail("native paths are file names in the memory folder (e.g. note.md)")
        target = mem_dir / rel.name
        rel_posix = rel.name
    else:
        target = v.note_path(src, args.path)
        if target is None:
            return _fail(f"not an allowed note path in source '{src['id']}': {args.path}")
        rel_posix = Path(args.path).as_posix()

    now = datetime.now(timezone.utc)
    record = {"time": now.isoformat(), "source": src["id"], "path": rel_posix}
    if target.exists():
        if not target.is_file():
            return _fail(f"not a file: {target}")
        data = target.read_bytes()
        _, sha, _ = v.read_note(target)
        dest = ws / "backups" / now.strftime("%Y-%m-%d") / src["id"] / rel_posix
        dest.parent.mkdir(parents=True, exist_ok=True)
        kept_earlier = dest.exists()
        if kept_earlier and dest.read_bytes() != data:
            # Changed since today's first copy (another edit, or the user):
            # keep this version too, under a time-stamped name.
            dest = dest.with_name(f"{dest.stem}.{now.strftime('%H%M%S')}{dest.suffix}")
            kept_earlier = dest.exists()
        if not kept_earlier:
            tmp = dest.with_name(dest.name + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(dest)
            if dest.read_bytes() != data:
                return _fail("backup verification failed; don't edit this note", backup=str(dest))
        record.update({"existed": True, "sha": sha, "backup": str(dest), "kept_earlier_copy": kept_earlier})
    else:
        sha = None
        record.update({"existed": False, "sha": None, "backup": None})
    log = ws / "backups" / "log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    out = dict(record, target=str(target))
    if args.expect_sha is not None:
        out["matches_expected"] = sha == args.expect_sha
        if sha != args.expect_sha:
            out["action"] = ("the note changed since the pass (or was created/removed): re-read it, "
                             "refresh the proposal and ask the user again")
            print(json.dumps(out, indent=2))
            return 2
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
