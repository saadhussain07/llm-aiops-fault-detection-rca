#!/usr/bin/env python3
"""
replay.py - replay stored contexts through a second model.

Every source record stores the fused context object the agent received.
This script feeds those contexts to a chosen model through the ORIGINAL
RuntimeAnomalyAgent.analyze() - same system prompt, same five votes,
same retries, same majority logic, same history handling - changing only
the model name.

Fidelity rules, matching the source experiments:
  - one fresh agent per run, so history accumulates within a run and
    resets between runs, as main.py did (one process per run)
  - only cycles where the source agent was invoked are replayed
  - each model keeps its OWN history: identical telemetry in, but each
    sees its own previous verdicts

Progress is checkpointed after every cycle: the records so far and the
agent's exact conversation history. A stop (daily quota, failure, Ctrl+C)
resumes mid-run from the next cycle with history identical to an
uninterrupted run.

Output records use the source format, so every analysis script runs on
the replay directory unchanged.

Ablation (--strip-notes, see REPLAY_ABLATION_PROTOCOL.md):
  Removes every line of summary_for_llm whose text begins with "NOTE:"
  and changes nothing else. Only runs in which at least one context
  carries a NOTE are replayed. Output goes to replay/<model>__nonote/,
  never mixed with the unablated replay.

Usage:
  python analysis/replay.py run     <model> [--runs 1,2] [--scenario S] [--max-tokens N] [--strip-notes] [--votes K]
  python analysis/replay.py compare <model>
"""

import collections
import copy
import json
import os
import sys
import time
from datetime import datetime

from dotenv import load_dotenv
from pathlib import Path

ENV = os.environ.get("GROQ_ENV_FILE", ".env")   # never commit the key
SRC = Path("src/agents")
DATA = Path("data")
OUT = Path("replay")
N_VOTES = 5          # the deployed system's K; override with --votes
PAUSE_S = 2
RETRY_WAIT_S = 60
RETRIES = 3
ONLY_SCEN = None
MAX_TOKENS_CAP = None
STRIP_NOTES = False


def slug(model):
    return model.replace("/", "__")


def out_dir_for(model):
    d = slug(model) + ("__nonote" if STRIP_NOTES else "")
    if N_VOTES != 5:
        d += f"__k{N_VOTES}"
    return OUT / d


def strip_notes(context):
    """Return (ablated copy of context, number of NOTE lines removed).

    Only summary_for_llm changes. Numeric fields, anomaly JSON and every
    other line of the summary are left exactly as recorded."""
    ctx = copy.deepcopy(context)
    lines = ctx.get("summary_for_llm", "").split("\n")
    kept = [l for l in lines if not l.lstrip().startswith("NOTE:")]
    ctx["summary_for_llm"] = "\n".join(kept)
    return ctx, len(lines) - len(kept)


def load_source():
    runs = collections.defaultdict(list)
    for path in sorted(DATA.glob("S*.jsonl")):
        for line in path.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("llm_verdict"):
                runs[(path.stem.upper(), r["run_id"])].append(r)
    for k in runs:
        runs[k].sort(key=lambda r: r["cycle_num"])
    return runs


def done_runs(out_dir):
    done = set()
    for path in out_dir.glob("S*.jsonl"):
        for line in path.open(encoding="utf-8"):
            r = json.loads(line)
            done.add((r["scenario"], r["run_id"]))
    return done


