from r2 import cli
from r2.config import Settings


def test_serve_refuses_a_public_address_for_the_full_assistant(monkeypatch, capsys):
    monkeypatch.delenv("R2_ALLOW_LAN", raising=False)
    assert cli.cmd_serve(Settings(host="0.0.0.0")) == 2
    assert "Refusing to listen" in capsys.readouterr().out


def test_serve_allows_a_public_address_only_for_the_demo_profile(monkeypatch, tmp_path):
    started = {}
    monkeypatch.setattr("uvicorn.run", lambda app, host, port: started.update(host=host))
    settings = Settings(host="0.0.0.0", profile="demo", data_dir=tmp_path, db_path=tmp_path / "t.db", claude=False)
    assert cli.cmd_serve(settings) == 0 and started["host"] == "0.0.0.0"
