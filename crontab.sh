#!/bin/bash
# Installs or removes a per-minute cron job that runs exporter.py in one-shot mode.
# Idempotent: install can be called multiple times without creating duplicates.
#
# Usage:
#   ./crontab.sh install   — add cron entry (default)
#   ./crontab.sh remove    — remove cron entry
#   ./crontab.sh run       — run exporter once now (same command as cron uses)

set -euo pipefail

CRON_TAG="mysqlrouter-exporter"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTER_URL="${ROUTER_URL:-https://127.0.0.1:8443}"
ROUTER_USER="${ROUTER_USER:-prometheus}"
ROUTER_PASSWORD="${ROUTER_PASSWORD:-}"
ROUTER_TLS_VERIFY="${ROUTER_TLS_VERIFY:-false}"
PUSHGATEWAY_URL="${PUSHGATEWAY_URL:-http://gmk.lan:9091}"
PUSHGATEWAY_JOB="${PUSHGATEWAY_JOB:-mysql_router}"
PUSHGATEWAY_USER="${PUSHGATEWAY_USER:-}"
PUSHGATEWAY_PASSWORD="${PUSHGATEWAY_PASSWORD:-}"
LOG_FILE="${LOG_FILE:-/var/log/mysqlrouter-exporter.log}"

CRON_ENV="ROUTER_URL=${ROUTER_URL}"
CRON_ENV="${CRON_ENV} ROUTER_USER=${ROUTER_USER}"
CRON_ENV="${CRON_ENV} ROUTER_PASSWORD=${ROUTER_PASSWORD}"
CRON_ENV="${CRON_ENV} ROUTER_TLS_VERIFY=${ROUTER_TLS_VERIFY}"
CRON_ENV="${CRON_ENV} PUSHGATEWAY_URL=${PUSHGATEWAY_URL}"
CRON_ENV="${CRON_ENV} PUSHGATEWAY_JOB=${PUSHGATEWAY_JOB}"
[[ -n "${PUSHGATEWAY_USER}" ]]     && CRON_ENV="${CRON_ENV} PUSHGATEWAY_USER=${PUSHGATEWAY_USER}"
[[ -n "${PUSHGATEWAY_PASSWORD}" ]] && CRON_ENV="${CRON_ENV} PUSHGATEWAY_PASSWORD=${PUSHGATEWAY_PASSWORD}"
CRON_ENV="${CRON_ENV} SCRAPE_INTERVAL=0"

CRON_LINE="* * * * * ${CRON_ENV} python3 ${SCRIPT_DIR}/exporter.py >> ${LOG_FILE} 2>&1 # ${CRON_TAG}"

install_cron() {
  if crontab -l 2>/dev/null | grep -qF "# ${CRON_TAG}"; then
    echo "==> Cron entry already installed, skipping."
    crontab -l 2>/dev/null | grep -F "# ${CRON_TAG}"
    return 0
  fi

  (crontab -l 2>/dev/null; echo "${CRON_LINE}") | crontab -

  echo "==> Cron entry installed (every minute)."
  echo "    Log : ${LOG_FILE}"
  echo "    Entry:"
  echo "    ${CRON_LINE}"
}

remove_cron() {
  if ! crontab -l 2>/dev/null | grep -qF "# ${CRON_TAG}"; then
    echo "==> No cron entry found, nothing to do."
    return 0
  fi

  crontab -l 2>/dev/null | grep -vF "# ${CRON_TAG}" | crontab -
  echo "==> Cron entry removed."
}

run_once() {
  echo "==> Running exporter now..."
  env ${CRON_ENV} python3 "${SCRIPT_DIR}/exporter.py"
}

CMD="${1:-install}"

case "${CMD}" in
  install) install_cron ;;
  remove)  remove_cron ;;
  run)     run_once ;;
  *)
    echo "Usage: $0 [install|remove|run]"
    exit 1
    ;;
esac
