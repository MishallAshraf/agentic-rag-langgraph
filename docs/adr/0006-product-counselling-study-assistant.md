# 0006. First product: a counselling study assistant

- Status: Accepted
- Date: 2026-11-23
- Step: 3

## Context

From Step 3 the agent has a system prompt and tool descriptions, which need a product in mind: who the users are, what they ask, and what the assistant must not do. Later steps depend on this too: which tools to add (Step 4), what to evaluate (Step 8), which guardrails are needed (Step 9) and which roles exist (Step 12). The only document is two textbook chapters on counselling psychology.

## Decision

Build a study assistant for students of counselling and psychotherapy. It answers questions from the course material with page citations, says when the material does not cover a question, and later quizzes students, explains terms and finds recent research.

The domain lives only in `prompts.py`, the tool descriptions in `tools.py`, and `documents/`. The code stays domain-neutral.

## Alternatives considered

- **Assistant for practising therapists over clinic documents:** strong for the privacy and governance topics, but involves client data and clinical advice, which is high-risk and has no realistic test data here.
- **Mental-health chatbot for the public:** rejected. Acting as a therapist for people in distress is unsafe for a learning project.
- **Generic "chat with your documents":** domain-agnostic, but without a concrete user it is hard to design tools, evaluation and roles.

## Consequences

- It is the first bot of the bot-creator platform planned in [ADR 0007](0007-platform-bots-as-configuration.md), and its reference test case.

- Maps to three course case studies: customer support agent with RAG, enterprise semantic search, and deep research multi-agent.
- A crisis guardrail is required (Step 9): a message from someone in distress gets pointed to real help instead of being treated as a study question.
- Roles for Step 12 follow naturally: students, and instructors with access to restricted materials.
- Open question: whether off-topic questions (such as general knowledge) should be answered or declined. Currently the model answers them; see Step 3 in the course notes.
