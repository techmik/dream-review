#!/usr/bin/env python3
"""Check every evidence quote in this run's report section against the
scanned turns.

"Every proposal quotes its evidence" is an instruction to the model; this
script is the check. It reads the scan manifest for run_id and the turn
shards, finds the report section holding this run's "Run <run_id>" line, and
checks it. mark_success.py runs the same check itself on the current report
before saving the checkpoint, and refuses while anything blocks.

What it checks (code fences inside the section are skipped as proposal text):

  * Every proposal under the headings that need evidence (New memory
    candidates, Stale / needs update, Add to note, New note, Stale in vault,
    Reconstructed session entries) has at least one "Evidence:" line.
  * Every "Evidence:" line reads, in full, as
        session <id>, <YYYY-MM-DD>T<HH:MM[:SS]>Z: "exact words"
    optionally followed by later quotes from the same session:
        / <HH:MM[:SS]>Z: "more words"
    (a later quote may give its date too; otherwise the date carries
    forward, rolling over at midnight). "(user)" before "session" or after
    a time marks that quote as the user's own words. A quote runs from its
    first to its last double quote, so quotes inside it are kept; nothing
    may follow the closing quote. Proposals get credit only for an
    Evidence line indented under them.
  * Each quote appears in one turn of that session (a full id must match
    exactly; a shorter prefix must match exactly one scanned session), and
    that same turn is within TIME_TOLERANCE of the given time and, for a
    (user) quote, is the user's. Matching ignores case, curly quotes, dash
    style (each dash character counts as one "-"), backticks, bold markers
    and spacing, and needs whole words
    (letters, digits, "_") at the edges. "..." (or "…") is the only gap:
    the pieces around it must appear in order in the same turn.
    "[REDACTED]" must line up with a scanner redaction marker in the turn.

Verdicts:
  verified          found in a turn of that session near the given time
  wrong_time       found in that session, but not near the given time
                                                                 (blocks)
  wrong_role        tagged (user), but near that time only Claude's reply
                    has the words                                (blocks)
  not_found         no turn of that session holds it            (blocks)
  too_short         fewer than 3 letters or digits to check      (blocks)
  unparsed          an Evidence line or quote the check can't read:
                    no quote, no closing quote, text after the quote, a
                    citation not in the form above, a bad or ambiguous
                    session id, an impossible date or time       (blocks)
  missing_evidence  a proposal that needs evidence has no Evidence line
                    (proposal bullets "- ", "* " or "1. " at the start of
                    a line, under a bold or "####" heading)       (blocks)

Writes the full result to quote_check.json in the workspace (for reading;
the checkpoint step doesn't trust it, it re-checks) and prints a short
ASCII summary. Exit codes: 0 = the check ran (read "blocking"); 1 = it
couldn't run (no manifest, no section for this run, two sections for it,
section already reviewed).

Usage:
    python verify_quotes.py [--project-dir P] [--data-dir D]
"""
import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as c  # noqa: E402

QUOTE_CHECK_NAME = "quote_check.json"
TIME_TOLERANCE = timedelta(minutes=2)
MIN_CONTENT_CHARS = 3
BLOCKING = ("not_found", "wrong_time", "wrong_role", "too_short", "unparsed", "missing_evidence")
# Bold sub-headings whose proposals must carry at least one Evidence line.
NEEDS_EVIDENCE = {"new memory candidates", "stale / needs update", "add to note", "new note",
                  "stale in vault", "reconstructed session entries"}

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_QUOTES = {
    ord("“"): '"', ord("”"): '"', ord("„"): '"',
    ord("‘"): "'", ord("’"): "'", ord("«"): '"', ord("»"): '"',
}
_EVIDENCE = re.compile(r"^\s*(?:[-*]\s+|\d+\.\s+)?(?:\*\*)?Evidence(?:\*\*)?\s*:(?:\*\*)?\s*(.*)$",
                       re.IGNORECASE)
