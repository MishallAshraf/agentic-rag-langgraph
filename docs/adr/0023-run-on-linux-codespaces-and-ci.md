# 0023. Run on Linux: platform markers, Codespaces and CI

- Status: Accepted
- Date: 2026-11-25
- Step: Lampus Phase 1, Step 1
- Amends: [0004](0004-pin-all-dependency-versions.md)

## Context

The engine is becoming Lampus, a product that has to run on servers, in Docker containers, in GitHub Codespaces and in CI. All of those are Linux. Until now it only installed on Windows: `requirements.txt` came from `pip freeze` on a Windows laptop and pins two Windows-only packages, `pywin32` and `win32_setctime`. On Linux, `pip install -r requirements.txt` stopped at `pywin32==312` and nothing installed. The development laptop (8 GB RAM, dual-core) is also too small to run Postgres, Redis and several API copies at once, which Phase 1 needs.

## Decision

- Keep the fully pinned `requirements.txt` from ADR 0004, but mark the two Windows-only packages with an environment marker (`; sys_platform == "win32"`), so Windows installs them and Linux skips them.
- Add a dev container (`.devcontainer/devcontainer.json`): Python 3.11, Node for the frontend, and Docker inside the container for the later Phase 1 steps. It opens as a GitHub Codespace.
- Add a GitHub Actions workflow (`.github/workflows/ci.yml`) that installs the requirements and runs every test on Ubuntu for each push and pull request. It uses a placeholder API key, and points `LLM_BASE_URL` at a closed port, so a test that accidentally calls a model fails instead of spending money.
- Make `tests/` a package so `python -m unittest discover` finds every suite.
- Add `.gitattributes` so text files are stored with Linux line endings.

## Alternatives considered

- **Switch to a multi-platform lock file (uv, pip-tools) now:** the better long-term practice ADR 0004 already pointed to, but a bigger change than this step needs. Two markers fix the actual problem; revisit when the Docker image is built (Phase 1, Step 3).
- **Delete the two packages from the file:** Windows would then install a slightly different environment than the one tested.
- **Docker Desktop on the laptop instead of Codespaces:** runs a Linux VM that alone takes 2–4 GB of the laptop's 8 GB.

## Consequences

- One `requirements.txt` installs on both Windows and Linux. Checked with a pip dry run for Linux x86-64 and Python 3.11: before, the install failed at `pywin32`; after, all 143 Linux packages have wheels.
- Every push runs the 69 tests on Linux, so a change that breaks Linux is caught at once.
- Development can move to a Codespace, so the laptop is no longer the limit.
- The file is still regenerated with `pip freeze`; a new Windows-only package needs a marker added by hand until a lock-file tool replaces this.
