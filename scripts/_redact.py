"""Secret redaction for scanned text.

A key, token or password typed or pasted into a session would otherwise flow
verbatim into the intake shards, and from there could be quoted into the
report or an approved memory. scan.py passes every turn's text and every
native memory's description and body through redact() before writing them.
Vault notes, daily notes and find.py fetches are NOT redacted: the review
applies anchored edits to those using their sha, and a redacted copy could
end up writing a marker over the real value. SKILL.md covers quoting them.

Only the value is replaced, with "[REDACTED:<kind>]"; labels like "Bearer "
or "password=" stay so the sentence still reads. counts gets the number of
replacements by kind, never the values. Order matters: specific shapes before
generic ones (sk-ant- before sk-), and the key=value rule last, so a value
already replaced isn't matched again. A false positive only hides a word.
"""
import re

PATTERNS = [
    ("private_key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
        re.DOTALL)),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("bearer_token", re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._\-~+/]{20,}=*")),
    ("assigned_secret", re.compile(
        r"(?i)\b((?:password|passwd|pwd|passphrase|secret|client[_-]?secret|api[_-]?key"
        r"|access[_-]?token|auth[_-]?token|refresh[_-]?token|token)\s*[:=]\s*[\"']?)"
        r"(?!\[REDACTED)([^\s\"'`,;]{6,})")),
]

# Kinds whose first group is a label to keep in front of the marker.
_KEEP_LABEL = {"bearer_token", "assigned_secret"}


def redact(text, counts):
    if not isinstance(text, str) or not text:
        return text
    for kind, pattern in PATTERNS:
        def _sub(m, kind=kind):
            counts[kind] = counts.get(kind, 0) + 1
            label = m.group(1) if kind in _KEEP_LABEL else ""
            return f"{label}[REDACTED:{kind}]"
        text = pattern.sub(_sub, text)
    return text
