#!/usr/bin/env python3
"""Commit the auto-memory folder before approved proposals are applied.

Only acts if the memory folder is the top of its own git repository: it
never runs `git init`, never adds a remote and never pushes. If the folder
isn't a repo it says so and exits 0 (the README shows how to make it one).

Exit codes: 0 = snapshot taken, nothing to commit, or no repo; 1 = a git
step failed or timed out. On 1, the review must stop and ask the user.

Git runs with prompts turned off and a time limit, so a signing passphrase
or credential prompt makes it fail instead of hanging. Your own git hooks
still run (it doesn't bypass them).

Usage:
    python snapshot_memory.py [--project-dir P] [--data-dir D]
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as c  # noqa: E402

TIMEOUT = 60


def git(repo, *args):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_EDITOR="true")
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          timeout=TIMEOUT, stdin=subprocess.DEVNULL, env=env)


def fail(step, result=None, err=None):
    detail = err or (result.stderr.strip() or result.stdout.strip() if result else "")
    print(f"Memory snapshot FAILED at `git {step}`: {detail or 'no output'}. "
          "Stop and ask the user before applying anything.")
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    c.add_common_args(parser)
    args = parser.parse_args()
    mem = c.memory_dir(c.resolve_project_dir(args.project_dir))

    if not (mem / ".git").exists():
        print(f"Memory folder {mem} is not a git repo; no snapshot taken.")
        return 0
    try:
        for step in (["add", "-A"],):
            r = git(mem, *step)
            if r.returncode:
                return fail(" ".join(step), r)
        r = git(mem, "status", "--porcelain")
        if r.returncode:
            return fail("status", r)
        if not r.stdout.strip():
            print("Memory snapshot: nothing changed since the last commit.")
            return 0
        r = git(mem, "commit", "-q", "-m", "dream-review: snapshot before applying proposals")
        if r.returncode:
            return fail("commit", r)
        r = git(mem, "log", "--oneline", "-1")
        print("Memory snapshot:", r.stdout.strip())
        return 0
    except subprocess.TimeoutExpired:
        return fail("(timed out)", err=f"no answer within {TIMEOUT}s")
    except FileNotFoundError:
        return fail("(not found)", err="git is not on PATH")


if __name__ == "__main__":
    sys.exit(main())