# The citation in front of each quote, matched in full (anything else on the
# line is unparsed). First quote: session <id>, <date>T<time>Z [(user)]:
# A later quote: <time>Z (date optional) [(user)]:
_TIME_PART = r"(?P<h>\d{1,2}):(?P<m>\d{2})(?::(?P<s>\d{2})(?:\.\d+)?)?[Zz]"
_FIRST = re.compile(r"^\s*(?P<u1>\(user\)\s*)?session\s+(?P<sid>[^\s,]+)\s*,\s*"
                    r"(?P<date>\d{4}-\d{2}-\d{2})T" + _TIME_PART + r"\s*(?P<u2>\(user\))?\s*:\s*$",
                    re.IGNORECASE)
_NEXT = re.compile(r"^\s*(?:(?P<date>\d{4}-\d{2}-\d{2})T)?" + _TIME_PART +
                   r"\s*(?P<u2>\(user\))?\s*:\s*$", re.IGNORECASE)
# A new quote starts only where " / " after a closing quote is followed by a
# full later-quote citation and an opening quote; any other " / " is text.
_PIECE_SPLIT = re.compile(
    r'(?<=["”])\s+/\s+(?=(?:\d{4}-\d{2}-\d{2}T)?\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?[Zz]'
    r'\s*(?:\(user\)\s*)?:\s*["“])', re.IGNORECASE)
_OPEN = re.compile(r'["“]')
_SESSION_ID = re.compile(r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{8,32})$")
_REDACTED = re.compile(r"\[redacted(?::[^\]]*)?\]", re.IGNORECASE)
_GAP = re.compile(r"\.{3,}")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_BOLD_HEADING = re.compile(r"^\*\*(.+?)\*\*:?\s*$")
_SUB_HEADING = re.compile(r"^#{4,}\s+(.+?)\s*#*\s*$")
_ITEM = re.compile(r"^(?:[-*]|\d+[.)])\s+")


# ---------------------------------------------------------------- text

def norm(s):
    s = unicodedata.normalize("NFKC", s)  # also turns "…" into "..."
    s = s.translate(_DASHES).translate(_QUOTES)
    s = _REDACTED.sub("[redacted]", s)
    s = s.replace('\\"', '"').replace("`", "").replace("**", "")
    # Dash characters become "-", one for one: "--force" stays two dashes.
    s = re.sub(r"\s+", " ", s)
    return s.strip().casefold()


def fragments(quote):
    """The quote's pieces between '...' gaps, normalized."""
    out = []
    for piece in _GAP.split(norm(quote)):
        piece = piece.strip()  # whitespace only: "!enabled" must keep its "!"
        if piece:
            out.append(piece)
    return out


def _word(ch):
    return ch.isalnum() or ch == "_"


def content_chars(frags):
    return sum(ch.isalnum() for f in frags for ch in f.replace("[redacted]", ""))


def _bounded(text, frag, i):
    end = i + len(frag)
    if _word(frag[0]) and i > 0 and _word(text[i - 1]):
        return False
    if _word(frag[-1]) and end < len(text) and _word(text[end]):
        return False
    return True


def in_order(text, frags):
    """All fragments appear in order, each as whole words at its edges."""
    pos = 0
    for f in frags:
        i = text.find(f, pos)
        while i >= 0 and not _bounded(text, f, i):
            i = text.find(f, i + 1)
        if i < 0:
            return False
        pos = i + len(f)
    return True


# ---------------------------------------------------------------- report

def outside_fences(lines, start=0, end=None):
    """Yield (index, line) for lines not inside a code fence. A fence opens
    with 3+ backticks or tildes and closes only with the same character, at
    least as many, and nothing else on the line (CommonMark's rule), so a
    shorter fence inside a longer one stays inside."""
    fence = None  # (char, length) of the open fence
    for i in range(start, len(lines) if end is None else end):
        m = _FENCE.match(lines[i])
        if m:
            run, rest = m.group(1), m.group(2)
            if fence is None:
                # A backtick fence's info string can't hold a backtick, so
                # "```code```" on one line is inline code, not a fence.
                if run[0] == "~" or "`" not in rest:
                    fence = (run[0], len(run))
                    continue
            elif run[0] == fence[0] and len(run) >= fence[1] and not rest.strip():
                fence = None
                continue
        if fence is None:
            yield i, lines[i]