def run_mode(model, only_runs):
    load_dotenv(ENV, override=True)
    if not os.getenv("GROQ_API_KEY"):
        sys.exit(f"GROQ_API_KEY not found at {ENV}")
    sys.path.insert(0, str(SRC))
    from runtime_anomaly_agent import RuntimeAnomalyAgent

    out_dir = out_dir_for(model)
    out_dir.mkdir(parents=True, exist_ok=True)

    runs = load_source()
    done = done_runs(out_dir)
    todo = [k for k in sorted(runs)
            if (only_runs is None or k[1] in only_runs) and k not in done
            and (ONLY_SCEN is None or k[0] == ONLY_SCEN)]
    if STRIP_NOTES:
        # Mechanical selection: a run is ablated only if at least one of
        # its agent-invoking contexts carries a NOTE line.
        with_notes = [k for k in todo
                      if any(strip_notes(r["context_object"])[1]
                             for r in runs[k])]
        for k in todo:
            if k not in with_notes:
                print(f"  skipping {k[0]} run {k[1]}: no NOTE lines")
        todo = with_notes
        print("\nABLATION: NOTE lines stripped from summary_for_llm")

    n_cycles = sum(len(runs[k]) for k in todo)
    print(f"\nModel: {model}")
    print(f"Runs to replay: {len(todo)}  ({len(done)} already complete)")
    print(f"Agent-invoking cycles: {n_cycles}  (~{n_cycles * N_VOTES} API calls)\n")

    for i, key in enumerate(todo, 1):
        scen, run_id = key
        agent = RuntimeAnomalyAgent(n_votes=N_VOTES)
        agent.model = model
        agent.conversation_history = []
        if MAX_TOKENS_CAP:
            # Free-tier per-minute output limits: cap each call's ceiling and
            # let the SDK wait out per-minute 429s (it honours retry-after)
            # instead of failing the vote. The agent's own code is unchanged.
            # A response exceeding the cap fails to parse and is recorded by
            # the agent as a schema_failure, so truncation is counted.
            agent.client = type(agent.client)(
                api_key=os.getenv("GROQ_API_KEY"), max_retries=10)
            _create = agent.client.chat.completions.create

            def _capped(*a, _create=_create, **k):
                if k.get("max_tokens", 0) > MAX_TOKENS_CAP:
                    k["max_tokens"] = MAX_TOKENS_CAP
                return _create(*a, **k)

            agent.client.chat.completions.create = _capped

        buffered = []
        done_cycles = set()
        ckpt = out_dir / f"_checkpoint_{scen}_run{run_id}.json"
        if ckpt.exists():
            state = json.loads(ckpt.read_text(encoding="utf-8"))
            buffered = state["records"]
            agent.conversation_history = state["history"]
            done_cycles = set(state["done_cycles"])
            print(f"[{i}/{len(todo)}] {scen} run {run_id}: resuming from "
                  f"checkpoint, {len(done_cycles)} of {len(runs[key])} "
                  f"cycles already done", flush=True)
        else:
            print(f"[{i}/{len(todo)}] {scen} run {run_id}: "
                  f"{len(runs[key])} cycles", flush=True)

        for src in runs[key]:
            if src["cycle_num"] in done_cycles:
                continue
            ctx, n_removed = src["context_object"], 0
            if STRIP_NOTES:
                ctx, n_removed = strip_notes(src["context_object"])
            verdict = None
            for attempt in range(1, RETRIES + 1):
                v = agent.analyze(ctx)
                if v.get("quota_exhausted"):
                    print("\n  Daily token quota exhausted.")
                    print(f"  Progress kept: {len(buffered)} cycle(s) of this "
                          f"run are checkpointed.")
                    print("  Resume later with the same command; it continues "
                          "from the next cycle.")
                    sys.exit(2)
                if v.get("verdict") != "ERROR":
                    verdict = v
                    break
                print(f"    cycle {src['cycle_num']}: all votes failed "
                      f"(attempt {attempt}/{RETRIES}), waiting {RETRY_WAIT_S}s")
                time.sleep(RETRY_WAIT_S)

            if verdict is None:
                print("  Stopping after repeated failures; progress is "
                      "checkpointed and resumes on the next invocation.")
                sys.exit(3)

            rec = dict(src)
            rec["llm_verdict"] = verdict
            rec["pipeline_status"] = "REPLAY"
            rec["replay_model"] = model
            rec["replayed_at"] = datetime.now().isoformat()
            rec["source_verdict"] = src["llm_verdict"].get("verdict")
            rec["replay_max_tokens_cap"] = MAX_TOKENS_CAP
            rec["replay_votes"] = N_VOTES
            if STRIP_NOTES:
                # context_object stays the recorded original; the text the
                # model actually saw is stored alongside it for audit.
                rec["replay_ablation"] = "strip_notes"
                rec["notes_removed"] = n_removed
                rec["ablated_summary_for_llm"] = ctx["summary_for_llm"]
            buffered.append(rec)
            done_cycles.add(src["cycle_num"])
            # Checkpoint after EVERY cycle: the records so far plus the
            # agent's exact conversation history, so a stop resumes mid-run
            # with history identical to an uninterrupted run.
            ckpt.write_text(json.dumps({
                "records": buffered,
                "history": agent.conversation_history,
                "done_cycles": sorted(done_cycles),
            }, ensure_ascii=False, default=str), encoding="utf-8")
            tag = f"  [notes removed: {n_removed}]" if STRIP_NOTES else ""
            print(f"    cycle {src['cycle_num']:>2}: {verdict.get('verdict'):<18}"
                  f" {verdict.get('vote_agreement', '')}"
                  f"   (source: {rec['source_verdict']}){tag}", flush=True)
            time.sleep(PAUSE_S)

        with (out_dir / f"{scen}.jsonl").open("a", encoding="utf-8") as f:
            for rec in buffered:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        ckpt.unlink(missing_ok=True)

    print(f"\nDone. Output: {out_dir}")


