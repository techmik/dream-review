#!/usr/bin/env python3
"""SessionStart reminder: name any dream-review nights not yet reviewed.

Prints one short line (added to the session's context) when the project's
dream_report.md has "## Night of" sections without a "**Reviewed ...:**"
line. Prints nothing otherwise. Never fails a session start: any error is
swallowed and it exits 0.
"""
import re
import sys
from pathlib import Path

REVIEWED_RE = re.compile(r"\*\*Reviewed\s+\d{4}-\d{2}-\d{2}[^:]*:\*\*")


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _common as c

    project_dir = c.resolve_project_dir(None)
    data_dir = c.resolve_data_dir(None)
    report = data_dir / "projects" / c.project_slug(project_dir) / c.REPORT_NAME
    if not report.is_file():
        return
    lines = report.read_text(encoding="utf-8").splitlines()
    pending, title, reviewed = [], None, False
    for line in lines + [c.SECTION_PREFIX + " <end>"]:
        if line.startswith(c.SECTION_PREFIX):
            if title and not reviewed:
                pending.append(title)
            title, reviewed = line[len("## "):].strip(), False
        elif REVIEWED_RE.search(line):
            reviewed = True
    if pending:
        names = ", ".join(pending[:5]) + (f" (+{len(pending) - 5} more)" if len(pending) > 5 else "")
        print(f"dream-review: {len(pending)} unreviewed night(s) in {report}: {names}. "
              "Offer to review them (say \"review my dreams\").")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
