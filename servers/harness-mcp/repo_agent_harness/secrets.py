"""Secret-path refusal and output redaction."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:  # keep the package importable in minimal (hook) environments
    yaml = None  # ty: ignore[invalid-assignment]

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


def _resolve(root: str | Path) -> Path | None:
    """First existing secrets.yml in the resolution chain: repo policies > home > packaged."""
    from repo_agent_harness.paths import harness_home, repo_id  # noqa: PLC0415

    h = harness_home()
    rid = repo_id(str(root))
    candidates = [
        h / "repos" / rid / "policies" / "secrets.yml",
        h / "policies" / "secrets.yml",
        Path(__file__).parent / "defaults" / "secrets.yml",
    ]
    return next((p for p in candidates if yaml is not None and p.is_file()), None)


def _read(path: Path) -> dict:
    """Parse a secrets.yml; an empty document reads as an empty mapping.

    A valid-YAML non-mapping document (list/scalar) raises YAMLError so that both
    load's callers and validate treat it like any other unreadable file.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        msg = f"expected a mapping, got {type(data).__name__}"
        raise yaml.YAMLError(msg)
    return data


def _merged(builtin: list[str], extra: list[str]) -> list[str]:
    """Builtins first (order-stable), then file-sourced additions, deduplicated."""
    return list(builtin) + [p for p in extra if p not in builtin]


def load(root: str | Path) -> SecretsConfig:
    """Load SecretsConfig from the config resolution chain.

    File patterns extend the builtins; a repo file can add, never remove.
    """
    path = _resolve(root)
    if path is None:
        return SecretsConfig()
    data = _read(path)
    return SecretsConfig(
        secret_paths=_merged(DEFAULT_SECRET_PATHS, data.get("secret_paths") or []),
        redact_patterns=_merged(DEFAULT_REDACT_PATTERNS, data.get("redact_patterns") or []),
    )


def validate(root: str | Path) -> list[str]:
    """Health-check the winning secrets.yml; never raises, empty list == healthy.

    Only file-sourced redact patterns need compiling (builtins are compiled constants;
    ``secret_paths`` are fnmatch globs with nothing to validate).
    """
    path = _resolve(root)
    if path is None:
        return []
    try:
        data = _read(path)
    except (yaml.YAMLError, OSError) as err:
        return [f"secrets.yml ({path}): unreadable ({err})"]
    problems = []
    for pat in data.get("redact_patterns") or []:
        try:
            re.compile(pat)
        except re.error as err:
            problems.append(f"secrets.yml ({path}): invalid redact pattern {pat!r}: {err}")
    return problems


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
