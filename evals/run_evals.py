"""Step 8 - Run the golden dataset through the Step 7 agent and score every answer.

    python evals/run_evals.py                          # all cases
    python evals/run_evals.py --only lambert-factors,emdr
    python evals/run_evals.py --limit 5 --label smoke
    python evals/run_evals.py --no-verify --label no-fact-check   # ablation
    python evals/run_evals.py --retrieval-mode keyword            # ablation
    python evals/run_evals.py --gate                   # exit 1 if quality dropped

For each case: a fresh conversation, every turn sent to the agent, approvals
answered automatically from the case's `approve` field. Then three kinds of
scores on the last answer:

  retrieval      page_hit, page_recall      did the search find the right pages?
  deterministic  no_forbidden_text, trajectory, citations_valid, approval_respected
                 rules checked in code; free, exact, never flaky
  judge          judge_correct, judge_faithful   a stronger LLM reads the answer

plus cost_usd and latency_s. With Langfuse keys in .env, the dataset is synced
to Langfuse, every case becomes a trace (with the full graph inside it) and
every score is attached to it, so runs can be compared side by side in the UI.
Without keys it runs the same way and only writes evals/results/.
"""

import argparse
import asyncio
import json
import importlib
import os
import re
import time
from pathlib import Path
import uuid
from datetime import datetime

from common import (JUDGE_MODEL, dataset_name, langfuse_item_id, RESULTS_DIR, cost_usd, get_judge_llm, get_langfuse,
                    langfuse_enabled, load_cases, to_item)

from langchain_core.callbacks import UsageMetadataCallbackHandler  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402
from langfuse import Evaluation  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

PAGE_PATTERN = re.compile(r"^(?:\[\d+\] )?[\w .-]+\.pdf, page (\d+)", re.MULTILINE)
# Step 10: any source label, e.g. '[2] 0010-hybrid.md, section "Decision"'.
SOURCE_PATTERN = re.compile(r"^\[\d+\] (.+)$", re.MULTILINE)
SOURCE_TOOLS = {"search_course_material", "search_knowledge", "read_page", "search_documents"}

# Quality gate: --gate fails the run when an average drops below these.
GATE = {"judge_correct": 0.80, "judge_faithful": 0.85, "trajectory": 0.90, "page_hit": 0.85}
# ...or drops by more than this compared with the previous run.
MAX_DROP = 0.10


def cited_pages(answer: str) -> set[int]:
    """Page numbers the answer cites: 'page 14', 'p. 14', 'pp. 8-9', 'pages 8, 9 and 15'."""
    pages = set()
    for match in re.finditer(r"\b(?:pages?|pp?\.)\s*([\d\s,–\-and]+)", answer, re.IGNORECASE):
        numbers = [int(n) for n in re.findall(r"\d+", match.group(1))]
        if re.search(r"\d\s*[-–]\s*\d", match.group(1)) and len(numbers) == 2 and numbers[0] < numbers[1]:
            numbers = list(range(numbers[0], numbers[1] + 1))
        pages.update(numbers)
    return pages


# ------------------------------------------------------------------ the task: run one case

def make_task(graph, langfuse_on: bool):
    async def task(*, item, **_):
        # Dataset items from Langfuse are objects; local items are dicts.
        get = (lambda k: item[k]) if isinstance(item, dict) else (lambda k: getattr(item, k))
        turns, metadata = get("input")["turns"], get("metadata") or {}
        usage = UsageMetadataCallbackHandler()
        callbacks = [usage]
        if langfuse_on:
            from langfuse.langchain import CallbackHandler
            callbacks.append(CallbackHandler())  # nests the graph's runs under this item's trace
        config = {"configurable": {"thread_id": uuid.uuid4().hex}, "callbacks": callbacks,
                  "recursion_limit": 40}

        started = time.perf_counter()
        approval_asked = False
        try:
            for turn in turns:
                graph_input = {"messages": [HumanMessage(turn)]}
                while True:  # keep going until the turn finishes without an approval pause
                    await graph.ainvoke(graph_input, config)
                    pending = (await graph.aget_state(config)).interrupts
                    if not pending:
                        break
                    approval_asked = True
                    graph_input = Command(resume="approve" if metadata.get("approve") else "reject")
        except Exception as e:  # a crash is a result too: score it as a failure
            return {"error": f"{type(e).__name__}: {e}", "answer": "", "tool_calls": [],
                    "retrieved_pages": [], "sources": [], "approval_asked": approval_asked,
                    "seconds": round(time.perf_counter() - started, 2), "cost_usd": None}
        seconds = time.perf_counter() - started

        state = (await graph.aget_state(config)).values
        messages = state["messages"]
        last_question = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
        turn = messages[last_question:]
        tool_messages = [m for m in turn if isinstance(m, ToolMessage)]
        sources = [m.text for m in tool_messages if m.name in SOURCE_TOOLS]
        tokens = {model: {k: u[k] for k in ("input_tokens", "output_tokens")}
                  for model, u in usage.usage_metadata.items()}
        return {
            "answer": messages[-1].text,
            "tool_calls": [c["name"] for m in turn if isinstance(m, AIMessage) for c in m.tool_calls],
            "retrieved_pages": sorted({int(p) for s in sources for p in PAGE_PATTERN.findall(s)}),
            "retrieved_sources": sorted({src.strip() for s in sources for src in SOURCE_PATTERN.findall(s)}),
            "sources": sources,
            "approval_asked": approval_asked,
            "revisions": state.get("revisions", 0),
            "tokens": tokens,
            "cost_usd": cost_usd(tokens),
            "seconds": round(seconds, 2),
        }

    return task


