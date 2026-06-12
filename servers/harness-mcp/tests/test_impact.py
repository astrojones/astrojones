from repo_agent_harness import impact


def test_impact_high_risk(repo):
    out = impact.file_impact(str(repo), "src/payment.py")
    assert out["risk"] == "high"
    assert any("test_payment" in t for t in out["test_targets"])


def test_impact_low_risk_unreferenced(repo):
    (repo / "lonely.py").write_text("x = 1\n")
    out = impact.file_impact(str(repo), "lonely.py")
    assert out["risk"] in {"low", "medium"}
