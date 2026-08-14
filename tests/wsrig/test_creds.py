import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import wsrig.creds as creds


def test_prefers_the_id_suffixed_name_from_the_env_file(tmp_path, monkeypatch):
    f = tmp_path / "trading.env"
    f.write_text("KALSHI_API_KEY_ID=abc123\nKALSHI_PRIVATE_KEY_PATH=/k.pem\n")
    monkeypatch.setattr(creds, "ENV_FILE", f)
    monkeypatch.delenv("KALSHI_API_KEY", raising=False)
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    assert creds.load_creds() == ("abc123", "/k.pem")


def test_process_env_wins_over_the_file(tmp_path, monkeypatch):
    f = tmp_path / "trading.env"
    f.write_text("KALSHI_API_KEY_ID=fromfile\n")
    monkeypatch.setattr(creds, "ENV_FILE", f)
    monkeypatch.setenv("KALSHI_API_KEY", "fromenv")
    assert creds.load_creds()[0] == "fromenv"


def test_missing_file_yields_none_not_empty_string(tmp_path, monkeypatch):
    """Empty string would sign a request with a blank key and fail obscurely."""
    monkeypatch.setattr(creds, "ENV_FILE", tmp_path / "nope.env")
    monkeypatch.delenv("KALSHI_API_KEY", raising=False)
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    assert creds.load_creds() == (None, None)


def test_strips_quotes_and_comments(tmp_path, monkeypatch):
    f = tmp_path / "trading.env"
    f.write_text('# comment\nKALSHI_API_KEY_ID="quoted"\n\n')
    monkeypatch.setattr(creds, "ENV_FILE", f)
    monkeypatch.delenv("KALSHI_API_KEY", raising=False)
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    assert creds.load_creds()[0] == "quoted"
