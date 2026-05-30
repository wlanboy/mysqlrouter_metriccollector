# MySQL Router Prometheus Exporter

Liest alle Metriken aus der MySQL Router REST API und schickt sie per HTTP PUT an ein Prometheus Pushgateway. Implementiert in reinem Python (stdlib only, keine pip-Abhängigkeiten).

Läuft standardmäßig als Daemon-Loop (60-Sekunden-Intervall) oder optional als One-Shot.

---

## Voraussetzungen

- Python 3.7+
- MySQL Router mit aktivierter REST API (dateibasiertes Auth-Backend)
- Prometheus Pushgateway

### MySQL Router REST API vorbereiten

Der Exporter benötigt einen dedizierten User im Router. Einmalig einrichten via `router.sh create-user` aus dem übergeordneten Verzeichnis:

```bash
EXPORTER_PASSWORD=geheim ../router.sh create-user
```

Dieser Befehl:
- Stellt das Auth-Backend in `mysqlrouter.conf` auf `file` um
- Legt den User `prometheus` mit dem angegebenen Passwort an

---

## Konfiguration

Alle Parameter werden über Environment Variables gesetzt:

| Variable               | Default                   | Beschreibung                                      |
|------------------------|---------------------------|---------------------------------------------------|
| `ROUTER_URL`           | `https://127.0.0.1:8443`  | MySQL Router REST API Base URL                    |
| `ROUTER_USER`          | `prometheus`              | HTTP Basic Auth User                              |
| `ROUTER_PASSWORD`      | —                         | HTTP Basic Auth Passwort (Pflicht)                |
| `ROUTER_TLS_VERIFY`    | `false`                   | TLS-Zertifikat verifizieren (`true`/`false`)      |
| `PUSHGATEWAY_URL`      | `http://127.0.0.1:9091`   | Prometheus Pushgateway Base URL                   |
| `PUSHGATEWAY_JOB`      | `mysql_router`            | Job-Label im Pushgateway                          |
| `PUSHGATEWAY_INSTANCE` | Hostname                  | Instance-Label im Pushgateway                     |
| `PUSHGATEWAY_USER`     | —                         | Basic Auth User für Pushgateway (optional)        |
| `PUSHGATEWAY_PASSWORD` | —                         | Basic Auth Passwort für Pushgateway (optional)    |
| `SCRAPE_INTERVAL`      | `60`                      | Sekunden zwischen Scrapes; `0` = One-Shot         |
| `LOG_LEVEL`            | `INFO`                    | `DEBUG`, `INFO`, `ERROR`                          |

---

## Ausführung

### Daemon-Loop (Standard, alle 60 Sekunden)

```bash
ROUTER_PASSWORD=geheim \
PUSHGATEWAY_URL=http://prometheus:9091 \
uv run python exporter.py
```

### Daemon-Loop mit eigenem Intervall

```bash
ROUTER_PASSWORD=geheim \
PUSHGATEWAY_URL=http://prometheus:9091 \
SCRAPE_INTERVAL=15 \
uv run python exporter.py
```

### One-Shot (z.B. via cron)

```bash
ROUTER_URL=https://127.0.0.1:8443 \
ROUTER_PASSWORD=geheim \
PUSHGATEWAY_URL=http://prometheus:9091 \
SCRAPE_INTERVAL=0 \
uv run python exporter.py
```

Exit-Code `0` bei Erfolg, `1` wenn das Pushgateway nicht erreichbar war.

### Als Docker-Sidecar

```bash
docker run -d \
  --name mysqlrouter-exporter \
  --network host \
  -e ROUTER_URL=https://127.0.0.1:8443 \
  -e ROUTER_PASSWORD=geheim \
  -e PUSHGATEWAY_URL=http://prometheus:9091 \
  -v "$(pwd)/exporter.py:/app/exporter.py:ro" \
  python:3.12-slim python3 /app/exporter.py
```

---

## Gesammelte Metriken

| Metrikname                                     | Typ     | Labels               | Quelle                    |
|------------------------------------------------|---------|----------------------|---------------------------|
| `mysql_router_up`                              | gauge   | `version`, `edition` | router/status             |
| `mysql_router_process_id`                      | gauge   | —                    | router/status             |
| `mysql_router_uptime_seconds`                  | gauge   | —                    | router/status (berechnet) |
| `mysql_router_route_health`                    | gauge   | `route`              | routes/{r}/health         |
| `mysql_router_route_active_connections`        | gauge   | `route`              | routes/{r}/status         |
| `mysql_router_route_total_connections_total`   | counter | `route`              | routes/{r}/status         |
| `mysql_router_route_blocked_hosts`             | gauge   | `route`              | routes/{r}/status         |
| `mysql_router_route_bytes_from_server_total`   | counter | `route`              | routes/{r}/connections    |
| `mysql_router_route_bytes_to_server_total`     | counter | `route`              | routes/{r}/connections    |
| `mysql_router_route_destinations`              | gauge   | `route`              | routes/{r}/destinations   |
| `mysql_router_metadata_refresh_succeeded`      | gauge   | `instance`           | metadata/{i}/status       |
| `mysql_router_metadata_last_refresh_timestamp` | gauge   | `instance`           | metadata/{i}/status       |

### Beispiel-Output (Prometheus Text Format)

```
# HELP mysql_router_up MySQL Router is reachable
# TYPE mysql_router_up gauge
mysql_router_up{edition="MySQL Community - GPL",version="8.0.36"} 1.0
# HELP mysql_router_process_id MySQL Router process ID
# TYPE mysql_router_process_id gauge
mysql_router_process_id 1234.0
# HELP mysql_router_uptime_seconds MySQL Router uptime in seconds
# TYPE mysql_router_uptime_seconds gauge
mysql_router_uptime_seconds 3600.0
# HELP mysql_router_route_active_connections Active connections per route
# TYPE mysql_router_route_active_connections gauge
mysql_router_route_active_connections{route="bootstrap_rw"} 3.0
mysql_router_route_active_connections{route="bootstrap_ro"} 1.0
```

---

## Fehlerverhalten

| Situation                    | Verhalten                                                               |
|------------------------------|-------------------------------------------------------------------------|
| Router nicht erreichbar      | `mysql_router_up 0` pushen, alle anderen Metriken weglassen             |
| Einzelner Endpunkt 4xx/5xx   | Endpunkt überspringen, Warnung loggen, übrige Metriken trotzdem pushen  |
| Pushgateway nicht erreichbar | Fehler loggen; Exit-Code 1 (One-Shot) bzw. nächsten Zyklus abwarten    |
| JSON-Parse-Fehler            | Endpunkt überspringen, Warnung loggen                                   |

---

## Projektstruktur

```
mysqlrouter_metriccollector/
├── exporter.py   — Hauptscript (einzelne Datei, keine weiteren Module)
├── spec.md       — Technische Spezifikation
└── README.md     — Diese Datei
```

---

## Technische Details

- **Keine externen Abhängigkeiten** — ausschließlich Python-Stdlib (`urllib`, `ssl`, `base64`, `json`, `socket`, `logging`)
- **TLS** — selbstsignierte Zertifikate des Routers werden per Default akzeptiert (`ROUTER_TLS_VERIFY=false`)
- **Pushgateway-Protokoll** — HTTP PUT auf `/metrics/job/{job}/instance/{instance}`, Content-Type `text/plain; version=0.0.4`
- **Python-Kompatibilität** — Python 3.7+