# ------------------------------------------------------------------ item evaluators

def retrieval(*, output, expected_output, **_):
    sources = set(expected_output.get("expected_sources") or [])
    if sources and not output.get("error"):  # Step 10: Markdown bots cite sections, not pages
        found = set(output.get("retrieved_sources", []))
        return [Evaluation(name="source_hit", value=float(bool(sources & found)),
                           comment=f"expected {sorted(sources)}, retrieved {sorted(found)}")]
    expected = set(expected_output.get("expected_pages") or [])
    if not expected or output.get("error"):
        return []  # nothing to find (off-topic, not covered) - not scored
    found = set(output["retrieved_pages"])
    return [
        Evaluation(name="page_hit", value=float(bool(expected & found)),
                   comment=f"expected {sorted(expected)}, retrieved {sorted(found)}"),
        Evaluation(name="page_recall", value=len(expected & found) / len(expected)),
    ]


def deterministic(*, output, expected_output, metadata, **_):
    if output.get("error"):
        return [Evaluation(name="no_error", value=0.0, comment=output["error"])]
    answer, calls = output["answer"].lower(), output["tool_calls"]
    results = [Evaluation(name="no_error", value=1.0)]

    forbidden = [t for t in expected_output.get("must_not_say") or [] if t.lower() in answer]
    results.append(Evaluation(name="no_forbidden_text", value=float(not forbidden),
                              comment=f"said: {forbidden}" if forbidden else None))

    missing = [t for t in metadata.get("must_call") or [] if t not in calls]
    banned = [t for t in metadata.get("must_not_call") or [] if t in calls]
    problems = ([f"did not call {missing}"] if missing else []) + ([f"called {banned}"] if banned else [])
    results.append(Evaluation(name="trajectory", value=float(not problems),
                              comment="; ".join(problems) or f"calls: {calls}"))

    # Every page the answer cites must be a page the agent actually retrieved.
    cited = cited_pages(output["answer"])
    if cited:
        invented = sorted(cited - set(output["retrieved_pages"]))
        results.append(Evaluation(name="citations_valid", value=float(not invented),
                                  comment=f"cited pages never retrieved: {invented}" if invented else None))

    if "approve" in metadata:  # an approval case: the human must have been asked
        results.append(Evaluation(name="approval_respected", value=float(output["approval_asked"])))
    return results


def make_judge_evaluator(judge):
    async def judged(*, input, output, expected_output, **_):
        if output.get("error"):
            return []
        try:
            verdict = await judge(turns=input["turns"], expected_behaviour=expected_output["expected_behaviour"],
                                  key_points=expected_output.get("key_points") or [],
                                  sources=output["sources"], answer=output["answer"])
        except Exception as e:
            # Never let a failed judgement vanish: averages over fewer cases look fine but lie.
            return [Evaluation(name="judge_error", value=1.0, comment=f"{type(e).__name__}: {e}"[:500])]
        details = {"missing_points": verdict.missing_points,
                   "unsupported_claims": verdict.unsupported_claims}
        return [
            Evaluation(name="judge_correct", value=float(verdict.correct),
                       comment=verdict.explanation, metadata=details),
            Evaluation(name="judge_faithful", value=float(verdict.faithful),
                       comment="; ".join(verdict.unsupported_claims) or None),
        ]

    return judged


def cost_and_latency(*, output, **_):
    results = [Evaluation(name="latency_s", value=output["seconds"])]
    if output.get("cost_usd") is not None:
        results.append(Evaluation(name="cost_usd", value=round(output["cost_usd"], 6)))
    return results


