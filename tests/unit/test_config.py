from app.config import Settings


def test_vercel_relative_sqlite_uses_writable_tmp(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    settings = Settings(_env_file=None, database_url="sqlite+aiosqlite:///data/vintexchange.db")
    assert str(settings.sqlite_path) == "/tmp/vintexchange.db"
