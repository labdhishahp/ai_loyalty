"""Configuration reading. No database, no network.

The rule under test is the one that bites: a .env with a blank value must be
indistinguishable from no value at all. Without it, an unfilled placeholder
shadows every default in the codebase and the symptom appears far from the cause.
"""

from __future__ import annotations

import pytest

from core import config


@pytest.fixture
def env(monkeypatch):
    return monkeypatch


def test_blank_and_whitespace_are_unset(env):
    for blank in ("", "   ", "\t"):
        env.setenv("LMART_TEST_VALUE", blank)
        assert config.get("LMART_TEST_VALUE") is None
        assert config.get("LMART_TEST_VALUE", "fallback") == "fallback"
        assert config.get_int("LMART_TEST_VALUE", 7) == 7
        assert config.get_bool("LMART_TEST_VALUE", True) is True


def test_values_are_stripped(env):
    env.setenv("LMART_TEST_VALUE", "  hello  ")
    assert config.get("LMART_TEST_VALUE") == "hello"


def test_require_names_the_variable_and_points_at_the_template(env):
    env.delenv("LMART_TEST_VALUE", raising=False)
    with pytest.raises(config.ConfigError, match="LMART_TEST_VALUE.*\\.env\\.example"):
        config.require("LMART_TEST_VALUE")


def test_typed_readers_reject_nonsense(env):
    env.setenv("LMART_TEST_VALUE", "many")
    with pytest.raises(config.ConfigError, match="whole number"):
        config.get_int("LMART_TEST_VALUE", 1)
    with pytest.raises(config.ConfigError, match="true or false"):
        config.get_bool("LMART_TEST_VALUE", False)


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("TRUE", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("0", False), ("no", False), ("off", False),
])
def test_boolean_spellings(env, raw, expected):
    env.setenv("LMART_TEST_VALUE", raw)
    assert config.get_bool("LMART_TEST_VALUE", not expected) is expected


def test_pool_url_falls_back_to_the_direct_connection(env):
    env.setenv("DATABASE_URL", "postgresql://direct")
    env.setenv("DATABASE_POOL_URL", "")
    assert config.pool_url() == "postgresql://direct"
    env.setenv("DATABASE_POOL_URL", "postgresql://pooled")
    assert config.pool_url() == "postgresql://pooled"
