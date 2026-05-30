#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Requires Python 2.7.9+ (ssl.create_default_context and urllib2.HTTPSHandler(context=...) support)
import base64
import json
import logging
import os
import re
import socket
import ssl
import time
import urllib2
from collections import namedtuple
from datetime import datetime, timedelta, tzinfo

Metric = namedtuple("Metric", ["name", "labels", "value", "help", "type"])

API_VERSION = "20190715"


class _UTC(tzinfo):
    def utcoffset(self, dt):
        return timedelta(0)
    def tzname(self, dt):
        return "UTC"
    def dst(self, dt):
        return timedelta(0)

_utc = _UTC()
_epoch = datetime(1970, 1, 1, tzinfo=_utc)


class _PutRequest(urllib2.Request):
    def get_method(self):
        return "PUT"


def load_config():
    return {
        "router_url": os.environ.get("ROUTER_URL", "https://127.0.0.1:8443"),
        "router_user": os.environ.get("ROUTER_USER", "prometheus"),
        "router_password": os.environ.get("ROUTER_PASSWORD", "geheim"),
        "router_tls_verify": os.environ.get("ROUTER_TLS_VERIFY", "false").lower() == "true",
        "pushgateway_url": os.environ.get("PUSHGATEWAY_URL", "http://gmk.lan:9091"),
        "pushgateway_job": os.environ.get("PUSHGATEWAY_JOB", "mysql_router"),
        "pushgateway_instance": os.environ.get("PUSHGATEWAY_INSTANCE", socket.gethostname()),
        "pushgateway_user": os.environ.get("PUSHGATEWAY_USER", ""),
        "pushgateway_password": os.environ.get("PUSHGATEWAY_PASSWORD", ""),
        "scrape_interval": int(os.environ.get("SCRAPE_INTERVAL", "60")),
        "log_level": os.environ.get("LOG_LEVEL", "INFO").upper(),
    }


def make_opener(tls_verify):
    ctx = ssl.create_default_context()
    if not tls_verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return urllib2.build_opener(urllib2.HTTPSHandler(context=ctx))


def make_auth_header(user, password):
    credentials = base64.b64encode("{}:{}".format(user, password))
    return {"Authorization": "Basic {}".format(credentials)}


def parse_iso(s):
    s = s.replace("Z", "+00:00")
    match = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?[+-]\d{2}:\d{2}", s)
    if not match:
        raise ValueError("Cannot parse datetime: {}".format(s))
    dt = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S")
    return dt.replace(tzinfo=_utc)


def uptime_seconds(started_str):
    started = parse_iso(started_str)
    now = datetime.utcnow().replace(tzinfo=_utc)
    return (now - started).total_seconds()


def api_get(base_url, path, user, password, opener):
    url = "{}/api/{}/{}".format(base_url, API_VERSION, path)
    headers = make_auth_header(user, password)
    req = urllib2.Request(url, headers=headers)
    try:
        resp = opener.open(req, timeout=10)
        return json.loads(resp.read().decode("utf-8"))
    except urllib2.HTTPError as e:
        logging.warning("HTTP %s for %s", e.code, url)
        return None
    except urllib2.URLError as e:
        logging.warning("URL error for %s: %s", url, e.reason)
        return None
    except ValueError as e:
        logging.warning("JSON parse error for %s: %s", url, e)
        return None


def fetch_router_status(cfg, opener):
    data = api_get(
        cfg["router_url"], "router/status",
        cfg["router_user"], cfg["router_password"], opener
    )
    if data is None:
        return [Metric("mysql_router_up", {}, 0.0, "MySQL Router is reachable", "gauge")]

    metrics = [
        Metric(
            "mysql_router_up",
            {"version": str(data.get("version", "")), "edition": str(data.get("productEdition", ""))},
            1.0,
            "MySQL Router is reachable",
            "gauge",
        ),
        Metric(
            "mysql_router_process_id",
            {},
            float(data["processId"]),
            "MySQL Router process ID",
            "gauge",
        ),
    ]

    if "timeStarted" in data:
        try:
            metrics.append(Metric(
                "mysql_router_uptime_seconds",
                {},
                uptime_seconds(data["timeStarted"]),
                "MySQL Router uptime in seconds",
                "gauge",
            ))
        except (ValueError, KeyError) as e:
            logging.warning("Could not compute uptime: %s", e)

    return metrics


