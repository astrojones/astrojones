import json

from repo_agent_harness import cli


def test_cli_overview(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    rc = cli.main(["overview"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["root"] == str(repo)


def test_cli_check_command_deny(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    cli.main(["check-command", "rm -rf /"])
    out = json.loads(capsys.readouterr().out)
    assert out["allowed"] is False


def test_cli_status(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    cli.main(["status"])
    out = json.loads(capsys.readouterr().out)
    assert "branch" in out
