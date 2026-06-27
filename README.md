# MySQL Router Prometheus Exporter

Liest alle Metriken aus der MySQL Router REST API und schickt sie per HTTP PUT an ein Prometheus Pushgateway. Implementiert in reinem Python (stdlib only, keine pip-Abhängigkeiten).

Läuft standardmäßig als Daemon-Loop (60-Sekunden-Intervall) oder optional als One-Shot.

---

## Voraussetzungen

- Docker installiert und gestartet
- MySQL-Cluster erreichbar (Primary + Replicas)


## REST-API-User manuell anlegen

Nach dem Bootstrap setzt MySQL Router das Auth-Backend der REST API auf `metadata_cache` — d.h. Credentials werden gegen die MySQL-Metadaten geprüft. Ein dedizierter Exporter-User funktioniert damit nicht. Das Backend muss auf `file` umgestellt werden, dann verwaltet eine lokale Passwortdatei die Zugangsdaten.

### Schritt 1 — `mysqlrouter.conf` anpassen

In `/etc/mysqlrouter/mysqlrouter.conf` (oder dem konfigurierten `--directory`) die Sektion `[http_auth_backend:...]` suchen und zwei Werte setzen:

```ini
[http_auth_backend:default_auth_backend]
backend=file
filename=/etc/mysqlrouter/prometheus-users.conf
```

> Der Sektionsname kann nach dem Bootstrap leicht variieren (`default_auth_backend` ist der Regelfall). Im Zweifel mit `grep '\[http_auth_backend' mysqlrouter.conf` prüfen.

### Schritt 2 — Exporter-User anlegen

Das Tool `mysqlrouter_passwd` schreibt einen Eintrag (bcrypt-Hash) in die Passwortdatei:

```bash
# Nativ (RPM/DEB):
printf 'geheim\ngeheim\n' | sudo mysqlrouter_passwd set \
  /etc/mysqlrouter/prometheus-users.conf prometheus

# Docker — Image enthält mysqlrouter_passwd:
printf 'geheim\ngeheim\n' | docker run --rm -i \
  -v /pfad/zum/config-verzeichnis:/etc/mysqlrouter \
  mysql/mysql-router:8.0 \
  mysqlrouter_passwd set /etc/mysqlrouter/prometheus-users.conf prometheus
```

Anschließend Datei-Permissions sichern (der Router-Prozess muss lesen dürfen):

```bash
sudo chown mysqlrouter:mysqlrouter /etc/mysqlrouter/prometheus-users.conf
sudo chmod 640 /etc/mysqlrouter/prometheus-users.conf
```

### Schritt 3 — Router neu starten und testen

```bash
# Systemd:
sudo systemctl restart mysqlrouter

# Docker:
docker restart mysql-router
```

Zugriff prüfen (TLS-Zertifikat des Routers ist selbstsigniert, daher `-k`):

```bash
curl -sk -u prometheus:geheim \
  https://127.0.0.1:8443/api/20190715/router/status | jq
```

Erwartete Antwort:

```json
{
  "processId": 1234,
  "productEdition": "MySQL Community - GPL",
  "timeStarted": "2026-06-04T05:00:00Z",
  "version": "8.0.32",
  "hostname": "myhost"
}
```

> Vollständige Anleitung für RPM/DEB-Installationen: [router.md](router.md)

---

## exporter.py — Prometheus Exporter

### Voraussetzungen

- Python 3.7+
- MySQL Router mit aktivierter REST API (dateibasiertes Auth-Backend, eingerichtet via `router.sh create-user` oder manuell wie oben beschrieben)
- Prometheus Pushgateway

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
| `SCRAPE_INTERVAL`      | `15`                      | Sekunden zwischen Scrapes; `0` = One-Shot         |
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
PUSHGATEWAY_URL=http://gmk.lan:9091 \
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

### Via cron (crontab.sh)

`crontab.sh` richtet einen minütlichen Cron-Job ein, der `exporter.py` im One-Shot-Modus ausführt (`SCRAPE_INTERVAL=0`). Idempotent — mehrfaches Aufrufen erstellt keinen Duplikat-Eintrag.

```bash
# Installieren
ROUTER_PASSWORD=geheim ./crontab.sh install

# Mit allen Optionen
ROUTER_PASSWORD=geheim \
PUSHGATEWAY_URL=http://gmk.lan:9091 \
./crontab.sh install

# Entfernen
./crontab.sh remove
```

Umgebungsvariablen für `crontab.sh`:

| Variable | Default | Beschreibung |
|----------|---------|--------------|
| `ROUTER_URL` | `https://127.0.0.1:8443` | MySQL Router REST API URL |
| `ROUTER_USER` | `prometheus` | REST API User |
| `ROUTER_PASSWORD` | — | REST API Passwort |
| `ROUTER_TLS_VERIFY` | `false` | TLS-Zertifikat verifizieren |
| `PUSHGATEWAY_URL` | `http://gmk.lan:9091` | Pushgateway URL |
| `PUSHGATEWAY_JOB` | `mysql_router` | Job-Label |
| `PUSHGATEWAY_USER` | — | Pushgateway Basic Auth User (optional) |
| `PUSHGATEWAY_PASSWORD` | — | Pushgateway Basic Auth Passwort (optional) |
| `LOG_FILE` | `<script-dir>/mysqlrouter-exporter.log` | Pfad zur Log-Datei |

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
├── router.sh     — MySQL Router Setup als Docker-Container (Bootstrap, Config, Start/Stop)
├── router.md     — Anleitung für RPM/DEB-Installation: REST API und Exporter-User einrichten
├── exporter.py   — Prometheus Exporter (einzelne Datei, keine weiteren Module)
├── crontab.sh    — Cron-Job einrichten / entfernen (minütlicher One-Shot)
├── spec.md       — Technische Spezifikation
└── README.md     — Diese Datei
```

---

## Technische Details

- **Keine externen Abhängigkeiten** — ausschließlich Python-Stdlib (`urllib`, `ssl`, `base64`, `json`, `socket`, `logging`)
- **TLS** — selbstsignierte Zertifikate des Routers werden per Default akzeptiert (`ROUTER_TLS_VERIFY=false`)
- **Pushgateway-Protokoll** — HTTP PUT auf `/metrics/job/{job}/instance/{instance}`, Content-Type `text/plain; version=0.0.4`
- **Python-Kompatibilität** — Python 3.7+
