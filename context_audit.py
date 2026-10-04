#!/usr/bin/env python3
"""context_audit — a tamper-evident edit log for CLM-style self-editing context.

The gap this fills: "Context Language Models" (arXiv:2609.37725) let a model edit its
own live context file, and their `edit_gate` only rejects edits that GROW context. The
paper's own safety section names the residual risk — *"editable context can become
another channel through which prompt injections or self-generated instructions persist
across turns"* — and proposes no defence. A hand that can keep a rule can rewrite it.

This layer does not TRY to stop that (you cannot out-think the editor from outside).
It makes it VISIBLE and PROVABLE:
  1. Every edit is diffed against the previous state.
  2. Edits that touch a PROTECTED region (instructions, operator rules) are FLAGGED.
  3. Every record is hash-chained to the one before it, anchored to a FIXED genesis
     hash, so the log itself cannot be quietly rewritten.

Run:  context_audit.py watch <ctxfile> --log <log> [--protected <patterns>]
      context_audit.py verify <log>
"""
from __future__ import annotations
import argparse, difflib, hashlib, json, os, re, sys, time

GENESIS = "0" * 64  # fixed anchor — never re-derived from current state


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "surrogatepass")).hexdigest()


# ---- protected-region detection ---------------------------------------------
# A line is "protected" if it looks like an instruction/rule the OPERATOR or the
# harness placed — the system/task prefix, a MUST/NEVER rule, a policy line. CLM's
# own mirror protects the prefix structurally; once the model can edit freely, that
# structural protection is exactly what needs auditing.
PROTECTED_PATTERNS = [
    r"^\s*\[\[\s*CTX_TURN\s+\d+\s+role\s*=\s*(system|task)",  # harness prefix turns
    r"\b(MUST|NEVER|ALWAYS|DO NOT|REQUIRED)\b",                # hard rules
    r"^\s*#\s*RULE\b", r"^\s*##\s*(Policy|Rules|Laws)\b",
]


def protected_lines(text: str) -> set[int]:
    pats = [re.compile(p, re.M | re.I) for p in PROTECTED_PATTERNS]
    out = set()
    for i, line in enumerate(text.splitlines(), 1):
        if any(p.search(line) for p in pats):
            out.add(i)
    return out


def diff_record(before: str, after: str) -> dict:
    """Unified diff + a verdict. Flags edits that removed/rewrote a protected line."""
    b_lines, a_lines = before.splitlines(), after.splitlines()
    b_prot, a_prot = protected_lines(before), protected_lines(after)
    sm = difflib.SequenceMatcher(a=b_lines, b=a_lines)
    removed_protected, added = [], 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("delete", "replace"):
            for k in range(i1 + 1, i2 + 1):
                if k in b_prot:
                    removed_protected.append({"line": b_lines[k - 1][:200], "orig_no": k})
        if tag in ("insert", "replace"):
            added += j2 - j1
    udiff = "\n".join(difflib.unified_diff(b_lines, a_lines, lineterm="", n=1))
    grew = len(after) > len(before)
    verdict = "PROTECTED_TOUCH" if removed_protected else ("GROW" if grew else "OK")
    return {
        "verdict": verdict,
        "removed_protected": removed_protected,
        "added_lines": added,
        "before_len": len(before), "after_len": len(after),
        "diff": udiff[:4000],
    }


# ---- tamper-evident log ------------------------------------------------------
def _last_chain(log_path: str) -> tuple[str, int]:
    if not os.path.exists(log_path):
        return GENESIS, 0
    last = None
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                last = json.loads(line)
    if last is None:
        return GENESIS, 0
    return last["chain"], last["seq"]


def append(log_path: str, rec: dict) -> dict:
    chain, seq = _last_chain(log_path)
    rec = dict(rec)
    rec["seq"] = seq + 1
    rec["ts"] = time.time()
    rec["prev_chain"] = chain
    rec["chain"] = _h(chain + json.dumps(rec, sort_keys=True))
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")
    return rec


def verify(log_path: str) -> tuple[bool, str]:
    """Re-walk the chain from the FIXED genesis. Any edit breaks it."""
    chain, seq = GENESIS, 0
    with open(log_path, "r", encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            # recompute the chain over the record WITHOUT its own chain field
            body = {k: v for k, v in rec.items() if k != "chain"}
            expect_prev = chain
            expect_chain = _h(chain + json.dumps(body, sort_keys=True))
            if rec.get("prev_chain") != expect_prev:
                return False, f"line {n}: prev_chain mismatch (record {rec.get('seq')})"
            if rec.get("chain") != expect_chain:
                return False, f"line {n}: chain mismatch (record {rec.get('seq')})"
            if rec.get("seq") != seq + 1:
                return False, f"line {n}: seq gap (got {rec.get('seq')}, want {seq+1})"
            chain, seq = rec["chain"], rec["seq"]
    return True, f"OK — {seq} records, chain intact from genesis"


def snapshot(log_path: str, path: str, state: dict) -> dict | None:
    """Poll one file once. Returns the new record if it changed, else None."""
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8", errors="surrogatepass") as f:
        cur = f.read()
    h = _h(cur)
    if state.get("hash") == h:
        return None
    first = "text" not in state
    prev = state.get("text", "")
    if first:
        # the FIRST sight of the file is a baseline, not an edit — never call it GROW
        rec = {"verdict": "BASELINE", "removed_protected": [], "added_lines": len(cur.splitlines()),
               "before_len": 0, "after_len": len(cur),
               "diff": "(baseline snapshot)"}
    else:
        rec = diff_record(prev, cur)
    rec["file"] = path
    rec["sha"] = h
    rec = append(log_path, rec)
    state["hash"], state["text"] = h, cur
    return rec


def main(argv=None):
    ap = argparse.ArgumentParser(prog="context_audit")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("watch"); w.add_argument("file"); w.add_argument("--log", required=True)
    w.add_argument("--interval", type=float, default=1.0); w.add_argument("--once", action="store_true")
    v = sub.add_parser("verify"); v.add_argument("log")
    a = ap.parse_args(argv)
    if a.cmd == "verify":
        ok, msg = verify(a.log); print(("PASS " if ok else "FAIL ") + msg); return 0 if ok else 1
    state: dict = {}
    while True:
        rec = snapshot(a.log, a.file, state)
        if rec:
            print(f"[{rec['seq']}] {rec['verdict']}  +{rec['added_lines']}  {rec['before_len']}->{rec['after_len']}")
            for p in rec["removed_protected"]:
                print(f"    !! PROTECTED line removed: {p['line']!r}")
        if a.once:
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
