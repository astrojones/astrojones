"""Secret-path refusal and output redaction."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:  # keep the package importable in minimal (hook) environments
    yaml = None

DEFAULT_SECRET_PATHS = [
    ".env",
    ".env.*",
    "secrets/",
    "credentials/",
    "service-account*.json",
    "id_rsa",
    "id_ed25519",
]
DEFAULT_REDACT_PATTERNS = [
    r"AKIA[0-9A-Z]{16}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"xox[baprs]-[A-Za-z0-9-]+",
    r"gh[pousr]_[A-Za-z0-9]{20,}",
]
REDACTION = "[REDACTED]"


@dataclass
class SecretsConfig:
    """Loaded secret-path and redaction-pattern configuration."""

    secret_paths: list[str] = field(default_factory=lambda: list(DEFAULT_SECRET_PATHS))
    redact_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_REDACT_PATTERNS))

    def __post_init__(self) -> None:
        self._compiled = [re.compile(p) for p in self.redact_patterns]


def load(root: str | Path) -> SecretsConfig:
    """Load SecretsConfig from the config resolution chain, or return defaults."""
    from repo_agent_harness.paths import harness_home, repo_id  # noqa: PLC0415

    h = harness_home()
    rid = repo_id(str(root))
    candidates = [
        h / "repos" / rid / "policies" / "secrets.yml",
        h / "policies" / "secrets.yml",
        Path(__file__).parent / "defaults" / "secrets.yml",
    ]
    path = next((p for p in candidates if yaml is not None and p.is_file()), None)
    if path is None:
        return SecretsConfig()
    data = yaml.safe_load(path.read_text()) or {}
    return SecretsConfig(
        secret_paths=data.get("secret_paths") or list(DEFAULT_SECRET_PATHS),
        redact_patterns=data.get("redact_patterns") or list(DEFAULT_REDACT_PATTERNS),
    )


def is_secret_path(rel_path: str, cfg: SecretsConfig) -> bool:
    """Return True if the repo-relative path matches a configured secret-path pattern."""
    rel = rel_path.replace("\\", "/")
    rel = rel.removeprefix("./")
    name = rel.rsplit("/", 1)[-1]
    for pat in cfg.secret_paths:
        if pat.endswith("/"):
            d = pat.rstrip("/")
            if rel == d or rel.startswith(d + "/") or ("/" + d + "/") in ("/" + rel):
                return True
        elif fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat):
            return True
    return False


def redact(text: str, cfg: SecretsConfig | None = None) -> str:
    """Redact known secret patterns from text using compiled regexes."""
    cfg = cfg or SecretsConfig()
    for rx in cfg._compiled:
        text = rx.sub(REDACTION, text)
    return text
