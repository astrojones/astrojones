from harness import secrets


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