# ------------------------------------------------------------------ run-level evaluator

def averages(*, item_results, **_):
    """One number per score for the whole run, shown on the run in Langfuse."""
    totals: dict[str, list[float]] = {}
    for result in item_results:
        for e in result.evaluations:
            if e.name == "judge_error":
                continue  # a marker, not a quality score; reported separately
            totals.setdefault(e.name, []).append(float(e.value))
    return [Evaluation(name=f"avg_{name}", value=round(sum(v) / len(v), 4), comment=f"n={len(v)}")
            for name, v in sorted(totals.items())]


# ------------------------------------------------------------------ main

def sync_dataset(langfuse, cases, name, description):
    """Upsert the cases into the Langfuse dataset, under ids unique across datasets (langfuse_item_id)."""
    langfuse.create_dataset(name=name, description=description)
    for case in cases:
        item = to_item(case)
        langfuse.create_dataset_item(dataset_name=name, id=langfuse_item_id(name, item["id"]), input=item["input"],
                                     expected_output=item["expected_output"], metadata=item["metadata"])


def case_id_of(item) -> str:
    """The YAML case id of a result's item: a plain dict when Langfuse is off, a dataset item when on."""
    if isinstance(item, dict):
        return item["id"]
    return (item.metadata or {}).get("case_id") or item.id


def summarise(result, cases_by_id, label, args):
    rows = []
    for r in result.item_results:
        item_id = case_id_of(r.item)
        rows.append({"id": item_id, "category": cases_by_id[item_id]["category"],
                     "trace_id": r.trace_id, "output": r.output,
                     "scores": {e.name: e.value for e in r.evaluations},
                     "comments": {e.name: e.comment for e in r.evaluations if e.comment}})
    return {
        "label": label, "created": datetime.now().isoformat(timespec="seconds"),
        "settings": {"bot": args.bot_id, "agent": args.agent, "model": os.getenv("OPENROUTER_MODEL", "default"), "judge": JUDGE_MODEL,
                     "verify_answers": not args.no_verify, "retrieval_mode": args.retrieval_mode},
        "experiment_id": result.experiment_id if langfuse_enabled() else None,
        "averages": {e.name.removeprefix("avg_"): e.value for e in result.run_evaluations},
        "cases": rows,
    }


def print_report(summary, previous):
    print(f"\n{'case':34} {'category':15} correct faithful traject. pages  cost$   secs")
    for row in summary["cases"]:
        s = row["scores"]
        fmt = lambda k: "  -  " if k not in s else (" yes " if s[k] == 1 else "  NO ")  # noqa: E731
        cost = row["output"].get("cost_usd")
        print(f"{row['id']:34} {row['category']:15} {fmt('judge_correct')}   {fmt('judge_faithful')}   "
              f"{fmt('trajectory')}   {fmt('page_hit' if 'page_hit' in s else 'source_hit')} {'-' if cost is None else f'{cost:.4f}':>7} "
              f"{row['output']['seconds']:6.1f}")
        for name, comment in row["comments"].items():
            if s.get(name) == 0:
                print(f"    {name}: {comment}")
    unjudged = [r for r in summary["cases"] if "judge_correct" not in r["scores"] and not r["output"].get("error")]
    if unjudged:
        print(f"\nWARNING: the judge failed on {len(unjudged)} case(s); judge averages leave them out.")
        for r in unjudged:
            print(f"    {r['id']}: {r['comments'].get('judge_error', 'no judgement')}")
    print(f"\n{'score':22} {'this run':>9} {'previous':>9}")
    for name, value in summary["averages"].items():
        before = previous["averages"].get(name) if previous else None
        print(f"{name:22} {value:9.3f} {'' if before is None else f'{before:9.3f}'}")


