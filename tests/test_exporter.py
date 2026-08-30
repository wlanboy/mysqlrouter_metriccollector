import json
import ssl
import urllib.error
from datetime import UTC, datetime, timedelta
from email.message import Message
from unittest.mock import MagicMock, patch

import pytest

import exporter


def _router_client():
    return exporter.RouterClient("https://127.0.0.1:8443", "prometheus", "geheim", None)


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

def test_load_config_defaults(monkeypatch):
    for key in [
        "ROUTER_URL", "ROUTER_USER", "ROUTER_PASSWORD", "ROUTER_TLS_VERIFY",
        "PUSHGATEWAY_URL", "PUSHGATEWAY_JOB", "PUSHGATEWAY_INSTANCE",
        "PUSHGATEWAY_USER", "PUSHGATEWAY_PASSWORD", "SCRAPE_INTERVAL", "LOG_LEVEL",
    ]:
        monkeypatch.delenv(key, raising=False)

    cfg = exporter.load_config()

    assert cfg["router_url"] == "https://127.0.0.1:8443"
    assert cfg["router_user"] == "prometheus"
    assert cfg["router_tls_verify"] is False
    assert cfg["pushgateway_job"] == "mysql_router"
    assert cfg["scrape_interval"] == 60
    assert cfg["log_level"] == "INFO"


def test_load_config_from_env(monkeypatch):
    monkeypatch.setenv("ROUTER_URL", "https://router.example:8443")
    monkeypatch.setenv("ROUTER_TLS_VERIFY", "true")
    monkeypatch.setenv("SCRAPE_INTERVAL", "30")
    monkeypatch.setenv("LOG_LEVEL", "debug")

    cfg = exporter.load_config()

    assert cfg["router_url"] == "https://router.example:8443"
    assert cfg["router_tls_verify"] is True
    assert cfg["scrape_interval"] == 30
    assert cfg["log_level"] == "DEBUG"


# ---------------------------------------------------------------------------
# make_ssl_context / make_auth_header
# ---------------------------------------------------------------------------

def test_make_ssl_context_verify_disabled():
    ctx = exporter.make_ssl_context(False)
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


def test_make_ssl_context_verify_enabled():
    ctx = exporter.make_ssl_context(True)
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_make_auth_header():
    headers = exporter.make_auth_header("prometheus", "geheim")
    assert headers == {"Authorization": "Basic cHJvbWV0aGV1czpnZWhlaW0="}


# ---------------------------------------------------------------------------
# api_get
# ---------------------------------------------------------------------------

def _mock_response(payload_bytes):
    resp = MagicMock()
    resp.read.return_value = payload_bytes
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def test_api_get_success():
    payload = json.dumps({"foo": "bar"}).encode("utf-8")
    with patch("exporter.urllib.request.urlopen", return_value=_mock_response(payload)):
        result = exporter.api_get(exporter.RouterClient("https://host", "u", "p", None), "router/status")
    assert result == {"foo": "bar"}


def test_api_get_http_error():
    err = urllib.error.HTTPError("url", 401, "Unauthorized", Message(), None)
    with patch("exporter.urllib.request.urlopen", side_effect=err):
        result = exporter.api_get(exporter.RouterClient("https://host", "u", "p", None), "router/status")
    assert result is None


def test_api_get_url_error():
    err = urllib.error.URLError("connection refused")
    with patch("exporter.urllib.request.urlopen", side_effect=err):
        result = exporter.api_get(exporter.RouterClient("https://host", "u", "p", None), "router/status")
    assert result is None


def test_api_get_json_decode_error():
    with patch("exporter.urllib.request.urlopen", return_value=_mock_response(b"not json")):
        result = exporter.api_get(exporter.RouterClient("https://host", "u", "p", None), "router/status")
    assert result is None


# ---------------------------------------------------------------------------
# parse_iso / uptime_seconds
# ---------------------------------------------------------------------------

def test_parse_iso():
    dt = exporter.parse_iso("2026-06-04T05:00:00+00:00")
    assert dt == datetime(2026, 6, 4, 5, 0, 0, tzinfo=UTC)


def test_uptime_seconds():
    started = (datetime.now(UTC) - timedelta(seconds=3600)).isoformat()
    uptime = exporter.uptime_seconds(started)
    assert 3599 <= uptime <= 3601


# ---------------------------------------------------------------------------
# fetch_router_status
# ---------------------------------------------------------------------------

