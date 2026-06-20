from sluice_cli.config import load_config, resolve, save_config


def test_should_prefer_flag_over_context(tmp_path):
    p = tmp_path / "config.yaml"
    save_config(p, {"current-context": "prod", "contexts": {"prod": {"api": "https://prod", "api_key": "K"}}})
    r = resolve(api="https://flag", api_key=None, context=None, path=p)
    assert r.api == "https://flag" and r.api_key == "K"  # flag wins for api; context fills api_key


def test_should_default_api_when_nothing_set(tmp_path):
    r = resolve(api=None, api_key=None, context=None, path=tmp_path / "none.yaml")
    assert r.api == "http://localhost:8080" and r.api_key is None


def test_should_round_trip_set_and_use_context(tmp_path):
    from sluice_cli.config import set_context, use_context

    p = tmp_path / "config.yaml"
    set_context(p, "stg", api="https://stg", api_key="S")
    use_context(p, "stg")
    cfg = load_config(p)
    assert cfg["current-context"] == "stg"
    r = resolve(api=None, api_key=None, context=None, path=p)
    assert r.api == "https://stg" and r.api_key == "S"