def compare_mode(model):
    runs = load_source()
    src = {(k[0], k[1], r["cycle_num"]): r["llm_verdict"].get("verdict")
           for k, recs in runs.items() for r in recs}
    out_dir = out_dir_for(model)
    if not out_dir.exists():
        sys.exit(f"No replay output at {out_dir}")

    rep = {}
    for path in out_dir.glob("S*.jsonl"):
        for line in path.open(encoding="utf-8"):
            r = json.loads(line)
            rep[(r["scenario"], r["run_id"], r["cycle_num"])] = \
                r["llm_verdict"].get("verdict")

    by = collections.defaultdict(lambda: [0, 0])
    pairs = collections.Counter()
    for k, v in rep.items():
        if k not in src:
            continue
        by[k[0]][1] += 1
        if v == src[k]:
            by[k[0]][0] += 1
        else:
            pairs[(src[k], v)] += 1

    print(f"\nVerdict agreement: {model} replay vs source (gpt-oss-120b live)\n")
    print(f"{'Scenario':<24} {'n':>5} {'agree':>7} {'rate':>7}")
    print("-" * 46)
    ta = tn = 0
    for scen in sorted(by):
        a, n = by[scen]
        ta += a
        tn += n
        print(f"{scen:<24} {n:>5} {a:>7} {100*a/n:>6.1f}%")
    if tn:
        print("-" * 46)
        print(f"{'POOLED':<24} {tn:>5} {ta:>7} {100*ta/tn:>6.1f}%")
    if pairs:
        print("\nDisagreements (source -> replay):")
        for (s, r), c in pairs.most_common():
            print(f"   {s:<20} -> {r:<20} {c}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    mode, model = sys.argv[1], sys.argv[2]
    only = None
    if "--scenario" in sys.argv:
        ONLY_SCEN = sys.argv[sys.argv.index("--scenario") + 1].upper()
    if "--votes" in sys.argv:
        N_VOTES = int(sys.argv[sys.argv.index("--votes") + 1])
    if "--strip-notes" in sys.argv:
        STRIP_NOTES = True
    if "--max-tokens" in sys.argv:
        MAX_TOKENS_CAP = int(sys.argv[sys.argv.index("--max-tokens") + 1])
    if "--runs" in sys.argv:
        only = {int(x) for x in sys.argv[sys.argv.index("--runs") + 1].split(",")}
    if mode == "run":
        run_mode(model, only)
    elif mode == "compare":
        compare_mode(model)
    else:
        sys.exit(__doc__)