def fetch_routes(cfg, opener):
    data = api_get(
        cfg["router_url"], "routes",
        cfg["router_user"], cfg["router_password"], opener
    )
    if data is None:
        return []
    return [item["name"] for item in data.get("items", []) if "name" in item]


def fetch_route_status(cfg, opener, route):
    data = api_get(
        cfg["router_url"], "routes/{}/status".format(route),
        cfg["router_user"], cfg["router_password"], opener
    )
    if data is None:
        return []
    metrics = []
    labels = {"route": route}
    if "activeConnections" in data:
        metrics.append(Metric(
            "mysql_router_route_active_connections", labels,
            float(data["activeConnections"]),
            "Active connections per route", "gauge",
        ))
    if "totalConnections" in data:
        metrics.append(Metric(
            "mysql_router_route_total_connections_total", labels,
            float(data["totalConnections"]),
            "Total connections per route", "counter",
        ))
    if "blockedHosts" in data:
        metrics.append(Metric(
            "mysql_router_route_blocked_hosts", labels,
            float(data["blockedHosts"]),
            "Blocked hosts per route", "gauge",
        ))
    return metrics


def fetch_route_health(cfg, opener, route):
    data = api_get(
        cfg["router_url"], "routes/{}/health".format(route),
        cfg["router_user"], cfg["router_password"], opener
    )
    if data is None:
        return []
    labels = {"route": route}
    value = 1.0 if data.get("isAlive") else 0.0
    return [Metric(
        "mysql_router_route_health", labels, value,
        "Route health status", "gauge",
    )]


def fetch_route_connections(cfg, opener, route):
    data = api_get(
        cfg["router_url"], "routes/{}/connections".format(route),
        cfg["router_user"], cfg["router_password"], opener
    )
    if data is None:
        return []
    labels = {"route": route}
    bytes_from = sum(item.get("bytesFromServer", 0) for item in data.get("items", []))
    bytes_to = sum(item.get("bytesToServer", 0) for item in data.get("items", []))
    return [
        Metric(
            "mysql_router_route_bytes_from_server_total", labels,
            float(bytes_from),
            "Total bytes received from server per route", "counter",
        ),
        Metric(
            "mysql_router_route_bytes_to_server_total", labels,
            float(bytes_to),
            "Total bytes sent to server per route", "counter",
        ),
    ]


def fetch_route_destinations(cfg, opener, route):
    data = api_get(
        cfg["router_url"], "routes/{}/destinations".format(route),
        cfg["router_user"], cfg["router_password"], opener
    )
    if data is None:
        return []
    labels = {"route": route}
    count = len(data.get("items", []))
    return [Metric(
        "mysql_router_route_destinations", labels,
        float(count),
        "Number of destinations per route", "gauge",
    )]


def fetch_metadata(cfg, opener):
    data = api_get(
        cfg["router_url"], "metadata",
        cfg["router_user"], cfg["router_password"], opener
    )
    if data is None:
        return []
    metrics = []
    for item in data.get("items", []):
        name = item.get("name")
        if not name:
            continue
        status = api_get(
            cfg["router_url"], "metadata/{}/status".format(name),
            cfg["router_user"], cfg["router_password"], opener
        )
        if status is None:
            continue
        labels = {"instance": name}
        if "refreshSucceeded" in status:
            metrics.append(Metric(
                "mysql_router_metadata_refresh_succeeded", labels,
                1.0 if status["refreshSucceeded"] else 0.0,
                "Whether the last metadata refresh succeeded", "gauge",
            ))
        if "timeLastRefreshSucceeded" in status:
            try:
                ts = (parse_iso(status["timeLastRefreshSucceeded"]) - _epoch).total_seconds()
                metrics.append(Metric(
                    "mysql_router_metadata_last_refresh_timestamp", labels,
                    ts,
                    "Timestamp of the last successful metadata refresh", "gauge",
                ))
            except (ValueError, KeyError) as e:
                logging.warning("Could not parse metadata refresh timestamp: %s", e)
    return metrics


