# pyproject.toml is generated, not templated

This scaffold intentionally ships **no** `pyproject.toml` — that would be a
frozen copy of the org standard and would drift.

`/new-app` generates it at scaffold time via the `pyproject-canon` skill, which
fetches the live canonical config from
[astrojones/standards](https://github.com/astrojones/standards)
(`python/pyproject.canonical.toml`), applies the **api** shape (FastAPI +
uvicorn + httpx, `FAST` ruff rule) and Python 3.13, and substitutes the project
name/package. Then `uv sync` produces `uv.lock`.

To regenerate later (e.g. to pull a standards update), run `pyproject-canon`
again. Never hand-edit the `[tool.*]` sections.
