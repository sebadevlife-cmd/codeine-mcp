"""Smart command runner with heuristic log/test-output pruning.

Success (exit 0)  -> last 25 lines + summary.
Failure (exit!=0) -> first 10 lines + error-pattern lines + last 25 lines,
                     with explicit omission markers. Never dumps raw walls
                     of logs into the LLM context.
"""
from __future__ import annotations

import re
import subprocess

_SUCCESS_TAIL = 25
_FAIL_HEAD = 10
_FAIL_TAIL = 25
_FAIL_THRESHOLD = 60
_MAX_ERROR_LINES = 15

_ERROR_PATTERNS = [
    re.compile(r"Traceback", re.I),
    re.compile(r"^\s*File \"[^\"]+\", line \d+", re.M),
    re.compile(r"\bError\b\s*[:=]?", re.I),
    re.compile(r"\bException\b", re.I),
    re.compile(r"\bFAILED\b|^FAIL\b|^FAIL:", re.M | re.I),
    re.compile(r"error\[E\d+\]"),                      # rustc
    re.compile(r"^\s*-->\s+\S+:\d+:\d+", re.M),        # rustc notes
    re.compile(r"AssertionError|AssertError", re.I),
    re.compile(r"^\s*\^+\s*^", re.M),                  # caret markers
]

_OMIT = "[... {n} lines omitted ...]"


def _classify(line: str) -> bool:
    return any(p.search(line) for p in _ERROR_PATTERNS)


def prune_output(lines: list[str]) -> list[str]:
    """Heuristic prune for failing output > _FAIL_THRESHOLD lines."""
    total = len(lines)
    if total <= _FAIL_THRESHOLD:
        return lines

    head = lines[:_FAIL_HEAD]
    tail = lines[-_FAIL_TAIL:]
    head_set, tail_set = set(head), set(tail)

    errors: list[str] = []
    for line in lines[_FAIL_HEAD:-_FAIL_TAIL]:
        if len(errors) >= _MAX_ERROR_LINES:
            break
        if _classify(line) and line.strip() and line not in head_set \
                and line not in tail_set and line not in errors:
            errors.append(line.rstrip())

    out: list[str] = list(head)
    omitted_mid = total - _FAIL_HEAD - _FAIL_TAIL
    if errors:
        out.append(_OMIT.format(n=max(0, omitted_mid - len(errors))))
        out.extend(errors)
    else:
        out.append(_OMIT.format(n=omitted_mid))
    out.append("--- tail ---")
    out.extend(tail)
    return out


def run_pruned(command: str, workspace_root: str = ".", timeout: int = 30) -> str:
    """Run *command* in *workspace_root* and return a pruned, dense summary."""
    try:
        proc = subprocess.run(
            command,
            cwd=workspace_root,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, min(timeout, 600)),
        )
    except subprocess.TimeoutExpired:
        return (f"[TIMEOUT] command exceeded {timeout}s and was killed:\n"
                f"  $ {command}")
    except OSError as exc:
        return f"[ERROR] could not spawn command: {exc!r}\n  $ {command}"

    out = proc.stdout or ""
    err = proc.stderr or ""
    merged = out.rstrip("\n")
    if err.strip():
        merged = (merged + "\n[stderr]\n" + err.rstrip("\n")).strip("\n")
    lines = merged.splitlines() if merged else []

    summary = f"$ {command}\n[exit code: {proc.returncode}]"
    if proc.returncode == 0:
        tail = lines[-_SUCCESS_TAIL:]
        omitted = len(lines) - len(tail)
        parts = [summary, "[OK]"]
        if omitted > 0:
            parts.append(_OMIT.format(n=omitted))
        parts.extend(tail)
        return "\n".join(parts)

    pruned = prune_output(lines)
    return "\n".join([summary, "[FAILED - output pruned to errors + head + tail]"]
                     + pruned)
