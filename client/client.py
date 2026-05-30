#!/usr/bin/env python3
"""
Load generator — pushes MySQL Router metrics into Grenzbereiche (boundary zones).

Dashboard thresholds (mysql-router-overview.json):
  Aktive Verbindungen: yellow >= 50,  red >= 200
  Neue Verbindungen/s: yellow >= 10,  red >= 50
  Throughput RX/TX:   yellow >= 10 MB, red >= 100 MB

Usage:
  uv run python client/client.py [nano|light|medium|heavy] [--duration N] [--cleanup]
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

# Each level targets a specific Grenzbereich in the dashboard.
# Tuned for WiFi + small cluster on N150 hardware.
LEVELS = {
    #                persistent  burst  bulk   payload_kb  query_sleep  burst_sleep  write_sleep
    "nano":   dict(persistent=5,   burst=1, bulk=1, payload_kb=16,  query_sleep=5.0, burst_sleep=2.0, write_sleep=5.0),   # well below green
    "light":  dict(persistent=15,  burst=1, bulk=1, payload_kb=64,  query_sleep=2.0, burst_sleep=1.0, write_sleep=3.0),   # stays green
    "medium": dict(persistent=55,  burst=3, bulk=1, payload_kb=256, query_sleep=1.0, burst_sleep=0.5, write_sleep=2.0),   # hits yellow
    "heavy":  dict(persistent=80,  burst=8, bulk=2, payload_kb=512, query_sleep=0.5, burst_sleep=0.2, write_sleep=1.0),   # hits red
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
        connection_timeout=10,
        # read/write timeout for WiFi resilience (seconds)
        # mysql-connector uses these as socket timeouts
        autocommit=True,
        get_warnings=False,
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
    for _ in range(100):
        cur.execute("INSERT INTO load_data (payload) VALUES (%s)", (seed,))
    conn.commit()
    cur.close()
    conn.close()


def _cleanup():
    try:
        conn = _connect(ROUTER_RW_PORT)
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS load_data")
        conn.commit()
        cur.close()
        conn.close()
    except Exception as exc:
        print(f"cleanup error: {exc}")


def _persistent_reader(port, query_sleep):
    """Keeps one connection alive and polls at a controlled rate."""
    while not _stop.is_set():
        conn = None
        try:
            conn = _connect(port)
            _inc(active=1, total=1)
            cur = conn.cursor()
            while not _stop.is_set():
                cur.execute("SELECT id, ts FROM load_data ORDER BY RAND() LIMIT 50")
                rows = cur.fetchall()
                _inc(queries=1, bytes_rx=sum(len(str(r)) for r in rows))
                _stop.wait(timeout=query_sleep)
        except Exception:
            _inc(errors=1)
            _stop.wait(timeout=3)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                _inc(active=-1)


def _burst_connector(burst_sleep):
    """Opens and closes connections at a controlled rate — drives new-conn/s metric."""
    while not _stop.is_set():
        conn = None
        try:
            conn = _connect(ROUTER_RO_PORT)
            _inc(active=1, total=1)
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchall()
            cur.close()
            _inc(queries=1)
        except Exception:
            _inc(errors=1)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                _inc(active=-1)
        _stop.wait(timeout=burst_sleep)


def _bulk_writer(payload_kb, write_sleep):
    """Inserts rows — drives TX bytes metric."""
    payload = "".join(random.choices(string.ascii_lowercase, k=payload_kb * 1024))
    while not _stop.is_set():
        conn = None
        try:
            conn = _connect(ROUTER_RW_PORT)
            _inc(active=1, total=1)
            cur = conn.cursor()
            cur.execute("INSERT INTO load_data (payload) VALUES (%s)", (payload,))
            cur.close()
            _inc(queries=1, bytes_tx=len(payload))
        except Exception:
            _inc(errors=1)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                _inc(active=-1)
        _stop.wait(timeout=write_sleep)


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
        _stop.wait(timeout=10)
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
  nano    well below green  ( 5 conn, 1 burst thread)  [WiFi/N150 safe]
  light   stays green       (15 conn, 1 burst thread)
  medium  hits yellow       (55 conn, 3 burst threads)  [default]
  heavy   hits red          (80 conn, 8 burst threads)

examples:
  uv run python client/client.py nano --duration 300
  uv run python client/client.py medium --duration 120
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
    print(f"  Query interval         : {cfg['query_sleep']}s per persistent reader")
    print(f"  Burst interval         : {cfg['burst_sleep']}s per burst thread")
    print(f"  Write interval         : {cfg['write_sleep']}s per bulk writer")
    print()

    if not args.no_setup:
        print("Creating load_data table with 100 seed rows ...")
        _setup()
        print("Done.\n")

    signal.signal(signal.SIGINT, lambda *_: _stop.set())
    signal.signal(signal.SIGTERM, lambda *_: _stop.set())

    threads = []

    rw_count = cfg["persistent"] // 3
    ro_count = cfg["persistent"] - rw_count
    for _ in range(rw_count):
        t = threading.Thread(
            target=_persistent_reader,
            args=(ROUTER_RW_PORT, cfg["query_sleep"]),
            daemon=True,
        )
        t.start()
        threads.append(t)
        # Stagger thread startup to avoid connection storm
        time.sleep(0.1)
    for _ in range(ro_count):
        t = threading.Thread(
            target=_persistent_reader,
            args=(ROUTER_RO_PORT, cfg["query_sleep"]),
            daemon=True,
        )
        t.start()
        threads.append(t)
        time.sleep(0.1)
    for _ in range(cfg["burst"]):
        t = threading.Thread(
            target=_burst_connector,
            args=(cfg["burst_sleep"],),
            daemon=True,
        )
        t.start()
        threads.append(t)
    for _ in range(cfg["bulk"]):
        t = threading.Thread(
            target=_bulk_writer,
            args=(cfg["payload_kb"], cfg["write_sleep"]),
            daemon=True,
        )
        t.start()
        threads.append(t)

    threading.Thread(target=_printer, daemon=True).start()

    print("Running. Press Ctrl-C to stop.")
    if args.duration > 0:
        _stop.wait(timeout=args.duration)
        _stop.set()
    else:
        _stop.wait()

    print("\nStopping — waiting for connections to close ...")
    # Give worker threads a moment to finish their current operation and close connections
    for t in threads:
        t.join(timeout=15)

    with _lock:
        still_open = _stats["active"]
    if still_open > 0:
        print(f"  Warning: {still_open} connection(s) did not close cleanly.")
    print("Done.")


if __name__ == "__main__":
    main()