def test_fetch_router_status_unreachable():
    with patch("exporter.api_get", return_value=None):
        metrics = exporter.fetch_router_status(_router_client())
    assert len(metrics) == 1
    assert metrics[0] == exporter.Metric("mysql_router_up", {}, 0.0, "MySQL Router is reachable", "gauge")


def test_fetch_router_status_reachable_with_uptime():
    started = (datetime.now(UTC) - timedelta(seconds=100)).isoformat()
    data = {
        "version": "8.0.36",
        "productEdition": "MySQL Community - GPL",
        "processId": 1234,
        "timeStarted": started,
    }
    with patch("exporter.api_get", return_value=data):
        metrics = exporter.fetch_router_status(_router_client())

    by_name = {m.name: m for m in metrics}
    assert by_name["mysql_router_up"].value == 1.0
    assert by_name["mysql_router_up"].labels == {"version": "8.0.36", "edition": "MySQL Community - GPL"}
    assert by_name["mysql_router_process_id"].value == 1234.0
    assert 99 <= by_name["mysql_router_uptime_seconds"].value <= 101


def test_fetch_router_status_no_time_started():
    data = {"version": "8.0.36", "productEdition": "GPL", "processId": 1}
    with patch("exporter.api_get", return_value=data):
        metrics = exporter.fetch_router_status(_router_client())
    assert "mysql_router_uptime_seconds" not in {m.name for m in metrics}


def test_fetch_router_status_bad_time_started_is_skipped():
    data = {"version": "8.0.36", "productEdition": "GPL", "processId": 1, "timeStarted": "not-a-date"}
    with patch("exporter.api_get", return_value=data):
        metrics = exporter.fetch_router_status(_router_client())
    assert "mysql_router_uptime_seconds" not in {m.name for m in metrics}


# ---------------------------------------------------------------------------
# fetch_routes
# ---------------------------------------------------------------------------

def test_fetch_routes():
    data = {"items": [{"name": "bootstrap_rw"}, {"name": "bootstrap_ro"}, {"no_name": True}]}
    with patch("exporter.api_get", return_value=data):
        routes = exporter.fetch_routes(_router_client())
    assert routes == ["bootstrap_rw", "bootstrap_ro"]


def test_fetch_routes_none():
    with patch("exporter.api_get", return_value=None):
        routes = exporter.fetch_routes(_router_client())
    assert routes == []


# ---------------------------------------------------------------------------
# fetch_route_status
# ---------------------------------------------------------------------------

def test_fetch_route_status_all_fields():
    data = {"activeConnections": 3, "totalConnections": 42, "blockedHosts": 1}
    with patch("exporter.api_get", return_value=data):
        metrics = exporter.fetch_route_status(_router_client(), "bootstrap_rw")

    by_name = {m.name: m for m in metrics}
    assert by_name["mysql_router_route_active_connections"].value == 3.0
    assert by_name["mysql_router_route_active_connections"].labels == {"route": "bootstrap_rw"}
    assert by_name["mysql_router_route_total_connections_total"].value == 42.0
    assert by_name["mysql_router_route_blocked_hosts"].value == 1.0


def test_fetch_route_status_partial_fields():
    with patch("exporter.api_get", return_value={"activeConnections": 3}):
        metrics = exporter.fetch_route_status(_router_client(), "bootstrap_rw")
    assert [m.name for m in metrics] == ["mysql_router_route_active_connections"]


def test_fetch_route_status_none():
    with patch("exporter.api_get", return_value=None):
        metrics = exporter.fetch_route_status(_router_client(), "bootstrap_rw")
    assert metrics == []


# ---------------------------------------------------------------------------
# fetch_route_health
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("is_alive,expected", [(True, 1.0), (False, 0.0)])
def test_fetch_route_health(is_alive, expected):
    with patch("exporter.api_get", return_value={"isAlive": is_alive}):
        metrics = exporter.fetch_route_health(_router_client(), "bootstrap_rw")
    assert metrics == [exporter.Metric(
        "mysql_router_route_health", {"route": "bootstrap_rw"}, expected, "Route health status", "gauge",
    )]


def test_fetch_route_health_none():
    with patch("exporter.api_get", return_value=None):
        metrics = exporter.fetch_route_health(_router_client(), "bootstrap_rw")
    assert metrics == []


# ---------------------------------------------------------------------------
# fetch_route_connections
# ---------------------------------------------------------------------------

