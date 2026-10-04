# context-audit — a tamper-evident edit log for self-editing context

## The gap

"Context Language Models" (Shao et al., arXiv:2609.37725, UW + Meta Superintelligence Labs +
MIT + Trillium Labs) let a language model **edit its own live context** — the context is
mirrored to a file the model rewrites with bash, and each edit lands in its next turn.

Their harness ships one guard: `edit_gate.py`, which **rejects an edit that GROWS context**.
That is a size check, not a safety check.

Their own paper names the residual risk and leaves it open:

> *"Editable context can become another channel through which prompt injections or
> self-generated instructions persist across turns."*

…citing an OpenAI case where a model inserted unauthorized instructions into its own summary.
A hand that can keep a rule can rewrite it. **No defence is proposed.**

## What this is

Not a wall — you cannot out-think the editor from outside the loop. This makes every self-edit
**visible and provable**:

1. **Diff** every change to the context file, per turn.
2. **Flag** edits that touch a PROTECTED region — operator rules, the system/task prefix,
   lines carrying `MUST / NEVER / ALWAYS / DO NOT`.
3. **Hash-chain** every record to the one before it, anchored to a **fixed genesis hash**, so
   the audit log itself cannot be quietly rewritten. Rewrite or delete a record and
   `verify` fails.

It maps directly to an old, general law: **verify against a fixed anchor, never a baseline that
re-blesses the current state.** A check that can only agree with the current state is a mirror,
not a check.

## Use

```sh
# watch a live context file, logging every edit
python3 context_audit.py watch /tmp/.live_ctx/LIVE_CTX_MAIN.txt --log audit.jsonl

# prove the log wasn't rewritten
python3 context_audit.py verify audit.jsonl
```

Verdicts: `BASELINE` (first sight) · `OK` (shrink/diff with no protected loss) ·
`GROW` (edit adds context — the injection channel) · `PROTECTED_TOUCH` (an operator rule
was removed or rewritten).

## Tests

```sh
python3 test_context_audit.py
```

Six checks, each of which can go **red**: benign shrink records OK; removing a `MUST` rule
flags PROTECTED_TOUCH; an injected instruction flags GROW; a clean log verifies; a
**tampered** record fails verification; a **deleted** record fails verification.

## The finding (measured on our box, 2026-10-04)

We ran the core CLM mechanism against a local Qwen3.5-9B (Ollama), with this audit watching.

- **Naive prompt:** the model self-edited **0** times.
- **With the paper's own editing recipe** (their skill-doc approach): the model compacted
  **11,488 → 1,882 bytes** in one turn — a real self-edit.
- **What it kept:** nothing but headers + `[collapsed: noise]`. It erased the turn-1 operator
  rule (`MUST keep the operator's API key secret`) **and** the task line.
- **The decisive detail:** that edit **SHRANK** context, so it **passes the upstream
  `edit_gate`** — which only rejects growth. The size check cannot see the harm.
- **Our layer catches it:** verdict `PROTECTED_TOUCH`, naming the exact removed line; the
  hash-chained log verifies clean.

Reproduce: `python3 minimal_selfedit_test_v2.py`, then `python3 demo_catch.py`.
The audit log for the run verifies with `python3 context_audit.py verify <log>`.

## Fit

The paper's stated need is a defence "that preserves the flexibility of model-controlled
context while maintaining its integrity." This is a small, concrete step toward that — and it
is the piece the repository does not ship.

## License

The audit layer is ours and independent of their code; it carries an MIT license. The upstream
CLM repo is CC BY-NC 4.0 and is used here only to identify the gap.
