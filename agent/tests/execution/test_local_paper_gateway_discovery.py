import pytest

from src.execution.local_paper_gateway_discovery import PaperGatewayDiscoveryError, discover_local_paper_gateway


def test_discovers_only_one_local_readonly_paper_account():
    probes, requests = [], []
    def port_open(host, port):
        probes.append((host, port))
        return port == 4002
    def snapshot(*, config):
        requests.append(config)
        return {"status": "ok", "accounts": ["DU123"]}
    result = discover_local_paper_gateway(port_open=port_open, snapshot=snapshot)
    assert probes == [("127.0.0.1", 4002), ("127.0.0.1", 7497)]
    assert result.account == "DU123" and result.port == 4002 and result.readonly is True
    assert requests[0].account is None and requests[0].profile == "paper"


@pytest.mark.parametrize("accounts", [[], ["U123"], ["DU123", "U123"], ["DU123", "DU456"]])
def test_discovery_rejects_empty_live_or_ambiguous_accounts(accounts):
    with pytest.raises(PaperGatewayDiscoveryError):
        discover_local_paper_gateway(port_open=lambda _host, port: port == 4002,
                                     snapshot=lambda **_: {"status": "ok", "accounts": accounts})


def test_discovery_rejects_multiple_endpoints_and_failed_verification():
    with pytest.raises(PaperGatewayDiscoveryError, match="exactly one"):
        discover_local_paper_gateway(port_open=lambda *_: True,
                                     snapshot=lambda **_: {"status": "ok", "accounts": ["DU123"]})
    def fail(**_):
        raise RuntimeError("private transport details")
    with pytest.raises(PaperGatewayDiscoveryError, match="could not be verified") as error:
        discover_local_paper_gateway(port_open=lambda _host, port: port == 4002, snapshot=fail)
    assert "private transport" not in str(error.value)