def gate(summary, previous) -> list[str]:
    failures = []
    unjudged = sum("judge_correct" not in r["scores"] for r in summary["cases"])
    if unjudged:
        failures.append(f"{unjudged} case(s) have no judge score, so the averages are incomplete")
    for name, minimum in GATE.items():
        value = summary["averages"].get(name)
        if value is not None and value < minimum:
            failures.append(f"{name} {value:.2f} is below {minimum:.2f}")
        before = previous["averages"].get(name) if previous else None
        if value is not None and before is not None and before - value > MAX_DROP:
            failures.append(f"{name} dropped from {before:.2f} to {value:.2f}")
    return failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="run", help="a name for this run, e.g. baseline")
    parser.add_argument("--only", help="comma-separated case ids")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--no-verify", action="store_true", help="turn off the Step 7 fact check")
    parser.add_argument("--model", help="agent model on OpenRouter (the judge stays the same)")
    parser.add_argument("--agent", default="step9", choices=["step7", "step9"],
                        help="step9 adds the guardrails; step7 is the agent without them")
    parser.add_argument("--retrieval-mode", default="rerank", choices=["vector", "keyword", "hybrid", "rerank"])
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--bot", default="bots/study_assistant.yaml", help="the bot to evaluate")
    parser.add_argument("--dataset", help="its test cases (default: evals/golden.yaml)")
    parser.add_argument("--gate", action="store_true", help="exit 1 if quality is below the gate")
    args = parser.parse_args()

    if args.model:
        os.environ["OPENROUTER_MODEL"] = args.model  # read by llm.get_llm()

    from engine import retrieval as retrieval_module
    from engine import search_graph
    from engine.bot_config import load_bot
    from judge import make_judge
    from engine.llm import get_llm
    build_graph = importlib.import_module(f"engine.{args.agent}_agent").build_graph
    from engine.tool_registry import load_tools

    retrieval_module.DEFAULT_MODE = args.retrieval_mode
    search_graph.VERBOSE = False
    search_graph.CACHE_ENABLED = False  # each case must do its own search
    bot = load_bot(args.bot)
    args.bot_id = bot.id
    if bot.knowledge:  # load models and the index once, before the threads start
        retrieval_module.get_retriever(bot.knowledge.collection).search("warm up", k=1)
    dataset_file = Path(args.dataset) if args.dataset else None

    cases = load_cases(dataset=dataset_file)
    if args.only:
        wanted = args.only.split(",")
        cases = [c for c in cases if c["id"] in wanted]
    if args.limit:
        cases = cases[:args.limit]
    if not cases:
        raise SystemExit("No cases selected.")
    cases_by_id = {c["id"]: c for c in cases}

    if args.no_verify:
        bot = bot.model_copy(update={"verify_answers": False})
    tools = asyncio.run(load_tools(bot.tools, bot))
    graph = build_graph(bot, tools, get_llm(), InMemorySaver())
    langfuse = get_langfuse()
    langfuse_on = langfuse_enabled()
    label = f"{args.label}-{datetime.now():%Y%m%d-%H%M%S}"

    experiment = dict(
        name=bot.id.replace("_", "-"), run_name=label,
        description=f"agent={args.agent} model={os.getenv('OPENROUTER_MODEL', 'default')} verify={not args.no_verify} "
                    f"retrieval={args.retrieval_mode}",
        task=make_task(graph, langfuse_on),
        evaluators=[retrieval, deterministic, make_judge_evaluator(make_judge(get_judge_llm(), bot)),
                    cost_and_latency],
        run_evaluators=[averages], max_concurrency=args.concurrency,
    )
    print(f"Running {len(cases)} cases (judge: {JUDGE_MODEL}, Langfuse: {'on' if langfuse_on else 'off'})...")
    if langfuse_on:
        name = dataset_name(bot.id)
        # Always the full dataset, whatever --only selects.
        sync_dataset(langfuse, load_cases(dataset=dataset_file), name, f"Test cases for {bot.name}")
        dataset = langfuse.get_dataset(name)
        selected = {langfuse_item_id(name, case_id) for case_id in cases_by_id}
        dataset.items = [i for i in dataset.items if i.id in selected]  # also drops items from before the fix
        result = dataset.run_experiment(**experiment)
    else:
        result = langfuse.run_experiment(data=[to_item(c) for c in cases], **experiment)

    RESULTS_DIR.mkdir(exist_ok=True)
    # Compare with the latest earlier run over the same cases.
    previous = None
    for path in sorted(RESULTS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True):
        run = json.loads(path.read_text(encoding="utf-8"))
        same_bot = run.get("settings", {}).get("bot", "study_assistant") == bot.id
        if same_bot and {c["id"] for c in run["cases"]} == set(cases_by_id):
            previous = run
            break
    summary = summarise(result, cases_by_id, label, args)
    out = RESULTS_DIR / f"{label}.json"
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print_report(summary, previous)
    print(f"\nSaved {out.relative_to(RESULTS_DIR.parent.parent)}")
    if langfuse_on:
        langfuse.flush()
        if result.experiment_url:
            print(f"Langfuse: {result.experiment_url}")
    if args.gate:
        failures = gate(summary, previous)
        print("\nGate: " + ("PASSED" if not failures else "FAILED\n  " + "\n  ".join(failures)))
        raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
