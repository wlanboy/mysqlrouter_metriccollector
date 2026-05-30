#!/usr/bin/env python3
"""
Load generator — pushes MySQL Router metrics into Grenzbereiche (boundary zones).

Dashboard thresholds (mysql-router-overview.json):
  Aktive Verbindungen: yellow >= 50,  red >= 200
  Neue Verbindungen/s: yellow >= 10,  red >= 50
  Throughput RX/TX:   yellow >= 10 MB, red >= 100 MB

Usage:
  uv run python client/client.py [light|medium|heavy] [--duration N] [--cleanup]
"""
import argparse
import os
import random
import signal
import string
import sys
import threading
import time

try:
    import mysql.connector
except ImportError:
    sys.exit("Install dependency: uv add mysql-connector-python")

ROUTER_HOST = os.environ.get("ROUTER_HOST", "127.0.0.1")
ROUTER_RW_PORT = int(os.environ.get("ROUTER_RW_PORT", "6446"))
ROUTER_RO_PORT = int(os.environ.get("ROUTER_RO_PORT", "6447"))
USER = os.environ.get("DEMO_USER", "demouser")
PASSWORD = os.environ.get("DEMO_PASSWORD", "demosecret123")
DATABASE = os.environ.get("DEMO_DB", "appdb")

# Each level targets a specific Grenzbereich in the dashboard
LEVELS = {
    #                 persistent  burst   bulk   payload_kb
    "light":  dict(persistent=20,  burst=2,  bulk=1, payload_kb=64),    # stays green
    "medium": dict(persistent=60,  burst=5,  bulk=2, payload_kb=256),   # hits yellow
    "heavy":  dict(persistent=220, burst=15, bulk=5, payload_kb=1024),  # hits red
}

_stop = threading.Event()
_lock = threading.Lock()
_stats = {"active": 0, "total": 0, "queries": 0, "bytes_rx": 0, "bytes_tx": 0, "errors": 0}


def _inc(**kw):
    with _lock:
        for k, v in kw.items():
            _stats[k] += v


def _connect(port):
    return mysql.connector.connect(
        host=ROUTER_HOST, port=port,
        user=USER, password=PASSWORD, database=DATABASE,
        connection_timeout=5, autocommit=True,
    )


