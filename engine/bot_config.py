"""Load and validate a bot configuration file (bots/*.yaml).

A bot is data, not code: its name, look, prompt, knowledge and tool list live
in a YAML file, and the engine runs whichever file it is given. Pydantic checks
the file on load, so a typo fails immediately with a clear message instead of
halfway through a conversation.

From Step 10, configurations are also written by an LLM (the bot builder), so
validation is a security boundary: anything that ends up in a file path or a
database name (id, folder, collection) is checked against a strict pattern,
never trusted.
"""

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parent.parent
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")
AVATARS = {"bot", "owl", "book", "compass", "leaf", "spark", "shield", "chat"}


def contrast_with_white(hex_color: str) -> float:
    """WCAG contrast ratio of white text on this colour (4.5 or more is readable)."""
    def channel(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    luminance = 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
    return 1.05 / (luminance + 0.05)


class Theme(BaseModel):
    primary_color: str = "#0F6E56"
    avatar: str = "bot"

    @field_validator("primary_color")
    @classmethod
    def readable_color(cls, v: str) -> str:
        if not COLOR_PATTERN.match(v):
            raise ValueError("primary_color must look like #1A2B3C")
        if contrast_with_white(v) < 4.5:
            raise ValueError(f"primary_color {v} is too light for white text "
                             f"(contrast {contrast_with_white(v):.1f}, needs 4.5)")
        return v

    @field_validator("avatar")
    @classmethod
    def known_avatar(cls, v: str) -> str:
        if v not in AVATARS:
            raise ValueError(f"avatar must be one of {sorted(AVATARS)}")
        return v


class Knowledge(BaseModel):
    """The bot's own documents and vector collection (Step 10: one per bot)."""

    folder: str = Field(description="Folder of documents, relative to the project root.")
    collection: str = Field(description="Chroma collection holding this bot's chunks.")
    subject: str = Field(description="What the documents are, e.g. 'the course material on counselling'.")

    @field_validator("folder")
    @classmethod
    def inside_project(cls, v: str) -> str:
        path = (ROOT / v).resolve()
        if Path(v).is_absolute() or ".." in Path(v).parts or ROOT not in path.parents:
            raise ValueError("folder must be a relative path inside the project")
        return v

    @field_validator("collection")
    @classmethod
    def safe_collection(cls, v: str) -> str:
        if not ID_PATTERN.match(v):
            raise ValueError("collection must be 3-40 characters: lowercase letters, digits, underscores")
        return v

    @property
    def path(self) -> Path:
        return ROOT / self.folder


def rule_problems(rules: list[str]) -> list[str]:
    """Why a list of learned rules is not acceptable (empty if it is). A rule is one plain sentence
    that narrows behaviour; it can never be used to smuggle in new instructions or markup."""
    problems = []
    if len(rules) > MAX_LEARNED_RULES:
        problems.append(f"at most {MAX_LEARNED_RULES} learned rules, not {len(rules)}")
    if len({r.strip().lower() for r in rules}) != len(rules):
        problems.append("learned rules must not repeat")
    for r in rules:
        if not RULE_CHARS[0] <= len(r) <= RULE_CHARS[1]:
            problems.append(f"rule must be {RULE_CHARS[0]}-{RULE_CHARS[1]} characters: {r[:60]!r}")
        if "\n" in r or "<" in r or ">" in r or "```" in r or "http" in r.lower():
            problems.append(f"rule must be one line of plain text, without tags, code or links: {r[:60]!r}")
        if re.search(r"\b(ignore|disregard|override)\b.*\b(instructions|rules|prompt)\b", r, re.I):
            problems.append(f"rule must not cancel other instructions: {r[:60]!r}")
    return problems


class Research(BaseModel):
    """Step 14: a multi-agent bot that researches other bots' documents instead of its own."""
    sources: list[str] = Field(min_length=1, description="Ids of the bots whose documents may be researched.")
    max_tasks: int = Field(3, ge=1, le=5, description="Most sub-questions researched in parallel per round.")
    review_rounds: int = Field(1, ge=0, le=2, description="How often the reviewer may send the answer back.")

    @field_validator("sources")
    @classmethod
    def safe_sources(cls, v: list[str]) -> list[str]:
        bad = [s for s in v if not ID_PATTERN.match(s)]
        if bad:
            raise ValueError(f"invalid source ids {bad}")
        return list(dict.fromkeys(v))  # no duplicates


MAX_LEARNED_RULES = 12
RULE_CHARS = (10, 300)


class BotConfig(BaseModel):
    id: str
    name: str
    description: str
    welcome: str = ""
    suggested_questions: list[str] = []
    # Short names of what the bot covers; used when it declines or refuses.
    topics: list[str] = []
    theme: Theme = Theme()  # used by the frontend (Step 12)
    knowledge: Knowledge | None = None
    system_prompt: str
    tools: list[str]
    approval_required: list[str] = []
    # Step 7: fact-check final answers against the sources found in the same turn.
    verify_answers: bool = False
    # Step 14: set for a research bot (planner, parallel researchers, writer, reviewer).
    research: Research | None = None
    # Step 16: short rules added by the improvement loop (red team -> research -> fix -> verify)
    # and approved by a human. Code appends them to the system prompt at run time.
    learned_rules: list[str] = []

    @field_validator("id")
    @classmethod
    def safe_id(cls, v: str) -> str:
        # The id names a notes folder and a thread prefix: never let it be a path.
        if not ID_PATTERN.match(v):
            raise ValueError("id must be 3-40 characters: lowercase letters, digits, underscores, "
                             "starting with a letter")
        return v

    @field_validator("learned_rules")
    @classmethod
    def safe_rules(cls, v: list[str]) -> list[str]:
        problems = rule_problems(v)
        if problems:
            raise ValueError("; ".join(problems))
        return v

    def prompt_with_rules(self) -> str:
        """The system prompt the agent runs with: the bot's own, then the rules learned from testing."""
        if not self.learned_rules:
            return self.system_prompt
        return (self.system_prompt.rstrip() + "\n\nRules learned from testing (follow them as strictly as the rules above):\n"
                + "\n".join(f"- {r}" for r in self.learned_rules) + "\n")

    @model_validator(mode="after")
    def approval_tools_must_be_enabled(self):
        extra = set(self.approval_required) - set(self.tools)
        if extra:
            raise ValueError(f"approval_required lists tools not in tools: {sorted(extra)}")
        if self.research and self.id in self.research.sources:
            raise ValueError("a research bot cannot list itself as a source")
        return self

    def topics_text(self) -> str:
        return ", ".join(self.topics[:-1]) + f" or {self.topics[-1]}" if len(self.topics) > 1 else \
            (self.topics[0] if self.topics else self.description)


def load_bot(path: str | Path) -> BotConfig:
    with open(path, encoding="utf-8") as f:
        return BotConfig.model_validate(yaml.safe_load(f))
