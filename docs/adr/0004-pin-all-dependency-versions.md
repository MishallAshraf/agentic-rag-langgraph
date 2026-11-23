# 0004. Pin every dependency version

- Status: Accepted
- Date: 2026-10-10
- Step: 1

## Context

LangChain and LangGraph release frequently and occasionally change or deprecate APIs (for example, `langchain-community` is being sunset). Code written for one version may break on the next install. The project needs to be rebuildable exactly.

## Decision

Generate `requirements.txt` with `pip freeze`, pinning every installed package, including indirect dependencies, to an exact version.

## Alternatives considered

- **Pin only the packages we import directly:** shorter and more readable, but indirect dependencies can still change and break things.
- **A lock-file tool (uv, Poetry, pip-tools):** keeps a short list of direct dependencies and a separate full lock file, and can lock for several platforms. Better practice, but another tool to learn at Step 1.

## Consequences

- `pip install -r requirements.txt` reproduces the environment exactly on Windows.
- The file includes Windows-only packages (such as `colorama` and `win32_setctime`), so it may need adjusting on Linux or macOS, for example in a Docker image.
- It is harder to see which packages are direct dependencies; the README and the imports show that.
- The file must be regenerated whenever a package is added or upgraded.
- Revisit when the project is containerised or deployed (around Step 10), where a lock-file tool such as uv would be the better fit.
