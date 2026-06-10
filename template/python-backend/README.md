# __REPO_NAME__

FastAPI service for the astrojones org, deployed to nuklaut.

## Develop

```bash
uv sync
uv run uvicorn __REPO_PKG__.api:app --reload --port 8080   # http://localhost:8080/health
uv run pytest && uv run ruff check . && uv run ty check     # the gate
```

## Deploy

Push to `main`. CI builds `ghcr.io/astrojones/__REPO_NAME__:latest`, then
`nuk apply` publishes it at https://__REPO_NAME__.astrojones.de. No SSH.

Per-app secrets: add a repo secret named `APP_ENV` (multiline `key=value`).
See `.env.example`.

## Standards

Tooling is generated from [astrojones/standards](https://github.com/astrojones/standards).
Don't hand-edit the `[tool.*]` sections — regenerate with `pyproject-canon`.
Full project rules in `AGENTS.md`.
