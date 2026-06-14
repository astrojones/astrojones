from harness import verify


def test_verify_clean_repo_all_skipped(repo):
    out = verify.verify_changed(str(repo))
    assert out["ok"] is True
    assert {r["kind"] for r in out["results"]} == {"lint", "typecheck", "test"}
    assert all(r["via"] == "core" for r in out["results"])


def test_test_changed_runs_pytest_for_changed(repo):
    (repo / "src" / "payment.py").write_text("def charge():\n    return 2\n")
    out = verify.test_changed(str(repo))
    assert out["skipped"] is False
    assert "test_payment" in (out["command"] or "")


def test_verify_prefers_shim(repo):
    tools = repo / "agent" / "tools"
    tools.mkdir(parents=True)
    shim = tools / "lint-changed"
    shim.write_text('#!/bin/sh\necho \'{"ok": true, "skipped": false, "command": "fake", "output": ""}\'\n')
    shim.chmod(0o755)
    out = verify.verify_changed(str(repo))
    lint = next(r for r in out["results"] if r["kind"] == "lint")
    assert lint["via"] == "agent/tools/lint-changed"
