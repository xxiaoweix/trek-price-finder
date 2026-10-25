from bike_price_alerts.cli import main


def test_kill_switch_prevents_scan(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("TRACKER_ENABLED", "false")
    result = main(
        [
            "trek-scan",
            "--config",
            "examples/trek.yaml",
            "--db",
            str(tmp_path / "unused.sqlite3"),
        ]
    )
    assert result == 0
    assert "KILL SWITCH" in capsys.readouterr().out
    assert not (tmp_path / "unused.sqlite3").exists()