def run_sections(lines, run_id):
    """Every '## ' section (bounds outside fences) whose text, outside
    fences, holds 'Run <run_id>' as a word. Returns [(start, end), ...]."""
    marker = re.compile(r"\bRun\s+" + re.escape(run_id) + r"(?![\w-])")
    heads = [i for i, line in outside_fences(lines) if line.startswith("## ")]
    found = []
    for n, start in enumerate(heads):
        end = heads[n + 1] if n + 1 < len(heads) else len(lines)
        if any(marker.search(line) for _, line in outside_fences(lines, start, end)):
            found.append((start, end))
    return found


def proposals_missing_evidence(lines, start, end):
    """Proposal bullets under headings that need evidence, with no indented
    Evidence line under them before the next bullet or heading.
    Returns [(line_index, text)]."""
    missing, kind, item, has_ev = [], None, None, False

    def close():
        if item is not None and not has_ev:
            missing.append(item)

    def name(s):  # "**New note:**" / "New note:" / "New note" -> "new note"
        return s.strip().strip("*").strip().rstrip(":").strip().strip("*").strip().casefold()

    for i, line in outside_fences(lines, start + 1, end):
        sub = _SUB_HEADING.match(line)
        if line.startswith("#") and not sub:  # "## " / "### ": a new part
            close()
            kind, item, has_ev = None, None, False
            continue
        bold = _BOLD_HEADING.match(line) if line.startswith("**") else None
        if sub or bold:
            close()
            kind, item, has_ev = name((sub or bold).group(1)), None, False
            continue
        if _EVIDENCE.match(line):
            if item is not None and line[:1] in (" ", "\t"):  # indented under the item
                has_ev = True
            continue
        if _ITEM.match(line):  # a proposal starts at column 0: "- ", "* ", "1. "
            close()
            item, has_ev = ((i, line) if kind in NEEDS_EVIDENCE else None), False
    close()
    return missing


# ---------------------------------------------------------------- evidence

def _unparsed(text, detail):
    return {"session": None, "time": None, "quote": text[:160], "status": "unparsed",
            "detail": detail}


def parse_evidence(body):
    """Split one Evidence line body into quotes. Returns a list of dicts,
    each either a quote to check ('quote' set) or an 'unparsed' result."""
    out, session, prev_dt, carried_date = [], None, None, None
    for n, piece in enumerate(_PIECE_SPLIT.split(body.strip())):
        opens = [m.start() for m in _OPEN.finditer(piece)]
        closes = [i for i, ch in enumerate(piece) if ch in '"”']
        if not opens:
            out.append(_unparsed(piece, 'no quote: write session <id>, <time>Z: "exact words"'))
            continue
        q1 = opens[0]
        q2 = max((i for i in closes if i > q1), default=-1)
        if q2 < 0:
            out.append(_unparsed(piece, "the quote has no closing double quote"))
            continue
        context, quote, tail = piece[:q1], piece[q1 + 1:q2], piece[q2 + 1:]
        if tail.strip():
            out.append(_unparsed(piece, "text after the closing quote (nothing may follow it; "
                                        "a later quote goes ' / HH:MMZ: \"...\"')"))
            continue
        cite = (_FIRST if n == 0 else _NEXT).fullmatch(context)
        if not cite:
            form = ('session <id>, YYYY-MM-DDTHH:MMZ: "exact words"' if n == 0
                    else '" / HH:MMZ: "more words" (every quote needs its time)')
            out.append(_unparsed(piece, f"the citation before the quote isn't in the form {form}"))
            continue
        if n == 0:
            session = cite.group("sid").lower()
            if not _SESSION_ID.match(session):
                out.append(_unparsed(piece, f"'{session}' isn't a session id (8+ hex characters "
                                            "or the full id)"))
                session = None
                continue
        if not session:
            out.append(_unparsed(piece, "the first quote on this line couldn't be read, so this "
                                        "one has no session"))
            continue
        h, m, s = int(cite.group("h")), int(cite.group("m")), int(cite.group("s") or 0)
        if h > 23 or m > 59 or s > 59:
            out.append(_unparsed(piece, f"impossible time {cite.group('h')}:{cite.group('m')}"))
            continue
        own_date = None
        if cite.group("date"):
            try:
                own_date = datetime(*map(int, cite.group("date").split("-")), tzinfo=timezone.utc)
            except ValueError:
                out.append(_unparsed(piece, f"impossible date {cite.group('date')}"))
                continue
        day = own_date or carried_date
        if day is None:
            out.append(_unparsed(piece, "no date to carry forward to this time"))
            continue
        when = day + timedelta(hours=h, minutes=m, seconds=s)
        if own_date is None and prev_dt is not None and when < prev_dt - timedelta(hours=12):
            when += timedelta(days=1)  # a carried date crossing midnight
        prev_dt = when
        carried_date = when.replace(hour=0, minute=0, second=0)
        user = bool((cite.groupdict().get("u1") or "") + (cite.group("u2") or ""))
        out.append({"session": session, "when": when, "time": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "user": user, "quote": quote})
    return out


