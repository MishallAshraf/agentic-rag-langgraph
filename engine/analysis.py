"""Step 17 - Error analysis: build a bot's evaluation set from its own failures.

Generic metrics and generic test sets say little about one bot. This follows
the error-analysis method (Shankar and Husain, "Evals for AI Engineers"):

  1. dimensions  how this bot's users differ (persona, intent, difficulty...),
                 proposed by an LLM and edited by a person
  2. generate    synthetic first messages, one per combination of dimension
                 values, spread so that every value appears about equally often
  3. run         each message through the real Step 9 bot, recording every step
  4. label       a domain expert reads each conversation: pass or fail, a note
                 in their own words (open coding), and the first step that went
                 wrong (in the web app: #/review/<bot>)
  5. taxonomy    an LLM groups the notes into failure modes (axial coding);
                 the expert corrects and approves them
  6. judges      one LLM judge per failure mode, with examples from the expert's
                 labels, measured on labels it never saw: true positive and true
                 negative rates
  7. report      coverage, failure modes, where failures start, judge quality

Everything for a bot lives in evals/analysis/<bot id>/ and is plain YAML or
JSON, so a person can read, edit and review it in git.

    python -m engine.analysis dimensions --bot bots/customer_handbook.yaml
    python -m engine.analysis generate --bot bots/customer_handbook.yaml --n 50
    python -m engine.analysis run --bot bots/customer_handbook.yaml
    (label in the web app)
    python -m engine.analysis taxonomy --bot bots/customer_handbook.yaml
    (review and approve in the web app)
    python -m engine.analysis judges --bot bots/customer_handbook.yaml
    python -m engine.analysis report --bot bots/customer_handbook.yaml
"""

import argparse
import asyncio
import json
import os
import random
import re
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

import yaml
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import BaseModel, Field

from engine.bot_config import ROOT, BotConfig, load_bot
from engine.llm import STRUCTURED_METHOD

ANALYSIS_DIR = ROOT / "evals" / "analysis"
# The planner, the axial coder and the judges: a stronger model than the bot's (as in Step 8).
ANALYSIS_MODEL = os.getenv("ANALYSIS_MODEL", "anthropic/claude-sonnet-5.5")
BOT_ID = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
# Where a failure starts. Matches the Step 9 graph: input check -> search -> answer -> fact check.
STEPS = ("input_check", "search", "answer", "fact_check", "other")
SPLIT = {"train": 0.2, "dev": 0.4}  # the rest is test; train only supplies judge examples
CONCURRENCY = 2
MAX_NOTE = 1000
BATCH = 10


# ------------------------------------------------------------------ files