def _setup():
    conn = _connect(ROUTER_RW_PORT)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS load_data (
            id      INT AUTO_INCREMENT PRIMARY KEY,
            payload MEDIUMTEXT,
            ts      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    seed = "x" * 1024
    for _ in range(200):
        cur.execute("INSERT INTO load_data (payload) VALUES (%s)", (seed,))
    conn.commit()
    conn.close()


def _cleanup():
    try:
        conn = _connect(ROUTER_RW_PORT)
        conn.cursor().execute("DROP TABLE IF EXISTS load_data")
        conn.commit()
        conn.close()
    except Exception as exc:
        print(f"cleanup error: {exc}")


def _persistent_reader(port):
    """Keeps one connection alive and polls continuously — drives active-connection count."""
    while not _stop.is_set():
        try:
            conn = _connect(port)
            _inc(active=1, total=1)
            try:
                cur = conn.cursor()
                while not _stop.is_set():
                    cur.execute("SELECT * FROM load_data LIMIT 500")
                    rows = cur.fetchall()
                    _inc(queries=1, bytes_rx=sum(len(str(r)) for r in rows))
                    time.sleep(0.1)
            finally:
                conn.close()
                _inc(active=-1)
        except Exception:
            _inc(errors=1)
            time.sleep(1)


def _burst_connector():
    """Opens and closes connections at ~50/s — drives new-connections-per-second metric."""
    while not _stop.is_set():
        try:
            conn = _connect(ROUTER_RO_PORT)
            _inc(active=1, total=1)
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.fetchall()
                _inc(queries=1)
            finally:
                conn.close()
                _inc(active=-1)
        except Exception:
            _inc(errors=1)
        time.sleep(0.02)


def _bulk_writer(payload_kb):
    """Inserts large rows — drives TX bytes metric."""
    payload = "".join(random.choices(string.ascii_lowercase, k=payload_kb * 1024))
    while not _stop.is_set():
        try:
            conn = _connect(ROUTER_RW_PORT)
            _inc(active=1, total=1)
            try:
                cur = conn.cursor()
                cur.execute("INSERT INTO load_data (payload) VALUES (%s)", (payload,))
                _inc(queries=1, bytes_tx=len(payload))
            finally:
                conn.close()
                _inc(active=-1)
        except Exception:
            _inc(errors=1)
        time.sleep(0.2)


def _printer():
    Y_CONN, R_CONN = 50, 200
    Y_BYTES, R_BYTES = 10 * 1024 ** 2, 100 * 1024 ** 2

    def zone(v, y, r):
        if v >= r:
            return "RED   "
        if v >= y:
            return "YELLOW"
        return "green "

    header = (
        f"{'Time':8}  {'Active':>6} {'Zone':6}  {'Total':>7}  "
        f"{'Queries':>7}  {'RX MB':>7} {'Zone':6}  {'TX MB':>7} {'Zone':6}  {'Err':>5}"
    )
    print(f"\n{header}")
    print("-" * len(header))

    while not _stop.is_set():
        time.sleep(5)
        with _lock:
            s = dict(_stats)
        print(
            f"{time.strftime('%H:%M:%S')}  "
            f"{s['active']:6d} {zone(s['active'], Y_CONN, R_CONN)}  "
            f"{s['total']:7d}  "
            f"{s['queries']:7d}  "
            f"{s['bytes_rx'] / 1024 ** 2:7.1f} {zone(s['bytes_rx'], Y_BYTES, R_BYTES)}  "
            f"{s['bytes_tx'] / 1024 ** 2:7.1f} {zone(s['bytes_tx'], Y_BYTES, R_BYTES)}  "
            f"{s['errors']:5d}"
        )


def main():
    parser = argparse.ArgumentParser(
        description="MySQL Router load generator — Grenzbereiche",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
levels:
  light   stays green   (20 conn, 2 burst threads)
  medium  hits yellow   (60 conn, 5 burst threads)   [default]
  heavy   hits red     (220 conn, 15 burst threads)

examples:
  uv run python client/client.py medium --duration 120
  uv run python client/client.py heavy
  uv run python client/client.py --cleanup
        """,
    )
    parser.add_argument("level", nargs="?", default="medium",
                        choices=list(LEVELS))
    parser.add_argument("--duration", type=int, default=0,
                        help="Stop after N seconds (0 = Ctrl-C)")
    parser.add_argument("--no-setup", action="store_true",
                        help="Skip table creation (reuse existing load_data)")
    parser.add_argument("--cleanup", action="store_true",
                        help="Drop load_data table and exit")
    args = parser.parse_args()

    if args.cleanup:
        _cleanup()
        print("load_data table dropped.")
        return

    cfg = LEVELS[args.level]
    print(f"Level '{args.level}':")
    print(f"  Persistent connections : {cfg['persistent']:3d}   (yellow>=50, red>=200)")
    print(f"  Burst-connector threads: {cfg['burst']:3d}   (yellow>=10/s new conn, red>=50/s)")
    print(f"  Bulk-writer threads    : {cfg['bulk']:3d}   (yellow>=10 MB TX, red>=100 MB)")
    print(f"  Bulk payload           : {cfg['payload_kb']} KB per insert")
    print()

    if not args.no_setup:
        print("Creating load_data table with 200 seed rows ...")
        _setup()
        print("Done.\n")

    signal.signal(signal.SIGINT, lambda *_: _stop.set())
    signal.signal(signal.SIGTERM, lambda *_: _stop.set())

    threads = []

    rw_count = cfg["persistent"] // 3
    ro_count = cfg["persistent"] - rw_count
    for _ in range(rw_count):
        t = threading.Thread(target=_persistent_reader, args=(ROUTER_RW_PORT,), daemon=True)
        t.start()
        threads.append(t)
    for _ in range(ro_count):
        t = threading.Thread(target=_persistent_reader, args=(ROUTER_RO_PORT,), daemon=True)
        t.start()
        threads.append(t)
    for _ in range(cfg["burst"]):
        t = threading.Thread(target=_burst_connector, daemon=True)
        t.start()
        threads.append(t)
    for _ in range(cfg["bulk"]):
        t = threading.Thread(target=_bulk_writer, args=(cfg["payload_kb"],), daemon=True)
        t.start()
        threads.append(t)

    threading.Thread(target=_printer, daemon=True).start()

    print("Running. Press Ctrl-C to stop.")
    if args.duration > 0:
        _stop.wait(timeout=args.duration)
        _stop.set()
    else:
        _stop.wait()

    print("\nStopping ...")
    _cleanup()
    print("Done.")


if __name__ == "__main__":
    main()