def scrape(cfg, opener):
    metrics = []

    router_metrics = fetch_router_status(cfg, opener)
    metrics.extend(router_metrics)

    up_metric = next((m for m in router_metrics if m.name == "mysql_router_up"), None)
    if up_metric is not None and up_metric.value == 0.0:
        logging.error("MySQL Router is not reachable, skipping route scraping")
        return metrics

    routes = fetch_routes(cfg, opener)
    for route in routes:
        metrics.extend(fetch_route_status(cfg, opener, route))
        metrics.extend(fetch_route_health(cfg, opener, route))
        metrics.extend(fetch_route_connections(cfg, opener, route))
        metrics.extend(fetch_route_destinations(cfg, opener, route))

    metrics.extend(fetch_metadata(cfg, opener))
    return metrics


def _escape_label_value(value):
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def format_metrics(metrics):
    seen_help = set()
    lines = []
    for m in metrics:
        if m.name not in seen_help:
            lines.append("# HELP {} {}".format(m.name, m.help))
            metric_type = "counter" if m.name.endswith("_total") else "gauge"
            lines.append("# TYPE {} {}".format(m.name, metric_type))
            seen_help.add(m.name)
        if m.labels:
            label_str = ",".join(
                '{}="{}"'.format(k, _escape_label_value(str(v)))
                for k, v in sorted(m.labels.items())
            )
            lines.append("{}{{{}}} {}".format(m.name, label_str, repr(m.value)))
        else:
            lines.append("{} {}".format(m.name, repr(m.value)))
    return "\n".join(lines) + "\n"


def push(cfg, payload):
    url = "{}/metrics/job/{}/instance/{}".format(
        cfg["pushgateway_url"].rstrip("/"),
        cfg["pushgateway_job"],
        cfg["pushgateway_instance"],
    )
    headers = {"Content-Type": "text/plain; version=0.0.4"}
    if cfg["pushgateway_user"]:
        headers.update(make_auth_header(cfg["pushgateway_user"], cfg["pushgateway_password"]))

    data = payload.encode("utf-8")
    req = _PutRequest(url, data=data, headers=headers)
    try:
        resp = urllib2.urlopen(req, timeout=10)
        logging.info("Pushed metrics to Pushgateway: HTTP %s", resp.getcode())
        return True
    except urllib2.HTTPError as e:
        logging.error("Pushgateway returned HTTP %s: %s", e.code, e.read().decode("utf-8", errors="replace"))
        return False
    except urllib2.URLError as e:
        logging.error("Cannot reach Pushgateway at %s: %s", url, e.reason)
        return False


def main():
    cfg = load_config()

    logging.basicConfig(
        level=getattr(logging, cfg["log_level"], logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    opener = make_opener(cfg["router_tls_verify"])

    def run_once():
        logging.info("Scraping MySQL Router at %s", cfg["router_url"])
        metrics = scrape(cfg, opener)
        payload = format_metrics(metrics)
        logging.debug("Metrics payload:\n%s", payload)
        ok = push(cfg, payload)
        return ok

    if cfg["scrape_interval"] <= 0:
        ok = run_once()
        raise SystemExit(0 if ok else 1)
    else:
        logging.info("Starting daemon loop, interval=%ds", cfg["scrape_interval"])
        while True:
            run_once()
            time.sleep(cfg["scrape_interval"])


if __name__ == "__main__":
    main()