def test_fetch_route_connections_sums_bytes():
    data = {"items": [
        {"bytesFromServer": 100, "bytesToServer": 10},
        {"bytesFromServer": 200, "bytesToServer": 20},
    ]}
    with patch("exporter.api_get", return_value=data):
        metrics = exporter.fetch_route_connections(_router_client(), "bootstrap_rw")

    by_name = {m.name: m for m in metrics}
    assert by_name["mysql_router_route_bytes_from_server_total"].value == 300.0
    assert by_name["mysql_router_route_bytes_to_server_total"].value == 30.0


def test_fetch_route_connections_missing_fields_default_zero():
    data = {"items": [{}]}
    with patch("exporter.api_get", return_value=data):
        metrics = exporter.fetch_route_connections(_router_client(), "bootstrap_rw")
    by_name = {m.name: m for m in metrics}
    assert by_name["mysql_router_route_bytes_from_server_total"].value == 0.0
    assert by_name["mysql_router_route_bytes_to_server_total"].value == 0.0


def test_fetch_route_connections_none():
    with patch("exporter.api_get", return_value=None):
        metrics = exporter.fetch_route_connections(_router_client(), "bootstrap_rw")
    assert metrics == []


# ---------------------------------------------------------------------------
# fetch_route_destinations
# ---------------------------------------------------------------------------

def test_fetch_route_destinations():
    data = {"items": [{"address": "a"}, {"address": "b"}]}
    with patch("exporter.api_get", return_value=data):
        metrics = exporter.fetch_route_destinations(_router_client(), "bootstrap_rw")
    assert metrics == [exporter.Metric(
        "mysql_router_route_destinations", {"route": "bootstrap_rw"}, 2.0,
        "Number of destinations per route", "gauge",
    )]


def test_fetch_route_destinations_none():
    with patch("exporter.api_get", return_value=None):
        metrics = exporter.fetch_route_destinations(_router_client(), "bootstrap_rw")
    assert metrics == []


# ---------------------------------------------------------------------------
# fetch_metadata
# ---------------------------------------------------------------------------

def test_fetch_metadata_success():
    last_refresh = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
    metadata_list = {"items": [{"name": "mycluster"}]}
    status = {"refreshSucceeded": True, "timeLastRefreshSucceeded": last_refresh}

    with patch("exporter.api_get", side_effect=[metadata_list, status]):
        metrics = exporter.fetch_metadata(_router_client())

    by_name = {m.name: m for m in metrics}
    assert by_name["mysql_router_metadata_refresh_succeeded"].value == 1.0
    assert by_name["mysql_router_metadata_refresh_succeeded"].labels == {"instance": "mycluster"}
    assert "mysql_router_metadata_last_refresh_timestamp" in by_name
    assert 29 <= by_name["mysql_router_metadata_last_refresh_age_seconds"].value <= 31


def test_fetch_metadata_skips_item_without_name():
    metadata_list = {"items": [{}]}
    with patch("exporter.api_get", return_value=metadata_list) as mock_get:
        metrics = exporter.fetch_metadata(_router_client())
    assert metrics == []
    mock_get.assert_called_once()


def test_fetch_metadata_skips_when_status_none():
    metadata_list = {"items": [{"name": "mycluster"}]}
    with patch("exporter.api_get", side_effect=[metadata_list, None]):
        metrics = exporter.fetch_metadata(_router_client())
    assert metrics == []


def test_fetch_metadata_none():
    with patch("exporter.api_get", return_value=None):
        metrics = exporter.fetch_metadata(_router_client())
    assert metrics == []


def test_fetch_metadata_bad_timestamp_still_yields_refresh_metric():
    metadata_list = {"items": [{"name": "mycluster"}]}
    status = {"refreshSucceeded": False, "timeLastRefreshSucceeded": "not-a-date"}
    with patch("exporter.api_get", side_effect=[metadata_list, status]):
        metrics = exporter.fetch_metadata(_router_client())
    assert [m.name for m in metrics] == ["mysql_router_metadata_refresh_succeeded"]
    assert metrics[0].value == 0.0


# ---------------------------------------------------------------------------
# scrape
# ---------------------------------------------------------------------------

def test_scrape_skips_routes_when_router_down():
    with patch("exporter.fetch_router_status", return_value=[
        exporter.Metric("mysql_router_up", {}, 0.0, "help", "gauge"),
    ]), \
         patch("exporter.fetch_routes") as mock_routes, \
         patch("exporter.fetch_metadata") as mock_metadata:
        metrics = exporter.scrape(_router_client())

    mock_routes.assert_not_called()
    mock_metadata.assert_not_called()
    assert len(metrics) == 1
    assert metrics[0].value == 0.0