_locks: dict[Path, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(path: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(path, threading.Lock())


def _write(path: Path, text: str):
    """Write to a temporary file, then rename: a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _dump(data) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110)


class Workspace:
    """The analysis files of one bot. Every write is validated first."""

    def __init__(self, bot_id: str, root: Path | None = None):
        if not BOT_ID.fullmatch(bot_id or ""):
            raise ValueError(f"Not a bot id: {bot_id!r}")
        self.bot_id = bot_id
        self.dir = (root or ANALYSIS_DIR) / bot_id
        self.dimensions_file = self.dir / "dimensions.yaml"
        self.queries_file = self.dir / "queries.yaml"
        self.traces_file = self.dir / "traces.jsonl"
        self.labels_file = self.dir / "labels.json"
        self.taxonomy_file = self.dir / "taxonomy.yaml"
        self.judges_file = self.dir / "judges.yaml"
        self.report_file = self.dir / "report.md"

    def exists(self) -> bool:
        return self.traces_file.exists()

    def _yaml(self, path: Path, default):
        return (yaml.safe_load(path.read_text(encoding="utf-8")) or default) if path.exists() else default

    def dimensions(self) -> dict:
        return self._yaml(self.dimensions_file, {})

    def queries(self) -> list[dict]:
        return self._yaml(self.queries_file, [])

    def traces(self) -> list[dict]:
        if not self.traces_file.exists():
            return []
        return [json.loads(line) for line in self.traces_file.read_text(encoding="utf-8").splitlines() if line]

    def labels(self) -> dict:
        return json.loads(self.labels_file.read_text(encoding="utf-8")) if self.labels_file.exists() else {}

    def taxonomy(self) -> dict:
        return self._yaml(self.taxonomy_file, {})

    def judges(self) -> dict:
        return self._yaml(self.judges_file, {})

    def save_yaml(self, path: Path, data):
        with _lock(path):
            _write(path, _dump(data))

    def add_traces(self, traces: list[dict]):
        with _lock(self.traces_file):
            known = {t["id"] for t in self.traces()}
            new = [t for t in traces if t["id"] not in known]
            text = "".join(json.dumps(t, ensure_ascii=False) + "\n" for t in self.traces() + new)
            _write(self.traces_file, text)

    # ---------------------------------------------------------------- labels (open coding)

    def save_label(self, trace_id: str, verdict: str, note: str = "", first_failure: str | None = None,
                   by: str = "") -> dict:
        if trace_id not in {t["id"] for t in self.traces()}:
            raise KeyError(trace_id)
        note = " ".join((note or "").split())
        if verdict not in ("pass", "fail"):
            raise ValueError("The verdict is pass or fail.")
        if len(note) > MAX_NOTE:
            raise ValueError(f"Keep the note under {MAX_NOTE} characters.")
        if verdict == "fail":
            if len(note) < 3:
                raise ValueError("Say what went wrong: the note is what the failure modes are built from.")
            if first_failure not in STEPS:
                raise ValueError(f"Choose where the failure started: {', '.join(STEPS)}.")
        else:
            first_failure = None
        label = {"verdict": verdict, "note": note, "first_failure": first_failure, "by": by,
                 "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        with _lock(self.labels_file):
            labels = self.labels()
            labels[trace_id] = label
            _write(self.labels_file, json.dumps(labels, indent=1, ensure_ascii=False))
        # A changed verdict changes what the failure modes must cover: they need a new review.
        tax = self.taxonomy()
        assigned = tax.get("assignments", {}) if tax else {}
        if tax and ((verdict == "pass" and trace_id in assigned) or (verdict == "fail" and trace_id not in assigned)):
            if verdict == "pass":
                del assigned[trace_id]
            else:
                assigned[trace_id] = []
            tax["status"] = "proposed"
            self.save_yaml(self.taxonomy_file, tax)
        return label

    def failed_ids(self) -> list[str]:
        return [tid for tid, label in self.labels().items() if label["verdict"] == "fail"]

    # ---------------------------------------------------------------- taxonomy (axial coding)

    def set_assignment(self, trace_id: str, modes: list[str]) -> dict:
        tax = self.taxonomy()
        if not tax:
            raise ValueError("There is no taxonomy yet. Run: python -m engine.analysis taxonomy")
        known = {m["id"] for m in tax["modes"]}
        if unknown := [m for m in modes if m not in known]:
            raise ValueError(f"Unknown failure modes: {', '.join(unknown)}")
        if trace_id not in self.failed_ids():
            raise ValueError("Only conversations labelled as failures get failure modes.")
        tax["assignments"][trace_id] = sorted(set(modes))
        tax["status"] = "proposed"  # any change needs a new approval
        self.save_yaml(self.taxonomy_file, tax)
        return tax

    def unassigned(self) -> list[str]:
        tax = self.taxonomy()
        return [tid for tid in self.failed_ids() if not tax.get("assignments", {}).get(tid)]

    def approve_taxonomy(self, by: str) -> dict:
        tax = self.taxonomy()
        if not tax:
            raise ValueError("There is no taxonomy yet.")
        if missing := self.unassigned():
            raise ValueError(f"Give every failed conversation a failure mode first: {', '.join(missing)}")
        tax.update(status="approved", approved_by=by, approved_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        self.save_yaml(self.taxonomy_file, tax)
        return tax

    # ---------------------------------------------------------------- for the web app

    def summary(self) -> dict:
        traces, labels = self.traces(), self.labels()
        return {"bot_id": self.bot_id, "traces": len(traces), "labelled": len(labels),
                "failed": sum(v["verdict"] == "fail" for v in labels.values()),
                "taxonomy_status": self.taxonomy().get("status"), "judges_built": bool(self.judges())}

    def detail(self) -> dict:
        labels = self.labels()
        return {**self.summary(), "dimensions": self.dimensions(), "steps": list(STEPS),
                "items": [{**t, "label": labels.get(t["id"])} for t in self.traces()],
                "taxonomy": self.taxonomy() or None, "judges": self.judges() or None}


def workspaces(root: Path | None = None) -> list[Workspace]:
    base = root or ANALYSIS_DIR
    if not base.exists():
        return []
    return [w for p in sorted(base.iterdir()) if p.is_dir() and BOT_ID.fullmatch(p.name)
            for w in [Workspace(p.name, base)] if w.exists()]


# ------------------------------------------------------------------ 1. dimensions

DIMENSIONS_PROMPT = """You plan test conversations for a chatbot, so that a domain expert can find how it fails.
The bot: {bot}
Its topics: {topics}
Excerpts from its documents:
{excerpts}

Propose 3 or 4 dimensions along which real users' first messages differ in ways likely to change whether the bot answers well. Typical ones:
- persona: who is writing (e.g. a first-time visitor, a regular, a parent, a non-native speaker)
- intent: what they want, using the bot's real topics
- scenario or difficulty: a clear question, an ambiguous one, one the documents don't cover, a multi-part one, pressure to bend a policy, an emotional message, a typo-filled one
Give each dimension 3 to 6 short values. Include one ordinary value per dimension, so that not every message is an edge case."""


class Dimension(BaseModel):
    name: str = Field(description="snake_case, e.g. persona")
    description: str = Field(description="One sentence: how users differ along this dimension.")
    values: list[str] = Field(description="3 to 6 short values.")


class DimensionPlan(BaseModel):
    dimensions: list[Dimension]


def dimension_problems(dims: dict) -> list[str]:
    problems = []
    if not 2 <= len(dims) <= 5:
        problems.append("Use 2 to 5 dimensions.")
    for name, values in dims.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,30}", name):
            problems.append(f"Dimension name {name!r}: lowercase snake_case.")
        if not isinstance(values, list) or not 2 <= len(values) <= 8 or len(set(values)) != len(values):
            problems.append(f"Dimension {name!r}: 2 to 8 different values.")
    return problems


def structured(llm, schema):
    return llm.with_structured_output(schema, method=STRUCTURED_METHOD).with_retry(stop_after_attempt=3)


def bot_text(bot: BotConfig) -> str:
    return f"{bot.name}. {bot.description}"


async def propose_dimensions(bot: BotConfig, llm, excerpts: str) -> dict:
    plan = await structured(llm, DimensionPlan).ainvoke([HumanMessage(DIMENSIONS_PROMPT.format(
        bot=bot_text(bot), topics="; ".join(bot.topics) or "(none listed)", excerpts=excerpts))])
    return {"description": {d.name: d.description for d in plan.dimensions},
            "dimensions": {d.name: d.values for d in plan.dimensions}}


# ------------------------------------------------------------------ 2. generate

def sample_profiles(dimensions: dict[str, list[str]], n: int, seed: int = 0) -> list[dict]:
    """n combinations of dimension values. Each value appears about n / (its dimension's size)
    times, and combinations repeat only when n is larger than the number of combinations."""
    rng = random.Random(seed)
    names = list(dimensions)
    columns = {}
    for name in names:
        values = dimensions[name]
        column = (values * (n // len(values) + 1))[:n]
        rng.shuffle(column)
        columns[name] = column

    def rows():
        return [tuple(columns[name][i] for name in names) for i in range(n)]

    def repeats():  # sum of squared counts: smallest when combinations repeat as evenly as possible
        return sum(c * c for c in Counter(rows()).values())

    possible = 1
    for values in dimensions.values():
        possible *= len(values)
    q, r = divmod(n, possible)
    floor = r * (q + 1) ** 2 + (min(n, possible) - r) * q * q if n >= possible else n
    best = repeats()
    for _ in range(5000):  # swap values between rows: keeps each value's count, spreads combinations
        if best <= floor:
            break
        i, j, name = rng.randrange(n), rng.randrange(n), rng.choice(names)
        columns[name][i], columns[name][j] = columns[name][j], columns[name][i]
        now = repeats()
        if now > best:
            columns[name][i], columns[name][j] = columns[name][j], columns[name][i]
        else:
            best = now
    return [dict(zip(names, row)) for row in rows()]


QUERIES_PROMPT = """Write the first message a real user would send to this chatbot, one for each numbered profile.
The bot: {bot}
Excerpts from its documents:
{excerpts}

Rules:
- Match every value in the profile: the persona's voice, what they want, how hard the message is.
- Sound like a real person typing: usually short, informal where it fits, never "As a tourist, ...".
- Use real details from the excerpts (dish names, prices, times) when the message needs them. Invent plausible details only when the profile is about something the documents don't cover.
- Never mention testing, profiles or dimensions.
Return one message per profile number."""


class Query(BaseModel):
    number: int
    text: str


class QueryBatch(BaseModel):
    queries: list[Query]


async def write_queries(bot: BotConfig, llm, profiles: list[dict], excerpts: str) -> list[dict]:
    writer = structured(llm, QueryBatch)
    out = []
    for start in range(0, len(profiles), BATCH):
        batch = profiles[start:start + BATCH]
        listing = "\n".join(f"{i}. " + "; ".join(f"{k}: {v}" for k, v in p.items())
                            for i, p in enumerate(batch, 1))
        result = await writer.ainvoke([SystemMessage(QUERIES_PROMPT.format(bot=bot_text(bot), excerpts=excerpts)),
                                       HumanMessage(listing)])
        texts = {q.number: " ".join(q.text.split()) for q in result.queries}
        for i, profile in enumerate(batch, 1):
            if texts.get(i):
                out.append({"id": f"q{start + i:03d}", "profile": profile, "message": texts[i]})
    return out


# ------------------------------------------------------------------ 3. run

_TAGS = re.compile(r"</?tool_output[^>]*>")


def trace_steps(messages: list, values: dict, verify: bool) -> list[dict]:
    """What the bot did in the last turn, in order: input check, tool calls and results, fact check."""
    guard = values.get("guard") or "ok"
    steps = [{"step": "input_check", "result": guard.split(":")[0], "detail": guard}]
    last_question = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1)
    for m in messages[last_question + 1:]:
        if isinstance(m, AIMessage):
            for call in m.tool_calls:
                steps.append({"step": "tool_call", "tool": call["name"], "args": call["args"]})
        elif isinstance(m, ToolMessage):
            steps.append({"step": "tool_result", "tool": m.name, "text": _TAGS.sub("", m.text).strip()[:2000]})
    if verify and guard.startswith("ok"):
        steps.append({"step": "fact_check", "revisions": values.get("revisions", 0)})
    return steps


class TraceRunner:
    """Runs a bot's real Step 9 graph on one message. Approvals are always rejected."""

    def __init__(self, bot: BotConfig, llm):
        self.bot, self.llm, self.graph = bot, llm, None

    async def __call__(self, query: dict) -> dict:
        from engine import step9_agent
        from engine.tool_registry import load_tools

        if self.graph is None:
            self.graph = step9_agent.build_graph(self.bot, await load_tools(self.bot.tools, self.bot), self.llm,
                                                 InMemorySaver())
        usage = UsageMetadataCallbackHandler()
        config = {"configurable": {"thread_id": uuid.uuid4().hex}, "recursion_limit": 40, "callbacks": [usage]}
        trace = {"id": query["id"], "profile": query["profile"], "message": query["message"],
                 "answer": "", "steps": [], "models": [], "error": "", "seconds": 0.0}
        started = time.perf_counter()
        try:
            graph_input = {"messages": [HumanMessage(query["message"])]}
            while True:
                await self.graph.ainvoke(graph_input, config)
                if not (await self.graph.aget_state(config)).interrupts:
                    break
                graph_input = Command(resume="reject")
            values = (await self.graph.aget_state(config)).values
            trace["answer"] = values["messages"][-1].text
            trace["steps"] = trace_steps(values["messages"], values, self.bot.verify_answers)
        except Exception as e:  # noqa: BLE001 - a crash is a finding, not the end of the run
            trace["error"] = f"{type(e).__name__}: {e}"
        trace["seconds"] = round(time.perf_counter() - started, 1)
        trace["models"] = sorted(usage.usage_metadata)
        return trace


async def run_queries(queries: list[dict], runner) -> list[dict]:
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(q):
        async with sem:
            t = await runner(q)
            print(f"  {q['id']}: {'error' if t['error'] else 'ok'} ({t['seconds']} s)")
            return t

    return list(await asyncio.gather(*(one(q) for q in queries)))


# ------------------------------------------------------------------ 5. taxonomy

AXIAL_PROMPT = """You help with error analysis of a chatbot: {bot}
A domain expert read conversations with it and wrote, in their own words, what went wrong in each failed one (open coding). Group these notes into failure modes (axial coding).
- 2 to 8 modes. Each is specific, observable in one conversation, and named in plain words.
- The definition says what counts and what does not, so that someone else would label consistently.
- Use the expert's wording where possible. Never add a failure the notes don't describe.
- Assign every failed conversation to each mode its note describes (usually one)."""


class Mode(BaseModel):
    id: str = Field(description="snake_case, e.g. invented_menu_item")
    name: str
    definition: str


class Assignment(BaseModel):
    trace_id: str
    modes: list[str]


class TaxonomyDraft(BaseModel):
    modes: list[Mode]
    assignments: list[Assignment]


def clean_taxonomy(draft: TaxonomyDraft, failed: list[str]) -> tuple[dict, list[str]]:
    """Keep what is valid; list what was dropped, for the person reviewing it."""
    problems, modes, seen = [], [], set()
    for m in draft.modes:
        mode_id = re.sub(r"[^a-z0-9_]", "_", m.id.lower()).strip("_")[:40]
        if not mode_id or mode_id in seen:
            problems.append(f"dropped a mode with a missing or repeated id: {m.name}")
            continue
        seen.add(mode_id)
        modes.append({"id": mode_id, "name": m.name.strip(), "definition": " ".join(m.definition.split())})
    assignments = {}
    for a in draft.assignments:
        if a.trace_id not in failed:
            problems.append(f"ignored an assignment for {a.trace_id}, which is not a labelled failure")
            continue
        ids = [x for x in a.modes if x in seen]
        if len(ids) < len(a.modes):
            problems.append(f"{a.trace_id}: dropped unknown modes {sorted(set(a.modes) - seen)}")
        assignments[a.trace_id] = sorted(set(ids))
    for tid in failed:
        if not assignments.get(tid):
            problems.append(f"{tid} has no failure mode yet: assign one in the review page")
            assignments[tid] = []
    return {"status": "proposed", "modes": modes, "assignments": assignments}, problems


async def propose_taxonomy(ws: Workspace, bot: BotConfig, llm) -> tuple[dict, list[str]]:
    labels, traces = ws.labels(), {t["id"]: t for t in ws.traces()}
    failed = ws.failed_ids()
    if not failed:
        raise ValueError("No conversation is labelled as a failure yet.")
    notes = "\n\n".join(
        f"[{tid}] first went wrong at: {labels[tid]['first_failure']}\nUser: {traces[tid]['message']}\n"
        f"Bot: {traces[tid]['answer'][:600]}\nExpert's note: {labels[tid]['note']}" for tid in failed)
    draft = await structured(llm, TaxonomyDraft).ainvoke([SystemMessage(AXIAL_PROMPT.format(bot=bot_text(bot))),
                                                          HumanMessage(notes)])
    return clean_taxonomy(draft, failed)


# ------------------------------------------------------------------ 6. judges

def assign_splits(groups: dict[str, str], seed: int = 0) -> dict[str, str]:
    """trace id -> train/dev/test, stratified by group (a trace's first failure mode, or "pass"),
    so every split sees each kind. Train only supplies judge examples, so it is the smallest."""
    rng = random.Random(seed)
    by_group: dict[str, list[str]] = {}
    for tid, group in sorted(groups.items()):
        by_group.setdefault(group, []).append(tid)
    splits = {}
    for group, ids in sorted(by_group.items()):
        rng.shuffle(ids)
        n_train = max(1, round(len(ids) * SPLIT["train"])) if len(ids) >= 3 else 0
        n_dev = round((len(ids) - n_train) / 2)
        for i, tid in enumerate(ids):
            splits[tid] = "train" if i < n_train else "dev" if i < n_train + n_dev else "test"
    return splits


def rates(pairs: list[tuple[bool, bool]]) -> dict:
    """(expert says failure, judge says failure) pairs -> confusion counts, TPR and TNR."""
    tp = sum(h and j for h, j in pairs)
    fn = sum(h and not j for h, j in pairs)
    tn = sum(not h and not j for h, j in pairs)
    fp = sum(not h and j for h, j in pairs)
    return {"tp": tp, "fn": fn, "tn": tn, "fp": fp,
            "tpr": round(tp / (tp + fn), 3) if tp + fn else None,
            "tnr": round(tn / (tn + fp), 3) if tn + fp else None}


def corrected_rate(observed: float, tpr: float | None, tnr: float | None) -> float | None:
    """The true failure rate, estimated from a judge's observed rate and its known errors
    (the Rogan-Gladen correction): (observed + TNR - 1) / (TPR + TNR - 1).
    None when the judge is no better than chance, where the formula means nothing."""
    if tpr is None or tnr is None or tpr + tnr <= 1:
        return None
    return round(min(1.0, max(0.0, (observed + tnr - 1) / (tpr + tnr - 1))), 3)


JUDGE_PROMPT = """You check a conversation with a chatbot for ONE specific failure.
The bot: {bot}
Failure: {name}
Definition: {definition}

Decide whether the bot's reply shows this failure. Judge only this failure and ignore any other problem. Reason briefly first, then decide.

Examples labelled by a domain expert:
{examples}"""


class ModeVerdict(BaseModel):
    reasoning: str = Field(description="One or two sentences.")
    failure: bool = Field(description="True if the reply shows this failure.")


def render(trace: dict, limit: int = 1500) -> str:
    sources = [s["text"][:limit] for s in trace["steps"] if s["step"] == "tool_result"]
    parts = [f"User: {trace['message']}"]
    if sources:
        parts.append("What the bot's search returned:\n" + "\n---\n".join(sources))
    parts.append(f"Bot: {trace['answer'] or '(no reply: ' + trace['error'] + ')'}")
    return "\n\n".join(parts)


def judge_examples(train: list[str], positives: set[str], k: int = 2) -> list[str]:
    """Up to k expert-labelled failures and k non-failures from the train split."""
    pos = [t for t in train if t in positives][:k]
    neg = [t for t in train if t not in positives][:k]
    return pos + neg


async def build_judges(ws: Workspace, bot: BotConfig, llm, seed: int = 0) -> dict:
    tax = ws.taxonomy()
    if tax.get("status") != "approved":
        raise ValueError("Approve the failure modes first (in the review page).")
    labels, traces = ws.labels(), {t["id"]: t for t in ws.traces()}
    labelled = [tid for tid in labels if tid in traces]
    assignments = tax["assignments"]
    groups = {tid: (assignments.get(tid) or ["pass"])[0] if labels[tid]["verdict"] == "fail" else "pass"
              for tid in labelled}
    splits = assign_splits(groups, seed)
    judge = structured(llm, ModeVerdict)
    sem = asyncio.Semaphore(CONCURRENCY + 2)
    result = {"model": ANALYSIS_MODEL, "splits": dict(Counter(splits.values())), "modes": {}}

    for mode in tax["modes"]:
        positives = {tid for tid, modes in assignments.items() if mode["id"] in modes}
        train = [tid for tid in labelled if splits[tid] == "train"]
        examples = judge_examples(train, positives)
        shown = "\n\n".join(f"Example ({'FAILURE' if tid in positives else 'NOT this failure'}):\n"
                            f"{render(traces[tid], 600)}" for tid in examples) or "(none)"
        system = JUDGE_PROMPT.format(bot=bot_text(bot), name=mode["name"], definition=mode["definition"],
                                     examples=shown)

        async def decide(tid):
            async with sem:
                v = await judge.ainvoke([SystemMessage(system), HumanMessage(render(traces[tid]))])
                return tid, v.failure

        entry = {"name": mode["name"], "examples": examples, "prompt": system}
        for split in ("dev", "test"):
            ids = [tid for tid in labelled if splits[tid] == split]
            decided = dict(await asyncio.gather(*(decide(t) for t in ids)))
            entry[split] = rates([(tid in positives, decided[tid]) for tid in ids])
            if split == "test" and ids:
                human = sum(tid in positives for tid in ids) / len(ids)
                observed = sum(decided.values()) / len(ids)
                entry["test_rates"] = {"expert": round(human, 3), "judge": round(observed, 3),
                                       "corrected": corrected_rate(observed, entry["dev"]["tpr"],
                                                                   entry["dev"]["tnr"])}
        result["modes"][mode["id"]] = entry
        print(f"  {mode['id']}: dev TPR {entry['dev']['tpr']} TNR {entry['dev']['tnr']}, "
              f"test TPR {entry['test']['tpr']} TNR {entry['test']['tnr']}")
    return result


# ------------------------------------------------------------------ 7. report

def pct(a: int, b: int) -> str:
    return f"{a}/{b} ({100 * a / b:.0f}%)" if b else "0/0"


def write_report(ws: Workspace) -> str:
    traces, labels, tax, judges = ws.traces(), ws.labels(), ws.taxonomy(), ws.judges()
    labelled = [t for t in traces if t["id"] in labels]
    failed = [t for t in labelled if labels[t["id"]]["verdict"] == "fail"]
    lines = [f"# Error analysis: {ws.bot_id}", "",
             f"{len(traces)} conversations, {len(labelled)} labelled by a domain expert, "
             f"{len(failed)} failed: {pct(len(failed), len(labelled))}.", ""]

    lines += ["## Failure rate by dimension value", "", "| Dimension | Value | Failed |", "|---|---|---|"]
    for dim in ws.dimensions().get("dimensions", {}):
        for value in ws.dimensions()["dimensions"][dim]:
            group = [t for t in labelled if t["profile"].get(dim) == value]
            lines.append(f"| {dim} | {value} | {pct(sum(t in failed for t in group), len(group))} |")

    starts = Counter(labels[t["id"]]["first_failure"] for t in failed)
    lines += ["", "## Where failures start", "", "| Step | Failures |", "|---|---|"]
    lines += [f"| {step} | {starts.get(step, 0)} |" for step in STEPS]

    if tax:
        counts = Counter(m for modes in tax.get("assignments", {}).values() for m in modes)
        lines += ["", f"## Failure modes ({tax.get('status')})", "", "| Mode | Failures | Definition |",
                  "|---|---|---|"]
        lines += [f"| {m['name']} | {counts.get(m['id'], 0)} | {m['definition']} |" for m in tax["modes"]]

    if judges:
        lines += ["", f"## Judges ({judges['model']}; splits {judges['splits']})", "",
                  "| Mode | Dev TPR | Dev TNR | Test TPR | Test TNR | Test: expert rate | judge rate | corrected |",
                  "|---|---|---|---|---|---|---|---|"]
        for mode_id, j in judges["modes"].items():
            r = j.get("test_rates", {})
            lines.append(f"| {j['name']} | {j['dev']['tpr']} | {j['dev']['tnr']} | {j['test']['tpr']} | "
                         f"{j['test']['tnr']} | {r.get('expert')} | {r.get('judge')} | {r.get('corrected')} |")
        lines += ["", "TPR: of the failures the expert found, the share the judge also flags. TNR: of the "
                  "conversations without that failure, the share the judge leaves alone. With few labelled "
                  "failures per mode these numbers are rough; label more conversations to tighten them."]
    text = "\n".join(lines) + "\n"
    _write(ws.report_file, text)
    return text


# ------------------------------------------------------------------ CLI

def excerpts_for(bot: BotConfig, n: int = 8) -> str:
    from engine.improve import document_excerpts

    return document_excerpts(bot, n)


async def main():
    from engine.llm import get_llm

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["dimensions", "generate", "run", "status", "taxonomy", "judges",
                                            "report"])
    parser.add_argument("--bot", default="bots/customer_handbook.yaml")
    parser.add_argument("--n", type=int, default=50, help="generate: how many messages")
    parser.add_argument("--force", action="store_true", help="dimensions/generate/taxonomy: replace existing")
    args = parser.parse_args()
    bot = load_bot(args.bot)
    ws = Workspace(bot.id)
    planner = get_llm(model=ANALYSIS_MODEL)

    def guard(path: Path):
        if path.exists() and not args.force:
            raise SystemExit(f"{path.relative_to(ROOT)} exists; edit it, or pass --force to replace it.")

    if args.command == "dimensions":
        guard(ws.dimensions_file)
        dims = await propose_dimensions(bot, planner, excerpts_for(bot))
        ws.save_yaml(ws.dimensions_file, dims)
        print(_dump(dims) + f"\nSaved {ws.dimensions_file.relative_to(ROOT)}. Edit it before generating.")
    elif args.command == "generate":
        guard(ws.queries_file)
        dims = ws.dimensions().get("dimensions") or {}
        if problems := dimension_problems(dims):
            raise SystemExit("Fix dimensions.yaml first:\n  " + "\n  ".join(problems))
        queries = await write_queries(bot, planner, sample_profiles(dims, args.n), excerpts_for(bot))
        ws.save_yaml(ws.queries_file, queries)
        print(f"Saved {len(queries)} messages to {ws.queries_file.relative_to(ROOT)}. Read a few, then run them.")
    elif args.command == "run":
        done = {t["id"] for t in ws.traces()}
        todo = [q for q in ws.queries() if q["id"] not in done]
        print(f"Running {len(todo)} messages through {bot.name} ({len(done)} already done)...")
        traces = await run_queries(todo, TraceRunner(bot, get_llm()))
        ws.add_traces(traces)
        print(f"Saved {len(traces)} conversations. Label them at #/review/{bot.id} in the web app.")
    elif args.command == "status":
        print(json.dumps(ws.summary(), indent=1))
    elif args.command == "taxonomy":
        guard(ws.taxonomy_file)
        tax, problems = await propose_taxonomy(ws, bot, planner)
        ws.save_yaml(ws.taxonomy_file, tax)
        print(_dump(tax["modes"]))
        print("\n".join(f"  note: {p}" for p in problems))
        print("Review, correct and approve the failure modes in the review page.")
    elif args.command == "judges":
        ws.save_yaml(ws.judges_file, await build_judges(ws, bot, planner))
        print(write_report(ws))
    elif args.command == "report":
        print(write_report(ws))


if __name__ == "__main__":
    asyncio.run(main())