def load_turns(shard_files):
    """Turns from the shards, with a turn split into parts joined back."""
    raw = []
    for p in shard_files or []:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        if isinstance(data, list):
            raw.extend(t for t in data if isinstance(t, dict))
    turns, open_part = [], None
    for t in raw:
        key = (t.get("sessionId", ""), t.get("timestamp"), t.get("role"))
        part = str(t.get("part") or "")
        if part and open_part is not None and open_part["key"] == key and not part.startswith("1/"):
            open_part["text"] += t.get("text") or ""
            open_part["truncated"] = open_part["truncated"] or bool(t.get("truncated"))
            continue
        ts = c.parse_iso_utc(t.get("timestamp"))
        if ts is None:
            continue
        entry = {"key": key, "session": str(t.get("sessionId", "")).lower(), "ts": ts,
                 "role": t.get("role"), "text": t.get("text") or "",
                 "truncated": bool(t.get("truncated"))}
        turns.append(entry)
        open_part = entry if part else None
    for t in turns:
        t["text"] = norm(t["text"])
    return turns


def resolve_session(token, session_ids):
    """(session id, None) or (None, (status, detail))."""
    if len(token) == 36:
        if token in session_ids:
            return token, None
        return None, ("not_found", f"session {token} isn't in this scan")
    matches = sorted(s for s in session_ids if s.startswith(token))
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, ("not_found", f"no session in this scan starts with {token}")
    return None, ("unparsed", f"session prefix {token} matches {len(matches)} sessions: use more of the id")


def check_quote(q, turns, session_ids, max_chars):
    result = {"session": q["session"], "time": q["time"], "quote": q["quote"][:160],
              "status": None, "detail": ""}
    frags = fragments(q["quote"])
    if content_chars(frags) < MIN_CONTENT_CHARS:
        result["status"] = "too_short"
        result["detail"] = "quote more of the turn"
        return result
    sid, problem = resolve_session(q["session"], session_ids)
    if problem:
        result["status"], result["detail"] = problem
        return result
    pool = [t for t in turns if t["session"] == sid]
    when = q["when"]
    tol = TIME_TOLERANCE.total_seconds()
    near = lambda t: abs((t["ts"] - when).total_seconds()) <= tol  # noqa: E731 (every quote has a time)
    hits = [t for t in pool if in_order(t["text"], frags)]
    if hits:
        stamp = lambda ts: ", ".join(t["ts"].strftime("%Y-%m-%dT%H:%M:%SZ") for t in ts[:3])  # noqa: E731
        close = [t for t in hits if near(t)]
        if not close:
            result["status"] = "wrong_time"
            result["detail"] = "found at " + stamp(hits)
            return result
        # One turn has to match the time and, for a (user) quote, the role.
        if q["user"] and not any(t["role"] == "user" for t in close):
            result["status"] = "wrong_role"
            users = [t for t in hits if t["role"] == "user"]
            result["detail"] = ("tagged (user) but at that time only Claude's reply has these words"
                                + (f"; a user turn has them at {stamp(users)}" if users else ""))
            return result
        result["status"] = "verified"
        return result
    result["status"] = "not_found"
    result["detail"] = "no turn of that session holds these words in this order"
    if any(t["truncated"] and near(t) for t in pool):
        result["detail"] += (f"; a turn near that time was cut at {max_chars} characters, "
                             "so quote only its visible part")
    return result