def test_scrape_collects_route_and_metadata_metrics_when_up():
    up_metric = exporter.Metric("mysql_router_up", {"version": "8.0"}, 1.0, "help", "gauge")
    route_metric = exporter.Metric("mysql_router_route_health", {"route": "bootstrap_rw"}, 1.0, "help", "gauge")
    metadata_metric = exporter.Metric("mysql_router_metadata_refresh_succeeded", {"instance": "c"}, 1.0, "help", "gauge")

    with patch("exporter.fetch_router_status", return_value=[up_metric]), \
         patch("exporter.fetch_routes", return_value=["bootstrap_rw"]), \
         patch("exporter.fetch_route_status", return_value=[]), \
         patch("exporter.fetch_route_health", return_value=[route_metric]), \
         patch("exporter.fetch_route_connections", return_value=[]), \
         patch("exporter.fetch_route_destinations", return_value=[]), \
         patch("exporter.fetch_metadata", return_value=[metadata_metric]):
        metrics = exporter.scrape(_router_client())

    assert metrics == [up_metric, route_metric, metadata_metric]


# ---------------------------------------------------------------------------
# _escape_label_value / format_metrics
# ---------------------------------------------------------------------------

def test_escape_label_value():
    assert exporter._escape_label_value('back\\slash "quote"\nnewline') == 'back\\\\slash \\"quote\\"\\nnewline'


def test_format_metrics_no_labels():
    metrics = [exporter.Metric("mysql_router_process_id", {}, 1234.0, "MySQL Router process ID", "gauge")]
    output = exporter.format_metrics(metrics)
    assert output == (
        "# HELP mysql_router_process_id MySQL Router process ID\n"
        "# TYPE mysql_router_process_id gauge\n"
        "mysql_router_process_id 1234.0\n"
    )


def test_format_metrics_with_labels_sorted_and_escaped():
    metrics = [exporter.Metric(
        "mysql_router_up", {"version": "8.0", "edition": 'a"b'}, 1.0, "help", "gauge",
    )]
    output = exporter.format_metrics(metrics)
    assert 'mysql_router_up{edition="a\\"b",version="8.0"} 1.0' in output


def test_format_metrics_counter_type_from_suffix():
    metrics = [exporter.Metric("mysql_router_route_total_connections_total", {}, 5.0, "help", "gauge")]
    output = exporter.format_metrics(metrics)
    assert "# TYPE mysql_router_route_total_connections_total counter" in output


def test_format_metrics_help_emitted_once_per_name():
    metrics = [
        exporter.Metric("mysql_router_route_health", {"route": "a"}, 1.0, "help", "gauge"),
        exporter.Metric("mysql_router_route_health", {"route": "b"}, 0.0, "help", "gauge"),
    ]
    output = exporter.format_metrics(metrics)
    assert output.count("# HELP mysql_router_route_health") == 1
    assert output.count("# TYPE mysql_router_route_health") == 1


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------

def _base_cfg():
    return {
        "pushgateway_url": "http://pushgw:9091",
        "pushgateway_job": "mysql_router",
        "pushgateway_instance": "host1",
        "pushgateway_user": "",
        "pushgateway_password": "",
    }


def test_push_success():
    resp = MagicMock()
    resp.status = 200
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False

    with patch("exporter.urllib.request.urlopen", return_value=resp) as mock_urlopen:
        ok = exporter.push(_base_cfg(), "payload\n")

    assert ok is True
    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "http://pushgw:9091/metrics/job/mysql_router/instance/host1"
    assert req.get_method() == "PUT"


def test_push_with_auth_sets_header():
    resp = MagicMock()
    resp.status = 200
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False

    cfg = _base_cfg()
    cfg["pushgateway_user"] = "u"
    cfg["pushgateway_password"] = "p"

    with patch("exporter.urllib.request.urlopen", return_value=resp) as mock_urlopen:
        exporter.push(cfg, "payload\n")

    req = mock_urlopen.call_args[0][0]
    assert req.get_header("Authorization") == "Basic dTpw"


def test_push_http_error():
    err = urllib.error.HTTPError("url", 500, "Internal Server Error", Message(), MagicMock(read=lambda: b"boom"))
    with patch("exporter.urllib.request.urlopen", side_effect=err):
        ok = exporter.push(_base_cfg(), "payload\n")
    assert ok is False


def test_push_url_error():
    with patch("exporter.urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
        ok = exporter.push(_base_cfg(), "payload\n")
    assert ok is False
