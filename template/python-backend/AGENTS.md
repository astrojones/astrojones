# AGENTS.md — __REPO_NAME__

Project charter every agent reads before editing.

## Standards (non-negotiable)

This project follows the org Python standard:
**[astrojones/standards](https://github.com/astrojones/standards)**
(`python/pyproject.canonical.toml`). The tooling config is generated from it —
do not hand-edit `[tool.ruff]`, `[tool.ty]`, `[tool.pytest.ini_options]`, or
`[tool.coverage.*]`. To refresh, regenerate with the `pyproject-canon` skill.

## The gate (must pass before every commit)

```bash
uv sync && uv run pytest && uv run ruff check . && uv run ty check
```

Type errors and lint failures are blockers, not warnings.

## Code rules

- **Pydantic-everywhere.** No `dict`, `list[dict]`, or `Any` across public
  function boundaries. Construct models at the call site.
- **No relative imports** (`ban-relative-imports = "all"`).
- **Google docstrings** on public modules/classes/functions.
- **TDD**: RED → GREEN → REFACTOR. Write the failing test first.
- **ty, not mypy.** `ruff format`, not black.

## Deploy

Push to `main` → builds → `ghcr.io/astrojones/__REPO_NAME__:latest` → `nuk apply`
→ https://__REPO_NAME__.astrojones.de. See the `nuklaut-deploy` skill for the
manifest rules. Per-app secrets go in the `APP_ENV` repo secret; never commit a
`.env`.

## Layout

```
src/__REPO_PKG__/api.py    FastAPI app (uvicorn entrypoint: __REPO_PKG__.api:app)
src/__REPO_PKG__/__main__.py  `python -m __REPO_PKG__` local runner
tests/                     pytest suite
.nuklaut/deployment.yml    nuk manifest
docker-compose.yml         service def (expose 8080, no ports/labels)
Dockerfile                 uv-based build
```
