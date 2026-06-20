import httpx
import pytest
from sluice_cli.client import AdminClient, CliError


def _t(handler):
    return httpx.MockTransport(handler)


def test_should_send_api_key_header_on_every_call():
    seen = {}

    def h(req):
        seen["key"] = req.headers.get("X-API-Key")
        return httpx.Response(200, json=[{"name": "m"}])

    AdminClient("http://c", "SECRET", transport=_t(h)).list_apps()
    assert seen["key"] == "SECRET"


def test_should_translate_401_to_friendly_error():
    c = AdminClient("http://c", None, transport=_t(lambda r: httpx.Response(401, json={"detail": "no"})))
    with pytest.raises(CliError, match="unauthorized|SLUICE_API_KEY"):
        c.list_apps()


def test_should_translate_404_to_not_found():
    c = AdminClient("http://c", "K", transport=_t(lambda r: httpx.Response(404, json={})))
    with pytest.raises(CliError, match="not found"):
        c.get_app("missing")


def test_should_translate_connect_error_to_friendly_message():
    def boom(req):
        raise httpx.ConnectError("refused")

    c = AdminClient("http://c", "K", transport=_t(boom))
    with pytest.raises(CliError, match="cannot reach"):
        c.list_apps()


def test_should_return_none_when_spec_absent():
    c = AdminClient("http://c", "K", transport=_t(lambda r: httpx.Response(404)))
    assert c.get_spec("ghost") is None


def test_should_return_spec_text_when_present():
    c = AdminClient("http://c", "K", transport=_t(lambda r: httpx.Response(200, text="apiVersion: sluice/v1\n")))
    assert c.get_spec("m").startswith("apiVersion")
