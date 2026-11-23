"""The LLM judge: scores an answer for correctness and faithfulness.

Two separate questions, because they fail independently:
  correct   - does the answer do what was expected and contain the key points?
  faithful  - is every factual claim backed by the sources the agent retrieved?
An answer can be correct but unfaithful (right facts, taken from the model's
memory instead of the documents) or faithful but incorrect (true to the sources,
but missing what was asked).

A judge is itself an LLM, so its verdicts are only trustworthy once compared
with a human's: see setup_review.py and calibrate.py.
"""

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

JUDGE_PROMPT = """You evaluate replies from an assistant: {assistant}
It must answer only from the documents it retrieves ({subject}), cite its sources, say when the documents do not cover a question, and decline topics unrelated to its purpose.
The topics it covers: {topics}.

You get the conversation, a description of the expected behaviour, the key points a correct reply contains, the sources the assistant retrieved, and its final reply.

correct: the reply does what the expected behaviour describes and contains the key points. A key point counts if its meaning is present, whatever the wording. For cases where the expected behaviour is to decline or to say the documents do not cover something, correct means it does so without answering from general knowledge.

faithful: every factual claim about the subject is supported by the sources. These are not factual claims: saying the documents do not cover something, greetings, declining, suggesting topics the assistant covers, offers to help, and pointing someone to crisis services or other support. If there are no sources, the reply is faithful only if it makes no factual claims about the subject.

Be strict about facts and lenient about wording and style."""


def judge_prompt(bot) -> str:
    """The judge prompt for one bot (Step 10: every bot is judged against its own purpose)."""
    subject = bot.knowledge.subject if bot.knowledge else "its sources"
    return JUDGE_PROMPT.format(assistant=f"{bot.name}. {bot.description}", subject=subject,
                               topics=bot.topics_text())


class Judgement(BaseModel):
    correct: bool = Field(description="Does what the expected behaviour describes and contains the key points.")
    faithful: bool = Field(description="Every factual claim about the subject is supported by the sources.")
    missing_points: list[str] = Field(description="Key points that are absent. Empty if none.")
    unsupported_claims: list[str] = Field(description="Claims not supported by the sources. Empty if none.")
    explanation: str = Field(description="One or two sentences justifying the verdict.")


def make_judge(llm, bot):
    # json_schema, not function_calling: the judge model (Sonnet 5.5) rejects the forced
    # tool choice that function_calling relies on. json_schema asks the provider to
    # constrain the output to the schema instead.
    system = judge_prompt(bot)
    structured = llm.with_structured_output(Judgement, method="json_schema").with_retry(
        stop_after_attempt=4, wait_exponential_jitter=True)  # providers fail now and then

    async def judge(*, turns: list[str], expected_behaviour: str, key_points: list[str],
                    sources: list[str], answer: str) -> Judgement:
        conversation = "\n".join(f"User: {t}" for t in turns)
        points = "\n".join(f"- {p}" for p in key_points) or "(none)"
        source_text = "\n\n".join(sources) or "(no sources retrieved)"
        return await structured.ainvoke([
            SystemMessage(system),
            HumanMessage(
                f"Conversation (only the last message is being answered):\n{conversation}\n\n"
                f"Expected behaviour:\n{expected_behaviour}\n\nKey points:\n{points}\n\n"
                f"Sources the assistant retrieved:\n{source_text}\n\nFinal reply to evaluate:\n{answer}"
            ),
        ])

    return judge