def check_report(ws, manifest, report_bytes):
    """Run the whole check on this report text. Returns a dict with either
    'error' or the result fields (results, counts, blocking, summary)."""
    run_id = manifest.get("run_id")
    try:
        lines = report_bytes.decode("utf-8").splitlines()
        turns = load_turns(manifest.get("turns_shard_files"))
    except (OSError, ValueError, UnicodeDecodeError) as e:
        return {"error": f"{e.__class__.__name__}: {e}"}
    spans = run_sections(lines, run_id)
    if not spans:
        return {"error": f"the report has no section with 'Run {run_id}' yet: write it first"}
    if len(spans) > 1:
        return {"error": f"the report has {len(spans)} sections for run {run_id}; keep one"}
    start, end = spans[0]
    if any(line.startswith("**Reviewed") for _, line in outside_fences(lines, start, end)):
        return {"error": "this run's section already has a Reviewed line; "
                         "the check is for a freshly written section"}

    max_chars = c.load_config(ws).get("max_turn_chars")
    session_ids = {t["session"] for t in turns}
    results = []
    for i, line in outside_fences(lines, start + 1, end):
        m = _EVIDENCE.match(line)
        if not m:
            continue
        for q in parse_evidence(m.group(1)):
            r = q if q.get("status") == "unparsed" else check_quote(q, turns, session_ids, max_chars)
            r["line"] = i + 1
            results.append(r)
    for i, text in proposals_missing_evidence(lines, start, end):
        results.append({"session": None, "time": None, "quote": text.strip()[:160],
                        "status": "missing_evidence", "line": i + 1,
                        "detail": "this proposal needs an Evidence: line quoting the session"})
    results.sort(key=lambda r: r["line"])

    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    blocking = sum(counts.get(k, 0) for k in BLOCKING)
    quotes = len([r for r in results if r["status"] != "missing_evidence"])
    summary = f"Quote check: {counts.get('verified', 0)} of {quotes} quotes verified"
    summary += "".join(f"; {n} {k}" for k, n in sorted(counts.items()) if k != "verified")
    return {"run_id": run_id, "report_section": lines[start], "quotes": quotes, "counts": counts,
            "blocking": blocking, "summary": summary, "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    c.add_common_args(parser)
    args = parser.parse_args()
    ws = c.workspace_path(c.resolve_data_dir(args.data_dir), c.resolve_project_dir(args.project_dir))

    manifest = c.read_json(ws / c.MANIFEST_NAME)
    if not isinstance(manifest, dict) or not manifest.get("run_id"):
        print(json.dumps({"error": f"no usable scan manifest in {ws}; run scan.py first"}))
        return 1
    try:
        report_bytes = (ws / c.REPORT_NAME).read_bytes()
    except OSError as e:
        print(json.dumps({"error": f"could not read the report: {e}"}))
        return 1
    out = check_report(ws, manifest, report_bytes)
    if "error" in out:
        print(json.dumps({"error": out["error"], "run_id": manifest.get("run_id")}))
        return 1
    out["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out_file = ws / QUOTE_CHECK_NAME
    try:
        c.atomic_write(out_file, json.dumps(out, indent=2, ensure_ascii=False))
    except OSError as e:
        print(json.dumps({"error": f"could not write {out_file}: {e}"}))
        return 1

    flagged = [r for r in out["results"] if r["status"] != "verified" or r["detail"]]
    # ASCII on stdout: a quote with an emoji must not crash a legacy Windows console.
    print(json.dumps({
        "report_section": out["report_section"],
        "summary": out["summary"],
        "blocking": out["blocking"],
        "flagged": [{k: r.get(k) for k in ("line", "status", "session", "time", "quote", "detail")}
                    for r in flagged],
        "out_file": str(out_file),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
