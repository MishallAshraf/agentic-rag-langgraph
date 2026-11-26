# Lampus

Lampus is a bilingual (English and Urdu / Roman Urdu) AI tutoring product for schools and private tutors, built on the bot-builder engine in [`../engine`](../engine). *Pass the light.*

The engine stays a general bot builder: agents, retrieval, guardrails, sign-in, quotas, evaluation, the API and the frontend. This folder holds what is specific to tutoring, which the builder can't make on its own:

- tutor bots per subject, grounded in a textbook with page citations
- a check that the tutor gives hints rather than answers
- homework grading, mastery tracking and the next-exercise recommender
- teacher dashboards and parent reports
- Urdu and Roman Urdu support
- each school as its own workspace

Nothing is here yet: Phase 1 makes the engine production-ready first (Linux, Postgres, Redis, a queue, several API copies). The tutor MVP starts in Phase 2.

The product plan, build phases and how each course topic maps to a feature are in [ai-system-design-lab](https://github.com/MishallAshraf/ai-system-design-lab/blob/main/docs/lampus-product.md).
