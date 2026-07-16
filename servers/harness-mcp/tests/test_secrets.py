from repo_agent_harness import paths, secrets


def test_is_secret_path_env():
    cfg = secrets.SecretsConfig()
    assert secrets.is_secret_path(".env", cfg)
    assert secrets.is_secret_path("config/.env.production", cfg)
    assert secrets.is_secret_path("secrets/key.txt", cfg)
    assert secrets.is_secret_path("app/credentials/token", cfg)
    assert not secrets.is_secret_path("src/app.py", cfg)


def test_redact_aws_key():
    out = secrets.redact("key=AKIAABCDEFGHIJKLMNOP done")
    assert "AKIA" not in out and "[REDACTED]" in out


def test_redact_private_key_header():
    out = secrets.redact("-----BEGIN RSA PRIVATE KEY-----")
    assert "[REDACTED]" in out


def test_load_merges_and_dedups(tmp_path, isolated_harness_home):
    """File patterns extend the builtins order-stable; repeating a builtin adds no duplicate."""
    pol = isolated_harness_home / "repos" / paths.repo_id(str(tmp_path)) / "policies"
    pol.mkdir(parents=True)
    (pol / "secrets.yml").write_text(
        "redact_patterns:\n"
        f'  - "{secrets.DEFAULT_REDACT_PATTERNS[0]}"\n'
        '  - "NEW-[0-9]{4}"\n'
        "secret_paths:\n"
        f'  - "{secrets.DEFAULT_SECRET_PATHS[0]}"\n'
        '  - "vault/"\n'
    )
    cfg = secrets.load(str(tmp_path))
    assert cfg.redact_patterns == [*secrets.DEFAULT_REDACT_PATTERNS, "NEW-[0-9]{4}"]
    assert cfg.secret_paths == [*secrets.DEFAULT_SECRET_PATHS, "vault/"]


def test_validate_reports_malformed_pattern_and_source(tmp_path, isolated_harness_home):
    """A broken regex in the winning file yields exactly one message naming file and pattern."""
    pol = isolated_harness_home / "repos" / paths.repo_id(str(tmp_path)) / "policies"
    pol.mkdir(parents=True)
    yml = pol / "secrets.yml"
    yml.write_text('redact_patterns:\n  - "foo("\n')
    problems = secrets.validate(str(tmp_path))
    assert len(problems) == 1
    assert str(yml) in problems[0]
    assert "foo(" in problems[0]


def test_validate_reports_unreadable_yaml(tmp_path, isolated_harness_home):
    """Malformed yaml never raises; it surfaces as an 'unreadable' message naming the file."""
    pol = isolated_harness_home / "repos" / paths.repo_id(str(tmp_path)) / "policies"
    pol.mkdir(parents=True)
    yml = pol / "secrets.yml"
    yml.write_text("redact_patterns: [unclosed")
    problems = secrets.validate(str(tmp_path))
    assert len(problems) == 1
    assert str(yml) in problems[0]
    assert "unreadable" in problems[0]


def test_validate_reports_non_mapping_yaml(tmp_path, isolated_harness_home):
    """Valid YAML that isn't a mapping (e.g. a list) must not raise out of validate."""
    pol = isolated_harness_home / "repos" / paths.repo_id(str(tmp_path)) / "policies"
    pol.mkdir(parents=True)
    yml = pol / "secrets.yml"
    yml.write_text('- "foo"\n- "bar"\n')
    problems = secrets.validate(str(tmp_path))
    assert len(problems) == 1
    assert str(yml) in problems[0]
    assert "unreadable" in problems[0]


def test_validate_ok_returns_empty(tmp_path):
    """Empty list == healthy: the packaged defaults compile clean."""
    assert secrets.validate(str(tmp_path)) == []
