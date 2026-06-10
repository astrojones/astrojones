"""HTTP API for __REPO_NAME__."""

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="__REPO_NAME__")


class Health(BaseModel):
    """Liveness response."""

    status: str


@app.get("/health")
async def health() -> Health:
    """Return service liveness for uptime checks."""
    return Health(status="ok")
