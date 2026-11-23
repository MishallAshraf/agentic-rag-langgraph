"""Load and validate a bot configuration file (bots/*.yaml).

A bot is data, not code: its name, look, prompt and tool list live in a YAML
file, and the engine runs whichever file it is given. Pydantic checks the file
on load, so a typo fails immediately with a clear message instead of halfway
through a conversation.
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, model_validator


class Theme(BaseModel):
    primary_color: str = "#0F6E56"
    avatar: str = "bot"


class BotConfig(BaseModel):
    id: str
    name: str
    description: str
    welcome: str = ""
    suggested_questions: list[str] = []
    theme: Theme = Theme()  # used by the frontend (Step 12)
    system_prompt: str
    tools: list[str]
    approval_required: list[str] = []
    # Step 7: fact-check final answers against the sources found in the same turn.
    verify_answers: bool = False

    @model_validator(mode="after")
    def approval_tools_must_be_enabled(self):
        extra = set(self.approval_required) - set(self.tools)
        if extra:
            raise ValueError(f"approval_required lists tools not in tools: {sorted(extra)}")
        return self


def load_bot(path: str | Path) -> BotConfig:
    with open(path, encoding="utf-8") as f:
        return BotConfig.model_validate(yaml.safe_load(f))
