import pytest

from stock_tracker.config import Config, load_config


def test_load_config_real_file():
    config = load_config()
    assert config.database.path == "data/tracker.db"
    assert config.quality.min_market_cap_eur == 30_000_000_000
    assert config.signals.tranche_1.sma50_discount_pct == 8.0


def test_config_attribute_and_dict_access():
    config = Config({"a": {"b": 1}})
    assert config.a.b == 1
    assert config["a"]["b"] == 1


def test_config_missing_key_raises_attribute_error():
    config = Config({"a": 1})
    with pytest.raises(AttributeError):
        _ = config.missing


def test_config_get_with_default():
    config = Config({"a": 1})
    assert config.get("missing", "fallback") == "fallback"
    assert config.get("a") == 1


def test_load_config_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")
