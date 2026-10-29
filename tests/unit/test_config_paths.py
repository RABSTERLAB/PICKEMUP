"""Relative sqlite paths must pin to the project root, not the launch CWD.

A CWD-relative database_url forks the operator's corpus when the API is
started from another directory - an incident we actually lived through.
"""
from pickemup_api import config
from pickemup_api.config import Settings


def test_relative_sqlite_pins_to_repo_root():
    s = Settings(database_url="sqlite:///./data/some_test.db")
    assert s.database_url == "sqlite:///" + (
        config._REPO_ROOT / "data/some_test.db").as_posix()
    # anchored result is absolute regardless of the launch CWD
    assert "some_test.db" in s.database_url
    assert "./" not in s.database_url


def test_explicit_locations_pass_through():
    for url in (
        "sqlite:////srv/x.db",                      # absolute unix style
        "sqlite:///R:/somewhere/data/p.db",         # absolute windows style
        "sqlite:///:memory:",
        "postgresql+psycopg://u:p@h:5432/db",
    ):
        assert Settings(database_url=url).database_url == url


def test_prod_fails_closed_on_default_credentials():
    import pytest
    from pickemup_api.config import Settings
    with pytest.raises(RuntimeError):
        Settings(pickemup_env="prod")  # shipped default secret
    with pytest.raises(RuntimeError):
        Settings(pickemup_env="prod", pickemup_secret_key="x" * 32)  # no API key
    s = Settings(pickemup_env="prod", pickemup_secret_key="x" * 32,
                 pickemup_api_key="k")
    assert s.pickemup_env == "prod"
