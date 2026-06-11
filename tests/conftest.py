"""Shared fixtures: load the extensionless template tools and build synthetic app dirs."""

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

# Never write __pycache__ next to the template tools — new-app copies template/_shared/
# verbatim and must not ship bytecode into scaffolded apps.
sys.dont_write_bytecode = True

TOOLS_DIR = (
    Path(__file__).resolve().parents[1] / "template" / "_shared" / "agent" / "tools"
)
TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "template"


def load_tool(name: str):
    """Import an extensionless agent tool script as a module."""
    loader = importlib.machinery.SourceFileLoader(
        name.replace("-", "_"), str(TOOLS_DIR / name)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def dv():
    return load_tool("deploy-validate")


COMPOSE = """services:
  web:
    image: ghcr.io/astrojones/{name}:latest   # two-segment; matches CI push
    restart: unless-stopped
    expose:
      - "8080"
"""

MANIFEST = """apiVersion: nuk/v1
kind: Deployment
metadata:
  name: {name}
spec:
  source: {{}}
  ingress:
    - host: {name}.astrojones.de
      service: web
      port: 8080
  envFrom:
    - secretRef: /opt/nuklaut/secrets/{name}.env
    - secretRef: /opt/nuklaut/secrets/_shared.env
"""

WORKFLOW = """name: deploy
on:
  push:
    branches: [main]
permissions:
  contents: read
  packages: write
jobs:
  deploy:
    uses: astrojones/.github/.github/workflows/nuk-deploy.yml@main
    secrets: inherit
"""

DOCKERFILE = """FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
EXPOSE 8080
CMD ["uv", "run", "--no-dev", "uvicorn", "demo.api:app", "--host", "0.0.0.0", "--port", "8080"]
"""


@pytest.fixture
def app(tmp_path: Path) -> Path:
    """A minimal valid deployable app dir named demo-app."""
    return make_app(tmp_path / "demo-app")


def make_app(root: Path, name: str | None = None) -> Path:
    name = name or root.name
    root.mkdir(parents=True, exist_ok=True)
    (root / "docker-compose.yml").write_text(COMPOSE.format(name=name))
    (root / ".nuklaut").mkdir(exist_ok=True)
    (root / ".nuklaut" / "deployment.yml").write_text(MANIFEST.format(name=name))
    (root / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (root / ".github" / "workflows" / "deploy.yml").write_text(WORKFLOW)
    (root / "Dockerfile").write_text(DOCKERFILE)
    return root
