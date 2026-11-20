#!/usr/bin/env python3
"""MAX! web server: logs RF telegrams, serves a web UI and sends commands.

  python max_server.py [--port COM3 | tcp://192.168.1.50:2323] [--db max.db] [--host 127.0.0.1] [--http 8080]

One thread owns the serial port (receive + send), the HTTP server only talks
to it through a queue.  Stop max_logger.py / max_send.py first.
"""
import argparse
import collections
import json
import os
import queue
import re
import socket
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import serial

import max_alerts
import max_backup
from max_auth import COOKIE, ROLES, Auth, get_setting, put_setting
from max_logger import (DEVICE_TYPES, MSG_TYPES, SCHEMA, decode,
                        parse_telegram, store)
from max_mqtt import DEFAULTS as MQTT_DEFAULTS
from max_mqtt import Bridge, load_config as mqtt_load
from max_send import (DAY_ID, MAX_ATTEMPTS, build_packet, build_raw,
                      build_time_packet, week_profile_packets)

WEB_DIR = Path(__file__).with_name("web")
ACK_TIMEOUT = 5.5
EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS names (addr TEXT PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS device_config (
    addr TEXT PRIMARY KEY, cfg TEXT, sent_at TEXT);
CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS device_room (
    addr TEXT PRIMARY KEY, room_id INTEGER REFERENCES rooms(id));
CREATE TABLE IF NOT EXISTS profiles (
    addr TEXT, day TEXT, points TEXT, sent_at TEXT, PRIMARY KEY (addr, day));
"""


class TcpLink:
    """culfw over its network port (telnet-like, default 2323), same API as
    the parts of serial.Serial that we use."""

    def __init__(self, host, port):
        self.sock = socket.create_connection((host, port), timeout=5)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self.sock.settimeout(0.2)

    def read(self, n):
        try:
            data = self.sock.recv(n)
        except socket.timeout:
            return b""
        if not data:
            raise ConnectionError("connection closed by the Cube")
        return data

    def write(self, data):
        self.sock.sendall(data)

    def close(self):
        self.sock.close()


def open_link(target):
    """'COM3' / '/dev/ttyUSB0' -> serial port; 'tcp://host[:port]' or
    'host:port' -> culfw over the network."""
    m = re.fullmatch(r"(?:tcp://)?([A-Za-z0-9._-]+)(?::(\d+))?", target)
    if target.lower().startswith("tcp://") or (
            m and m.group(2) and not re.fullmatch(r"(?i)com\d+", m.group(1))):
        return TcpLink(m.group(1), int(m.group(2) or 2323))
    return serial.Serial(target, 38400, timeout=0.2)


class Job:
    def __init__(self, dst, temp, mode):
        self.dst, self.temp, self.mode = dst, temp, mode
        self.done = threading.Event()
        self.result = {"ok": False, "error": "pending"}
        self.attempt = 0
        self.cnt = None
        self.packet = None
        self.kind = "set"       # "set", "time" or "raw" (prebuilt packet)
        self.raw = None         # (packet, cnt) for kind "raw"
        self.tag = None         # optional bookkeeping object (pairing event)
        self.cancelled = False  # set when nobody waits for the result any more
        self.max_wait = 90      # seconds to wait for send credit
        self.state = "new"      # new -> credit -> ack -> finished
        self.deadline = 0.0


class SerialWorker(threading.Thread):
    def __init__(self, port, db_path, src):
        super().__init__(daemon=True)
        self.port, self.db_path, self.src = port, db_path, src
        self.jobs = queue.Queue()
        self.job = None
        self.time_sync = {}     # addr -> ISO timestamp of last successful sync
        self.time_queued = {}   # addr -> time.time() of last queued sync
        self.prio = queue.Queue()    # time-critical jobs (pairing answers)
        self.paused = []             # jobs preempted by a pairing answer
        self.last_credit_poll = 0.0
        self.pairing_until = 0.0     # pairing mode is on until this time
        self.pair_events = []        # recent pairing requests (newest last)
        self.status = {"connected": False, "version": "", "error": "",
                       "last_telegram": None, "credit": None}
        self.listeners = []          # callables(addr) after a stored telegram
        self.console = collections.deque(maxlen=1500)   # live console entries
        self.console_n = 0
        self.console_lock = threading.Lock()

    def log_console(self, direction, raw, t=None, desc="", note=""):
        e = {"dir": direction, "ts": datetime.now().strftime("%H:%M:%S.%f")[:-3],
             "raw": raw, "note": note}
        if t:
            e.update(src=t["src"], dst=t["dst"], cnt=t["msgcnt"], flags=t["flags"],
                     type=t["type"], type_name=MSG_TYPES.get(
                         t["type"], "0x%02X" % t["type"]),
                     grp=t["grp"], payload=t["payload"].hex().upper(),
                     rssi=t["rssi"], desc=desc)
        with self.console_lock:
            self.console_n += 1
            e["n"] = self.console_n
            self.console.append(e)

    def console_since(self, since):
        with self.console_lock:
            return [e for e in self.console if e["n"] > since], self.console_n

    # ------------------------------------------------------------- serial
    def open(self):
        ser = open_link(self.port)
        ser.write(b"V\n")
        time.sleep(0.5)
        self.status["version"] = ser.read(512).decode(errors="replace").strip()
        ser.write(b"Zr\n")
        self.status.update(connected=True, error="")
        return ser

    def run(self):
        db = sqlite3.connect(self.db_path)
        ser, buf = None, ""
        while True:
            try:
                if ser is None:
                    ser = self.open()
                    buf = ""
                buf += ser.read(256).decode(errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    self.handle_line(db, line.strip())
                self.drive_job(ser)
            except (serial.SerialException, OSError) as e:
                self.status.update(connected=False, error=str(e))
                if self.job:
                    self.finish(False, "serial port lost")
                try:
                    if ser: ser.close()
                except Exception:
                    pass
                ser = None
                time.sleep(5)

    # ----------------------------------------------------------- handling
    def handle_line(self, db, line):
        m = re.fullmatch(r"\d+\s+(\d+)", line)
        if m:                                   # reply to 'X' (credit)
            self.status["credit"] = int(m.group(1))
            self.status["credit_last"] = int(m.group(1))
            self.log_console("info", line, note="Sendekontingent %d (%d Sendungen)"
                             % (int(m.group(1)), int(m.group(1)) // 106))
            return
        t = parse_telegram(line)
        if not t:
            if line:
                self.log_console("info", line)
            return
        ts = datetime.now().isoformat(timespec="seconds")
        desc, reading, extra = decode(t)
        store(db, ts, t, desc, reading, extra)
        self.log_console("rx", line, t, desc)
        self.status["last_telegram"] = ts
        for fn in self.listeners:
            try:
                fn(t["src"])
            except Exception:
                pass
        if t["type"] == 0x03 and t["dst"] == self.src:
            self.maybe_sync_time(t)
        if t["type"] == 0x00:
            self.handle_pair_ping(t)
        j = self.job
        if (j and j.state == "ack" and t["type"] == 0x02
                and t["src"] == j.dst and t["dst"] == self.src):
            p = t["payload"]
            ok = len(p) >= 1 and p[0] == 1
            self.finish(ok, None if ok else "thermostat rejected the command")

    def handle_pair_ping(self, t):
        """A device announces itself (PairPing).  Only while the user has
        switched pairing mode on do we answer with a PairPong, which binds the
        device to our RF address."""
        p = t["payload"]
        if time.time() >= self.pairing_until or len(p) < 13:
            return
        addr, dst = t["src"], t["dst"]
        now = datetime.now().isoformat(timespec="seconds")
        ev = next((e for e in self.pair_events if e["addr"] == addr
                   and time.time() - e["t"] < 30), None)
        if ev and ev["status"] in ("credit", "noack") and self.job is None:
            # an earlier answer failed: the device keeps pinging, so try again
            ev["count"] += 1
            self._pong(ev)
            return
        if ev:
            ev["count"] += 1
            return                      # same request repeating, already handled
        ev = {"addr": addr, "ts": now, "t": time.time(), "count": 1,
              "kind": DEVICE_TYPES.get(p[1], "type%d" % p[1]),
              "serial": p[3:13].decode("ascii", "replace"), "dst": dst,
              "status": "pending"}
        self.pair_events.append(ev)
        del self.pair_events[:-12]
        if dst not in ("000000", self.src):
            ev["status"] = "other"      # bound to another controller
            return
        self._pong(ev)

    def _pong(self, ev):
        job = Job(ev["addr"], 0.0, "raw")
        job.kind, job.max_wait = "raw", 30
        job.raw = build_raw(self.src, ev["addr"], 0x01, bytes([0]))   # PairPong
        job.tag = ev
        ev["status"] = "sending"
        ev.pop("wait", None)
        self.prio.put(job)

    def maybe_sync_time(self, t):
        """A device asks for (or reports) its clock: answer if it is off."""
        p, addr = t["payload"], t["src"]
        if len(p) >= 5:
            try:
                dev = datetime(2000 + p[0], ((p[3] >> 6) << 2) | (p[4] >> 6),
                               p[1], p[2] & 0x1F, p[3] & 0x3F, p[4] & 0x3F)
                if abs((datetime.now() - dev).total_seconds()) <= 10:
                    return                       # clock is fine
            except ValueError:
                pass                             # garbage -> resync
        if time.time() - self.time_queued.get(addr, 0) < 60:
            return                               # don't flood the 1% budget
        self.time_queued[addr] = time.time()
        job = Job(addr, 0.0, "time")
        job.kind = "time"
        self.jobs.put(job)

    def finish(self, ok, error):
        j = self.job
        if not j:
            return
        if j.kind == "time" and ok:
            self.time_sync[j.dst] = datetime.now().isoformat(timespec="seconds")
        if j.tag is not None and j.tag.get("status") != "credit":   # pairing
            j.tag["status"] = "paired" if ok else "noack"
        j.result = {"ok": ok, "error": error, "attempts": j.attempt}
        j.state = "finished"
        j.done.set()
        self.job = None

    def drive_job(self, ser):
        now = time.time()
        if (self.job is not None and self.job.tag is None
                and self.job.state in ("credit_req", "credit_wait")
                and not self.prio.empty()):
            self.job.state = "credit_req"          # preempt: pairing is urgent
            self.paused.append(self.job)
            self.job = None
        if self.job is None:
            try:                                   # pairing answers go first
                self.job = self.prio.get_nowait()
            except queue.Empty:
                try:
                    self.job = (self.paused.pop() if self.paused
                                else self.jobs.get_nowait())
                    if self.job.state != "new" and self.job.state != "finished":
                        self.job.next = 0.0
                        return                      # resume next loop
                except queue.Empty:
                    # idle: keep the send-credit display fresh
                    if now - self.last_credit_poll > 20:
                        self.last_credit_poll = now
                        ser.write(b"X\n")
                    return
            j = self.job
            if j.cancelled:                        # the HTTP request gave up
                self.job = None
                j.done.set()
                return
            if j.kind == "raw":
                j.packet, j.cnt = j.raw
            elif j.kind == "time":
                j.packet, j.cnt = build_time_packet(self.src, j.dst)
            else:
                j.packet, j.cnt = build_packet(self.src, j.dst, j.temp, j.mode)
            j.started, j.next = now, 0.0
            j.state = "credit_req"
        j = self.job
        if j.cancelled and j.state != "ack":
            self.job = None
            j.done.set()
            return
        if j.state == "credit_req" and now >= j.next:
            if j.attempt >= MAX_ATTEMPTS:
                return self.finish(False, "no acknowledgement from thermostat")
            if now - j.started > j.max_wait:
                return self.finish(False, "not enough send credit (duty cycle)")
            self.status["credit"] = None
            ser.write(b"X\n")
            j.state, j.deadline = "credit_wait", now + 3
        elif j.state == "credit_wait":
            credit = self.status["credit"]
            need = 100 + len(j.packet) * 4 // 10 + 1   # 1 s preamble + payload
            if credit is None:
                if now > j.deadline:
                    self.finish(False, "culfw did not report its send credit")
            elif credit >= need:
                j.attempt += 1
                if j.kind == "time":     # always send the current time
                    j.packet, _ = build_time_packet(self.src, j.dst, j.cnt)
                ser.write(f"Zs{j.packet}\n".encode())
                tx = parse_telegram("Z" + j.packet)
                self.log_console("tx", "Zs" + j.packet, tx,
                                 decode(tx)[0] if tx else "",
                                 "Versuch %d" % j.attempt)
                j.state, j.deadline = "ack", now + ACK_TIMEOUT
            elif j.tag is not None:
                # Pairing answers are time critical: don't wait for credit
                j.tag["status"] = "credit"
                j.tag["wait"] = need - credit
                self.finish(False, "no send credit")
            else:
                j.state, j.next = "credit_req", now + 5
        elif j.state == "ack" and now >= j.deadline:
            j.state, j.next = "credit_req", 0.0


# ------------------------------------------------------------------ HTTP
def q(db_path, sql, args=()):
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in db.execute(sql, args)]
    finally:
        db.close()


def build_state(db_path, worker):
    devices = q(db_path, "SELECT d.*, n.name, r.room_id FROM devices d "
                         "LEFT JOIN names n ON n.addr = d.addr "
                         "LEFT JOIN device_room r ON r.addr = d.addr "
                         "ORDER BY d.addr")
    for d in devices:
        for field in ("measured", "setpoint", "valve", "mode", "is_open"):
            r = q(db_path, f"SELECT {field} AS v, ts FROM readings WHERE addr=? "
                           f"AND {field} IS NOT NULL ORDER BY id DESC LIMIT 1",
                  (d["addr"],))
            d[field] = r[0]["v"] if r else None
            d[field + "_ts"] = r[0]["ts"] if r else None
        r = q(db_path, "SELECT rssi_dbm FROM telegrams WHERE src=? "
                       "ORDER BY id DESC LIMIT 1", (d["addr"],))
        d["rssi"] = r[0]["rssi_dbm"] if r else None
        d["time_sync"] = worker.time_sync.get(d["addr"])
        d["display_actual"] = None          # wall thermostat: shows actual temp?
        if d["kind"] == "WallThermostat":
            r = q(db_path, "SELECT payload FROM telegrams WHERE src=? AND "
                           "type=112 ORDER BY id DESC LIMIT 1", (d["addr"],))
            if r and len(r[0]["payload"]) >= 4:
                # byte 1: 0x04 = display shows the actual temperature
                # (0x00 / 0x01 = setpoint, e.g. after a factory reset)
                d["display_actual"] = bool(int(r[0]["payload"][2:4], 16) & 4)
    rooms = q(db_path, "SELECT id, name FROM rooms ORDER BY name COLLATE NOCASE")
    pairing = {"remaining": max(0, round(worker.pairing_until - time.time())),
               "events": [{k: v for k, v in e.items() if k != "t"}
                          for e in worker.pair_events]}
    alerts = q(db_path, "SELECT addr, kind, since FROM alerts WHERE cleared IS NULL "
                        "ORDER BY since")
    return {"devices": devices, "rooms": rooms, "pairing": pairing, "alerts": alerts,
            "now": datetime.now().isoformat(timespec="seconds"),
            "status": worker.status, "source": worker.src}


CFG_DEFAULT = {"comfort": 21.0, "eco": 17.0, "max": 30.5, "min": 4.5,
               "offset": 0.0, "window_temp": 12.0, "window_min": 15}


def validate_config(cfg):
    """Return a clean dict or raise ValueError."""
    out = {}
    for k in ("comfort", "eco", "max", "min", "window_temp"):
        v = float(cfg[k])
        if not 4.5 <= v <= 30.5 or (v * 2) % 1:
            raise ValueError(f"{k}: 4.5 .. 30.5 in 0.5 steps")
        out[k] = v
    off = float(cfg["offset"])
    if not -3.5 <= off <= 3.5 or (off * 2) % 1:
        raise ValueError("offset: -3.5 .. 3.5 in 0.5 steps")
    out["offset"] = off
    wm = int(cfg["window_min"])
    if not 0 <= wm <= 60 or wm % 5:
        raise ValueError("window_min: 0 .. 60 in 5 minute steps")
    out["window_min"] = wm
    if not out["min"] <= out["eco"] <= out["comfort"] <= out["max"]:
        raise ValueError("need min <= eco <= comfort <= max")
    return out


def config_payload(c):
    """ConfigTemperatures payload (7 bytes), as in FHEM MAX.pm."""
    return bytes([int(c["comfort"] * 2), int(c["eco"] * 2), int(c["max"] * 2),
                  int(c["min"] * 2), int((c["offset"] + 3.5) * 2),
                  int(c["window_temp"] * 2), c["window_min"] // 5])


def find_candidates(db_path, worker, since):
    """Devices that just did something a person can trigger: turned knob
    (SetTemperature), window opened/closed, wall thermostat setpoint changed,
    push button.  Periodic status telegrams that change nothing are ignored.
    since=0 only returns the current telegram id (baseline)."""
    last = q(db_path, "SELECT COALESCE(MAX(id),0) AS m FROM telegrams")[0]["m"]
    if since <= 0:
        return {"last_id": last, "candidates": []}
    base = q(db_path, "SELECT ts FROM telegrams WHERE id=?", (since,))
    base_ts = base[0]["ts"] if base else "0000"
    rows = q(db_path, "SELECT id, ts, type, src, dst, payload FROM telegrams "
                      "WHERE id>? AND type IN (48,64,66,80) AND src NOT IN "
                      "('000000',?) ORDER BY id", (since, worker.src))
    found = {}
    for r in rows:
        p = bytes.fromhex(r["payload"])
        ev = None
        prev = q(db_path, "SELECT is_open, setpoint FROM readings WHERE addr=? "
                          "AND ts<=? ORDER BY id DESC LIMIT 1",
                 (r["src"], base_ts))
        prev = prev[0] if prev else {}
        if r["type"] == 0x30 and p:                       # window contact
            is_open = (p[0] >> 1) & 1
            if prev.get("is_open") is None or prev["is_open"] != is_open:
                ev = {"event": "open" if is_open else "closed"}
        elif r["type"] == 0x40 and p:                     # knob turned
            ev = {"event": "knob", "to": (p[0] & 0x3F) / 2.0}
        elif r["type"] == 0x42 and p:                     # wall thermostat
            sp = (p[0] & 0x7F) / 2.0
            if prev.get("setpoint") is not None and prev["setpoint"] != sp:
                ev = {"event": "setpoint", "from": prev["setpoint"], "to": sp}
        elif r["type"] == 0x50:                           # push button
            ev = {"event": "button"}
        if ev:
            ev.update(addr=r["src"], ts=r["ts"], id=r["id"])
            found[r["src"]] = ev                          # keep the latest
    out = []
    for addr, ev in sorted(found.items(), key=lambda kv: -kv[1]["id"]):
        info = q(db_path, "SELECT d.kind, n.name, r.room_id FROM devices d "
                          "LEFT JOIN names n ON n.addr=d.addr "
                          "LEFT JOIN device_room r ON r.addr=d.addr "
                          "WHERE d.addr=?", (addr,))
        ev.update(info[0] if info else {"kind": None, "name": None,
                                        "room_id": None})
        out.append(ev)
    return {"last_id": last, "candidates": out}


def run_set(worker, addr, temp, auto):
    """Queue a SetTemperature job and wait for the result dict."""
    job = Job(addr, 0.0 if auto else temp, "auto" if auto else "manual")
    worker.jobs.put(job)
    if not job.done.wait(30):
        job.cancelled = True
        return {"ok": False, "error": "timeout (no send credit or no answer)"}
    return job.result


class Handler(BaseHTTPRequestHandler):
    server_version = "MAXserver/1"

    def log_message(self, *a):
        pass

    def reply(self, code, obj, ctype="application/json", cookie=None, headers=()):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in headers:
            self.send_header(k, v)
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------- auth
    def token(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return None

    def user(self):
        if not hasattr(self, "_user"):
            self._user = self.server.auth.user_for_token(self.token())
        return self._user

    @staticmethod
    def cookie(token, max_age=30 * 24 * 3600):
        return (f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; "
                f"Max-Age={max_age}")

    def allowed(self, role):
        """Check the logged-in user against a minimum role; replies on failure."""
        u = self.user()
        if not u:
            self.reply(401, {"error": "Anmeldung erforderlich"})
            return False
        if ROLES.index(u["role"]) > ROLES.index(role):      # admin=0 .. viewer=2
            self.reply(403, {"error": "Dafür fehlt die Berechtigung"})
            return False
        return True

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        db, worker = self.server.db_path, self.server.worker
        if u.path in ("/", "/index.html"):
            return self.reply(200, (WEB_DIR / "index.html").read_bytes(),
                              "text/html; charset=utf-8")
        if u.path in ("/logo.svg", "/favicon.svg", "/favicon.ico"):
            f = WEB_DIR / "logo.svg"
            return self.reply(200, f.read_bytes(), "image/svg+xml",
                              headers=[("Cache-Control", "public, max-age=3600")])
        if u.path == "/api/auth":
            return self.reply(200, {"setup": self.server.auth.user_count() == 0,
                                    "user": self.user()})
        if not self.allowed("viewer"):
            return
        if u.path == "/api/console":
            since = int((qs.get("since") or ["0"])[0] or 0)
            entries, last = worker.console_since(since)
            return self.reply(200, {"last": last, "entries": entries[-400:]})
        if u.path == "/api/alerts":
            out = {"open": max_alerts.open_alerts(db), "history": max_alerts.history(db)}
            if self.user()["role"] == "admin":
                cfg = max_alerts.load(db)
                cfg["email"]["password_set"] = bool(cfg["email"].pop("password", ""))
                out.update(settings=cfg, status=self.server.alerter.status)
            return self.reply(200, out)
        if u.path == "/api/users":
            if not self.allowed("admin"):
                return
            return self.reply(200, {"users": self.server.auth.list_users(),
                                    "roles": list(ROLES)})
        if u.path == "/api/backups":
            if not self.allowed("admin"):
                return
            return self.reply(200, {"backups": max_backup.listing(db),
                                    "settings": max_backup.settings(db),
                                    "dir": str(max_backup.backup_dir(db)),
                                    "error": self.server.autobackup.last_error})
        if u.path == "/api/backup/download":
            if not self.allowed("admin"):
                return
            try:
                f = max_backup.path_of(db, (qs.get("name") or [""])[0])
            except ValueError as e:
                return self.reply(404, {"error": str(e)})
            return self.reply(200, f.read_bytes(), "application/octet-stream",
                              headers=[("Content-Disposition",
                                        'attachment; filename="%s"' % f.name)])
        if u.path == "/api/mqtt":
            if not self.allowed("admin"):
                return
            cfg = mqtt_load(db)
            cfg["password_set"] = bool(cfg.pop("password", ""))
            return self.reply(200, {"config": cfg, "status": self.server.bridge.status})
        if u.path == "/api/state":
            return self.reply(200, build_state(db, worker))
        if u.path == "/api/history":
            addr = (qs.get("addr") or [""])[0].upper()
            hours = min(float((qs.get("hours") or ["24"])[0]), 24 * 90)
            since = (datetime.now() - timedelta(hours=hours)).isoformat(
                timespec="seconds")
            rows = q(db, "SELECT ts, measured, setpoint, valve, is_open FROM "
                         "readings WHERE addr=? AND ts>=? ORDER BY id LIMIT 5000",
                     (addr, since))
            return self.reply(200, rows)
        if u.path == "/api/profile":
            addr = (qs.get("addr") or [""])[0].upper()
            rows = q(db, "SELECT day, points, sent_at FROM profiles WHERE addr=?",
                     (addr,))
            return self.reply(200, {r["day"]: {"points": json.loads(r["points"]),
                                               "sent_at": r["sent_at"]}
                                    for r in rows})
        if u.path == "/api/config":
            addr = (qs.get("addr") or [""])[0].upper()
            r = q(db, "SELECT cfg, sent_at FROM device_config WHERE addr=?", (addr,))
            if r:
                return self.reply(200, {"cfg": json.loads(r[0]["cfg"]), "stored": True,
                                        "sent_at": r[0]["sent_at"],
                                        "defaults": CFG_DEFAULT})
            return self.reply(200, {"cfg": CFG_DEFAULT, "stored": False,
                                    "sent_at": None, "defaults": CFG_DEFAULT})
        if u.path == "/api/find":
            return self.reply(200, find_candidates(
                db, worker, int((qs.get("since") or ["0"])[0] or 0)))
        if u.path == "/api/log":
            rows = q(db, "SELECT ts, src, dst, type_name, rssi_dbm, payload "
                         "FROM telegrams ORDER BY id DESC LIMIT 60")
            return self.reply(200, rows)
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        # CSRF guard: JSON content type forces a CORS preflight, and the
        # Origin (if any) must be this very server.
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if (self.headers.get("Content-Type", "").split(";")[0] != "application/json"
                or (origin and urlparse(origin).netloc != host)):
            return self.reply(403, {"error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self.reply(400, {"error": "bad json"})
        db, worker = self.server.db_path, self.server.worker
        auth = self.server.auth

        # ---- accounts
        if self.path == "/api/setup":
            # first start: create the initial administrator
            if auth.user_count():
                return self.reply(409, {"error": "Einrichtung bereits erfolgt"})
            try:
                auth.create_user(data.get("username"), data.get("password"), "admin")
            except ValueError as e:
                return self.reply(400, {"error": str(e)})
            tok = auth.login(self.client_address[0], data.get("username"),
                             data.get("password"))
            return self.reply(200, {"ok": True}, cookie=self.cookie(tok))
        if self.path == "/api/login":
            try:
                tok = auth.login(self.client_address[0], data.get("username"),
                                 data.get("password"))
            except PermissionError as e:
                return self.reply(429, {"error": str(e)})
            if not tok:
                return self.reply(401, {"error": "Benutzername oder Passwort falsch"})
            return self.reply(200, {"ok": True}, cookie=self.cookie(tok))
        if not self.allowed("viewer"):
            return
        me = self.user()
        if self.path == "/api/logout":
            auth.logout(self.token())
            return self.reply(200, {"ok": True}, cookie=self.cookie("", 0))
        if self.path == "/api/password":
            if not auth.verify(me["id"], data.get("old")):
                return self.reply(403, {"error": "Altes Passwort falsch"})
            try:
                auth.set_password(me["id"], data.get("new"), self.token())
            except ValueError as e:
                return self.reply(400, {"error": str(e)})
            return self.reply(200, {"ok": True})
        if self.path == "/api/users":
            if not self.allowed("admin"):
                return
            act = data.get("action")
            try:
                if act == "create":
                    auth.create_user(data.get("username"), data.get("password"),
                                     data.get("role"))
                elif act == "delete":
                    if int(data.get("id", 0)) == me["id"]:
                        raise ValueError("du kannst dich nicht selbst löschen")
                    auth.delete_user(int(data.get("id", 0)))
                elif act == "role":
                    auth.set_role(int(data.get("id", 0)), data.get("role"))
                elif act == "password":
                    auth.set_password(int(data.get("id", 0)), data.get("password"))
                else:
                    raise ValueError("unbekannte Aktion")
            except (ValueError, TypeError) as e:
                return self.reply(400, {"error": str(e)})
            return self.reply(200, {"ok": True})
        if self.path == "/api/alerts":
            if not self.allowed("admin"):
                return
            old = max_alerts.load(db)
            if data.get("action") == "settings":
                e = data.get("email") or {}
                try:
                    port = int(e.get("port") or 587)
                    rep_h = int(data.get("repeat_hours") or 24)
                    if not (1 <= port <= 65535 and 1 <= rep_h <= 720):
                        raise ValueError
                except (TypeError, ValueError):
                    return self.reply(400, {"error": "Port 1-65535, Erinnerung 1-720 Stunden"})
                sec = e.get("security") if e.get("security") in ("none", "starttls", "ssl") else "starttls"
                to = ",".join(x.strip() for x in str(e.get("to") or "").split(",") if x.strip())[:300]
                if bool(e.get("enabled")) and (not str(e.get("host") or "").strip() or "@" not in to):
                    return self.reply(400, {"error": "E-Mail aktiv: SMTP-Server und gültigen Empfänger angeben"})
                cfg = {"repeat_hours": rep_h, "email": {
                    "enabled": bool(e.get("enabled")), "host": str(e.get("host") or "").strip()[:200],
                    "port": port, "security": sec, "username": str(e.get("username") or "")[:200],
                    "password": (str(e["password"])[:200] if e.get("password") else old["email"]["password"]),
                    "sender": str(e.get("sender") or "").strip()[:200], "to": to}}
                if e.get("clear_password"):
                    cfg["email"]["password"] = ""
                put_setting(db, "alerts", json.dumps(cfg))
                self.server.alerter.wake.set()
                return self.reply(200, {"ok": True})
            if data.get("action") == "test":
                try:
                    max_alerts.send_mail(old, "MAX! Zentrale: Testnachricht",
                                         "Diese Testnachricht bestätigt, dass die E-Mail-Benachrichtigung funktioniert.")
                except Exception as ex:
                    return self.reply(502, {"ok": False, "error": str(ex) or repr(ex)})
                return self.reply(200, {"ok": True})
            return self.reply(400, {"error": "unbekannte Aktion"})
        if self.path == "/api/backup":
            if not self.allowed("admin"):
                return
            act = data.get("action")
            try:
                if act == "create":
                    name = max_backup.create(db, "manual")
                    return self.reply(200, {"ok": True, "name": name})
                if act == "delete":
                    max_backup.delete(db, str(data.get("name", "")))
                elif act == "restore":
                    max_backup.restore(db, str(data.get("name", "")))
                elif act == "settings":
                    keep = int(data.get("keep"))
                    if not 1 <= keep <= 365:
                        raise ValueError("Aufbewahrung: 1-365 Backups")
                    put_setting(db, "backup", json.dumps(
                        {"auto": bool(data.get("auto")), "keep": keep}))
                else:
                    raise ValueError("unbekannte Aktion")
            except (ValueError, TypeError, OSError, sqlite3.Error) as e:
                return self.reply(400, {"error": str(e)})
            return self.reply(200, {"ok": True})
        if self.path == "/api/mqtt":
            if not self.allowed("admin"):
                return
            old = mqtt_load(db)
            try:
                port = int(data.get("port") or 1883)
                if not 1 <= port <= 65535:
                    raise ValueError
            except (TypeError, ValueError):
                return self.reply(400, {"error": "Port: 1-65535"})
            base = str(data.get("base") or "max").strip("/ ")[:60]
            if not re.fullmatch(r"[A-Za-z0-9_\-/]+", base or ""):
                return self.reply(400, {"error": "Basis-Topic: nur Buchstaben, Ziffern, _ - /"})
            cfg = {"enabled": bool(data.get("enabled")),
                   "host": str(data.get("host") or "").strip()[:200],
                   "port": port, "username": str(data.get("username") or "")[:100],
                   "password": (str(data["password"])[:200]
                                if data.get("password") else old["password"]),
                   "tls": bool(data.get("tls")), "base": base,
                   "discovery": bool(data.get("discovery")),
                   "discovery_prefix": str(data.get("discovery_prefix")
                                           or "homeassistant").strip("/ ")[:60]}
            if data.get("clear_password"):
                cfg["password"] = ""
            put_setting(db, "mqtt", json.dumps(cfg))
            self.server.bridge.reload()
            return self.reply(200, {"ok": True})
        # everything below changes thermostats / rooms: needs operator rights
        if not self.allowed("operator"):
            return

        if self.path == "/api/pairing":
            if data.get("enable"):
                worker.pairing_until = time.time() + 120
                worker.pair_events.clear()
            else:
                worker.pairing_until = 0.0
            return self.reply(200, {"ok": True})

        # ---- rooms (no device address needed)
        if self.path == "/api/room":
            action = data.get("action")
            name = str(data.get("name", "")).strip()[:60]
            c = sqlite3.connect(db)
            try:
                if action == "create" and name:
                    c.execute("INSERT INTO rooms(name) VALUES(?)", (name,))
                elif action == "rename" and name:
                    c.execute("UPDATE rooms SET name=? WHERE id=?",
                              (name, int(data.get("id", 0))))
                elif action == "delete":
                    rid = int(data.get("id", 0))
                    c.execute("DELETE FROM device_room WHERE room_id=?", (rid,))
                    c.execute("DELETE FROM rooms WHERE id=?", (rid,))
                else:
                    return self.reply(400, {"error": "bad request"})
                c.commit()
            except sqlite3.IntegrityError:
                return self.reply(409, {"error": "a room with this name exists"})
            except (TypeError, ValueError):
                return self.reply(400, {"error": "bad room id"})
            finally:
                c.close()
            return self.reply(200, {"ok": True})

        if self.path == "/api/room_set":
            # set all radiator thermostats of a room: {room_id, temp | auto}
            try:
                rid = int(data.get("room_id"))
            except (TypeError, ValueError):
                return self.reply(400, {"error": "bad room id"})
            auto = bool(data.get("auto"))
            temp = None
            if not auto:
                try:
                    temp = float(data["temp"])
                except (KeyError, TypeError, ValueError):
                    return self.reply(400, {"error": "temp missing"})
                if not 4.5 <= temp <= 30.5:
                    return self.reply(400, {"error": "temp out of range 4.5-30.5"})
            members = q(db, "SELECT d.addr FROM devices d JOIN device_room r "
                            "ON r.addr=d.addr WHERE r.room_id=? AND d.kind IN "
                            "('HeatingThermostat','HeatingThermostatPlus',"
                            "'WallThermostat')", (rid,))
            results = {m["addr"]: run_set(worker, m["addr"], temp, auto)
                       for m in members}
            ok = bool(results) and all(r["ok"] for r in results.values())
            return self.reply(200 if ok else 502, {
                "ok": ok, "results": results,
                "error": None if ok else ("no thermostats in this room"
                                          if not results else "some failed")})

        addr = str(data.get("addr", "")).upper()
        if not re.fullmatch(r"[0-9A-F]{6}", addr):
            return self.reply(400, {"error": "bad address"})

        if self.path == "/api/name":
            name = str(data.get("name", "")).strip()[:60]
            c = sqlite3.connect(db)
            if name:
                c.execute("INSERT INTO names(addr,name) VALUES(?,?) ON CONFLICT"
                          "(addr) DO UPDATE SET name=excluded.name", (addr, name))
            else:
                c.execute("DELETE FROM names WHERE addr=?", (addr,))
            c.commit(); c.close()
            return self.reply(200, {"ok": True})

        if self.path == "/api/device_room":
            known = q(db, "SELECT 1 FROM devices WHERE addr=?", (addr,))
            if not known:
                return self.reply(404, {"error": "unknown device"})
            rid = data.get("room_id")
            c = sqlite3.connect(db)
            if rid in (None, "", 0):
                c.execute("DELETE FROM device_room WHERE addr=?", (addr,))
            else:
                try:
                    rid = int(rid)
                except (TypeError, ValueError):
                    c.close()
                    return self.reply(400, {"error": "bad room id"})
                if not c.execute("SELECT 1 FROM rooms WHERE id=?", (rid,)).fetchone():
                    c.close()
                    return self.reply(404, {"error": "unknown room"})
                c.execute("INSERT INTO device_room(addr,room_id) VALUES(?,?) ON "
                          "CONFLICT(addr) DO UPDATE SET room_id=excluded.room_id",
                          (addr, rid))
            c.commit(); c.close()
            return self.reply(200, {"ok": True})

        if self.path in ("/api/config", "/api/preset"):
            known = q(db, "SELECT kind FROM devices WHERE addr=?", (addr,))
            if not known or known[0]["kind"] not in (
                    "HeatingThermostat", "HeatingThermostatPlus", "WallThermostat"):
                return self.reply(400, {"error": "not a known thermostat"})
            job = Job(addr, 0.0, "raw")
            job.kind = "raw"
            cfg = None
            if self.path == "/api/preset":
                ty = {"comfort": 0x43, "eco": 0x44}.get(data.get("preset"))
                if ty is None:
                    return self.reply(400, {"error": "preset must be comfort or eco"})
                job.raw = build_raw(worker.src, addr, ty, b"")
            else:
                try:
                    cfg = validate_config(data.get("cfg") or {})
                except (KeyError, TypeError, ValueError) as e:
                    return self.reply(400, {"error": f"invalid config: {e}"})
                job.raw = build_raw(worker.src, addr, 0x11, config_payload(cfg))
            worker.jobs.put(job)
            if not job.done.wait(30):
                job.cancelled = True
                return self.reply(504, {"ok": False, "error": "timeout (no send credit or no answer)"})
            if cfg is not None and job.result["ok"]:
                c = sqlite3.connect(db)
                c.execute("INSERT INTO device_config(addr,cfg,sent_at) VALUES(?,?,?) "
                          "ON CONFLICT(addr) DO UPDATE SET cfg=excluded.cfg, "
                          "sent_at=excluded.sent_at",
                          (addr, json.dumps(cfg),
                           datetime.now().isoformat(timespec="seconds")))
                c.commit(); c.close()
            return self.reply(200 if job.result["ok"] else 502, job.result)

        if self.path == "/api/display":
            # wall thermostat: show the actual temperature (True) or setpoint
            known = q(db, "SELECT kind FROM devices WHERE addr=?", (addr,))
            if not known or known[0]["kind"] != "WallThermostat":
                return self.reply(400, {"error": "not a known wall thermostat"})
            job = Job(addr, 0.0, "raw")
            job.kind = "raw"
            job.raw = build_raw(worker.src, addr, 0x82,
                                bytes([4 if data.get("actual") else 0]))
            worker.jobs.put(job)
            if not job.done.wait(30):
                job.cancelled = True
                return self.reply(504, {"ok": False, "error": "timeout (no send credit or no answer)"})
            return self.reply(200 if job.result["ok"] else 502, job.result)

        if self.path == "/api/time":
            known = q(db, "SELECT kind FROM devices WHERE addr=?", (addr,))
            if not known or known[0]["kind"] not in (
                    "HeatingThermostat", "HeatingThermostatPlus", "WallThermostat"):
                return self.reply(400, {"error": "not a known thermostat"})
            job = Job(addr, 0.0, "time")
            job.kind = "time"
            worker.jobs.put(job)
            if not job.done.wait(30):
                job.cancelled = True
                return self.reply(504, {"ok": False, "error": "timeout (no send credit or no answer)"})
            return self.reply(200 if job.result["ok"] else 502, job.result)

        if self.path == "/api/profile":
            # {addr, profile: {"Mon": [{temp, until}, ...], ...}, send: bool}
            profile = data.get("profile")
            if not isinstance(profile, dict) or not profile:
                return self.reply(400, {"error": "profile missing"})
            known = q(db, "SELECT kind FROM devices WHERE addr=?", (addr,))
            if not known or known[0]["kind"] not in (
                    "HeatingThermostat", "HeatingThermostatPlus", "WallThermostat"):
                return self.reply(400, {"error": "not a known thermostat"})
            try:
                plan = []
                for day, pts in profile.items():
                    if day not in DAY_ID:
                        raise ValueError(f"unknown day {day!r}")
                    plan.append((day, pts, week_profile_packets(
                        worker.src, addr, day, pts)))
            except (ValueError, KeyError, TypeError) as e:
                return self.reply(400, {"error": f"invalid profile: {e}"})
            c = sqlite3.connect(db)
            for day, pts, _ in plan:
                c.execute("INSERT INTO profiles(addr,day,points) VALUES(?,?,?) "
                          "ON CONFLICT(addr,day) DO UPDATE SET points=excluded.points",
                          (addr, day, json.dumps(pts)))
            c.commit()
            if not data.get("send"):
                c.close()
                return self.reply(200, {"ok": True, "sent": False})
            done = []
            for day, pts, packets in plan:
                for packet, cnt in packets:
                    job = Job(addr, 0.0, "raw")
                    job.kind, job.raw, job.max_wait = "raw", (packet, cnt), 120
                    worker.jobs.put(job)
                    if not job.done.wait(150) or not job.result["ok"]:
                        job.cancelled = not job.done.is_set()
                        c.close()
                        err = job.result.get("error") or "timeout"
                        return self.reply(502, {"ok": False, "sent_days": done,
                                                "error": f"{day}: {err}"})
                c.execute("UPDATE profiles SET sent_at=? WHERE addr=? AND day=?",
                          (datetime.now().isoformat(timespec="seconds"), addr, day))
                c.commit()
                done.append(day)
            c.close()
            return self.reply(200, {"ok": True, "sent": True, "sent_days": done})

        if self.path == "/api/set":
            known = q(db, "SELECT kind FROM devices WHERE addr=?", (addr,))
            if not known or known[0]["kind"] not in (
                    "HeatingThermostat", "HeatingThermostatPlus", "WallThermostat"):
                return self.reply(400, {"error": "not a known thermostat"})
            if data.get("auto"):
                job = Job(addr, 0.0, "auto")
            else:
                try:
                    temp = float(data["temp"])
                except (KeyError, TypeError, ValueError):
                    return self.reply(400, {"error": "temp missing"})
                if not 4.5 <= temp <= 30.5:
                    return self.reply(400, {"error": "temp out of range 4.5-30.5"})
                job = Job(addr, temp, "manual")
            worker.jobs.put(job)
            if not job.done.wait(30):
                job.cancelled = True
                return self.reply(504, {"ok": False, "error": "timeout (no send credit or no answer)"})
            return self.reply(200 if job.result["ok"] else 502, job.result)
        self.reply(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", default=os.environ.get("MAX_PORT", "COM3" if os.name == "nt" else "/dev/ttyACM0"),
                    help="serial port (COM3, /dev/ttyACM0) or culfw network "
                         "address (tcp://192.168.1.50:2323)")
    ap.add_argument("--db", default=os.environ.get("MAX_DB", "max.db"))
    ap.add_argument("--src", default="10B240",
                    help="RF sender address (the original Cube)")
    ap.add_argument("--host", default=os.environ.get("MAX_HOST", "127.0.0.1"),
                    help="bind address; 0.0.0.0 exposes the UI to your LAN "
                         "(login required)")
    ap.add_argument("--http", type=int, default=8080)
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.executescript(SCHEMA + EXTRA_SCHEMA)
    db.close()
    worker = SerialWorker(args.port, args.db, args.src.upper())
    auth = Auth(args.db)
    bridge = Bridge(args.db, worker, build_state, run_set)
    autobackup = max_backup.AutoBackup(args.db)
    alerter = max_alerts.Alerter(args.db)
    worker.start()
    bridge.start()
    autobackup.start()
    alerter.start()
    srv = ThreadingHTTPServer((args.host, args.http), Handler)
    srv.db_path, srv.worker, srv.auth, srv.bridge = args.db, worker, auth, bridge
    srv.autobackup = autobackup
    srv.alerter = alerter
    print(f"MAX! UI on http://{args.host}:{args.http}/  (serial {args.port})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
