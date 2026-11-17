#!/usr/bin/env python3
"""Finish a pass: save the checkpoint and release the pass lock.

Run it only after the report for that scan has been written. Marking early
would let a failed pass skip its material for good, so it checks: the report
must already contain this run's "Run <run_id>" line, or nothing is saved.

It reads window_end and run_id from the scan manifest, so the command line
stays the same every run (easy to allowlist), and it only acts if that run
still holds the lock (so an overlapping pass can't save someone else's
checkpoint).

If the scan couldn't read a transcript ("complete": false), the checkpoint
is NOT moved: the next run retries the same window. Pass --accept-incomplete
only when the user has decided to skip those files.

--abandon releases this run's lock without saving a checkpoint (use it when
the pass fails before the report is written).

--heartbeat only resets the lock's age (this run must still hold it), so a
long pass isn't mistaken for a dead one after two hours. It saves nothing.

Usage:
    python mark_success.py [--project-dir P] [--data-dir D]
                           [--accept-incomplete | --abandon | --heartbeat]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as c  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    c.add_common_args(parser)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--accept-incomplete", action="store_true")
    group.add_argument("--abandon", action="store_true")
    group.add_argument("--heartbeat", action="store_true")
    args = parser.parse_args()
    ws = c.workspace(c.resolve_data_dir(args.data_dir), c.resolve_project_dir(args.project_dir))

    manifest = c.read_json(ws / c.MANIFEST_NAME)
    if not isinstance(manifest, dict) or not manifest.get("window_end") or not manifest.get("run_id"):
        print(json.dumps({"error": f"no usable scan manifest in {ws}; run scan.py first"}))
        return 1
    run_id = manifest["run_id"]
    lock = c.read_json(ws / c.LOCK_NAME)
    if not isinstance(lock, dict) or lock.get("run_id") != run_id:
        print(json.dumps({"error": "this scan no longer holds the pass lock (another pass "
                                   "started, or the lock went stale); checkpoint NOT saved",
                          "run_id": run_id}))
        return 1

    if args.heartbeat:
        if not c.refresh_lock(ws, run_id):
            print(json.dumps({"error": "lock not refreshed (no longer held by this run)", "run_id": run_id}))
            return 1
        print(json.dumps({"heartbeat": run_id}))
        return 0

    if args.abandon:
        c.release_lock(ws, run_id)
        print(json.dumps({"abandoned": run_id, "checkpoint": "unchanged"}))
        return 0

    # Proof the report was written: it must hold this run's "Run <run_id>" line.
    report = ws / c.REPORT_NAME
    try:
        has_section = f"Run {run_id}" in report.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        has_section = False
    if not has_section:
        print(json.dumps({"error": f"{report} has no section for run {run_id} yet; checkpoint NOT saved. "
                                   "Write the report first and run this again, or run it with --abandon "
                                   "if the pass can't finish.", "run_id": run_id}))
        return 1

    if not manifest.get("complete", False) and not args.accept_incomplete:
        c.release_lock(ws, run_id)
        print(json.dumps({"checkpoint": "unchanged",
                          "reason": "scan was incomplete (unreadable transcripts or memory files); "
                                    "the next run retries this window",
                          "unreadable": manifest.get("skipped", {}).get("unreadable_transcripts", [])
                                        + manifest.get("skipped", {}).get("unreadable_memories", [])}))
        return 0

    state_file = ws / c.STATE_NAME
    c.atomic_write(state_file, json.dumps(
        {"last_successful_scan_through": manifest["window_end"], "run_id": run_id}, indent=2))
    c.release_lock(ws, run_id)
    print(json.dumps({"last_successful_scan_through": manifest["window_end"],
                      "state_file": str(state_file)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
