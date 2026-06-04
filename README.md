# MySQL Router Prometheus Exporter

Liest alle Metriken aus der MySQL Router REST API und schickt sie per HTTP PUT an ein Prometheus Pushgateway. Implementiert in reinem Python (stdlib only, keine pip-Abhängigkeiten).

Läuft standardmäßig als Daemon-Loop (60-Sekunden-Intervall) oder optional als One-Shot.

---

## router.sh — MySQL Router Setup

`router.sh` bootstrapt und startet MySQL Router als Docker-Container gegen ein bestehendes MySQL-Cluster (InnoDB ReplicaSet / Async Replication).

### Voraussetzungen

- Docker installiert und gestartet
- MySQL-Cluster erreichbar (Primary + Replicas)
- Schreibrechte auf `ROUTER_CONF_DIR` (Default: `./mysql-router-config`)

### Subcommands

| Befehl | Beschreibung |
|--------|--------------|
| `bootstrap` | Verbindet sich mit dem Primary, liest die Cluster-Topologie und schreibt `mysqlrouter.conf` |
| `patch-conf` | Trägt `connection_sharing`, `connection_sharing_delay` und `max_idle_server_connections` in die Classic-Protocol-Sektionen ein (nach bootstrap ausführen) |
| `create-user` | Stellt das REST-API-Auth-Backend auf dateibasiert um und legt den Exporter-User an |
| `stop` | Stoppt den laufenden Router-Container |
| `start` | Startet den Router-Container (entfernt vorher einen gestoppten Container gleichen Namens) |
| `all` | bootstrap → patch-conf → create-user (wenn `EXPORTER_PASSWORD` gesetzt) → start |

### Typischer Erststart

```bash
# Einmalig: Router bootstrappen, konfigurieren und starten
EXPORTER_PASSWORD=geheim ./router.sh all
```

```bash
# Oder schrittweise:
./router.sh bootstrap
./router.sh patch-conf
EXPORTER_PASSWORD=geheim ./router.sh create-user
./router.sh start
```

### Betrieb

```bash
./router.sh stop
./router.sh start
```

### Umgebungsvariablen

| Variable | Default | Beschreibung |
|----------|---------|--------------|
| `PRIMARY_IP` | `192.168.178.218` | IP des Primary-Knotens |
| `PRIMARY_PORT` | `3306` | Port des Primary |
| `REPLICA1_IP` | `192.168.178.219` | IP Replica 1 |
| `REPLICA2_IP` | `192.168.178.220` | IP Replica 2 |
| `CLUSTER_USER` | `clusteradmin` | Admin-User für Bootstrap |
| `CLUSTER_PASSWORD` | `clusteradminsecret` | Admin-Passwort |
| `ROUTER_NAME` | `mysql-router` | Docker-Container-Name |
| `ROUTER_CONF_DIR` | `./mysql-router-config` | Verzeichnis für `mysqlrouter.conf` |
| `MYSQL_VERSION` | `8.0` | Docker-Image-Tag (`mysql/mysql-router`) — aktuellster Tag: `8.0.32` |
| `EXPORTER_USER` | `prometheus` | REST-API-User für den Exporter |
| `EXPORTER_PASSWORD` | — | Passwort für den Exporter-User (Pflicht für `create-user`) |

### Ports nach dem Start

| Port | Protokoll | Funktion |
|------|-----------|----------|
| 6446 | Classic | Read/Write → Primary |
| 6447 | Classic | Read-Only → Replicas |
| 6448 | X Protocol | Read/Write |
| 6449 | X Protocol | Read-Only |
| 8443 | HTTPS | REST API / Metriken |

---

## exporter.py — Prometheus Exporter

### Voraussetzungen

- Python 3.7+
- MySQL Router mit aktivierter REST API (dateibasiertes Auth-Backend, eingerichtet via `router.sh create-user`)
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
| `LOG_FILE` | `/var/log/mysqlrouter-exporter.log` | Pfad zur Log-Datei |

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
├── router.sh     — MySQL Router Setup (Bootstrap, Config, Start/Stop)
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
