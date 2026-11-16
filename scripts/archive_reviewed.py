#!/usr/bin/env python3
"""Move reviewed, aged-out sections from dream_report.md to dream_report_archive.md.

A "## Night of ..." section moves only if it carries a
"**Reviewed YYYY-MM-DD:**" line dated at least --threshold-days ago
(default: the workspace config's archive_threshold_days, 7). A section with
no Reviewed line never moves, at any age.

Nothing is lost on a crash: the archive copy is written and flushed first,
then the live report is replaced in one step (temp file + rename). If a crash
lands between those two steps, the next run sees the sections already in the
archive (by their header line) and doesn't add them twice.

It won't run while a pass holds the workspace lock.

Usage:
    python archive_reviewed.py [--project-dir P] [--data-dir D]
                               [--threshold-days N] [--dry-run]
"""
import argparse
import datetime
import os
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as c  # noqa: E402

REVIEWED_RE = re.compile(r"\*\*Reviewed\s+(\d{4}-\d{2}-\d{2})[^:]*:\*\*")


def split_sections(text, prefix):
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.startswith(prefix)]
    if not starts:
        return text, []
    preamble = "".join(lines[: starts[0]])
    sections = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        section = "".join(lines[start:end])
        if not section.endswith("\n"):
            section += "\n"
        sections.append(section)
    return preamble, sections


def reviewed_date(section):
    m = REVIEWED_RE.search(section)
    if not m:
        return None
    try:
        return datetime.date.fromisoformat(m.group(1))
    except ValueError:
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    c.add_common_args(parser)
    parser.add_argument("--threshold-days", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    ws = c.workspace(c.resolve_data_dir(args.data_dir), c.resolve_project_dir(args.project_dir))
    threshold = args.threshold_days
    if threshold is None:
        threshold = int(c.load_config(ws)["archive_threshold_days"])
    report, archive = ws / c.REPORT_NAME, ws / c.ARCHIVE_NAME

    if not report.is_file():
        print(f"No report yet at {report}. Nothing to archive.")
        return 0

    run_id = "archive-" + uuid.uuid4().hex[:8]
    if not args.dry_run:
        ok, holder = c.acquire_lock(ws, run_id)
        if not ok:
            print(f"A pass is running (lock {holder.get('run_id')}); not archiving now. Nothing changed.")
            return 0
    try:
        text = report.read_text(encoding="utf-8")
        preamble, sections = split_sections(text, c.SECTION_PREFIX)
        today = datetime.date.today()
        kept, moved = [], []
        for s in sections:
            d = reviewed_date(s)
            (moved if d is not None and (today - d).days >= threshold else kept).append(s)

        print(f"Sections: {len(sections)}; to archive (reviewed >= {threshold} days ago): "
              f"{len(moved)}; staying: {len(kept)}")
        if not moved:
            print("Nothing to archive. No files changed.")
            return 0
        if args.dry_run:
            print("--dry-run, nothing written. Would archive:")
            for s in moved:
                print("  - " + s.splitlines()[0])
            return 0

        existing = archive.read_text(encoding="utf-8") if archive.is_file() else ""
        existing_headers = {line for line in existing.splitlines() if line.startswith(c.SECTION_PREFIX)}
        to_append = [s for s in moved if s.splitlines()[0] not in existing_headers]
        try:
            with archive.open("a", encoding="utf-8") as f:
                if not existing:
                    f.write("# Dream Report Archive\n\n")
                elif not existing.endswith("\n"):
                    f.write("\n")
                f.write("".join(to_append))
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            print(f"ERROR writing {archive}: {e}. Live report left unchanged; nothing lost.")
            return 1
        c.atomic_write(report, preamble + "".join(kept))
        print(f"Archived {len(moved)} section(s) to {archive}"
              + (f" ({len(moved) - len(to_append)} were already there)" if len(to_append) < len(moved) else ""))
        return 0
    finally:
        if not args.dry_run:
            c.release_lock(ws, run_id)


if __name__ == "__main__":
    sys.exit(main())
