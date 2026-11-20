"""Minimal MQTT 3.1.1 client + bridge (stdlib only, QoS 0).

Publishes the state of every device to  <base>/<addr>/state  (retained JSON),
accepts commands on  <base>/<addr>/set/temperature  (e.g. 21.5)  and
<base>/<addr>/set/mode  (auto | heat)  and optionally announces all devices to
Home Assistant via MQTT discovery.
"""
import json
import socket
import ssl
import struct
import threading
import time

from max_auth import get_setting

DEFAULTS = {"enabled": False, "host": "", "port": 1883, "username": "",
            "password": "", "tls": False, "base": "max", "discovery": True,
            "discovery_prefix": "homeassistant"}
THERMOSTATS = ("HeatingThermostat", "HeatingThermostatPlus", "WallThermostat")


def load_config(db_path):
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(get_setting(db_path, "mqtt", "{}")))
    except ValueError:
        pass
    return cfg


def _utf(s):
    b = s.encode()
    return struct.pack(">H", len(b)) + b


def _remaining(n):
    out = bytearray()
    while True:
        b, n = n % 128, n // 128
        out.append(b | (128 if n else 0))
        if not n:
            return bytes(out)


class MqttError(Exception):
    pass


class MqttClient:
    """Just enough MQTT: connect, publish, subscribe, ping, receive."""

    def __init__(self, cfg, client_id, will_topic):
        self.cfg, self.client_id, self.will_topic = cfg, client_id, will_topic
        self.sock = None
        self.buf = b""
        self.last_tx = self.last_rx = 0.0
        self.pid = 0

    def connect(self):
        s = socket.create_connection((self.cfg["host"], int(self.cfg["port"])), 8)
        if self.cfg.get("tls"):
            s = ssl.create_default_context().wrap_socket(
                s, server_hostname=self.cfg["host"])
        s.settimeout(1.0)
        self.sock = s
        flags = 0x02 | 0x04 | 0x20          # clean session, will, will retain
        payload = _utf(self.client_id) + _utf(self.will_topic) + _utf("offline")
        if self.cfg.get("username"):
            flags |= 0x80
            payload += _utf(self.cfg["username"])
            if self.cfg.get("password"):
                flags |= 0x40
                payload += _utf(self.cfg["password"])
        vh = _utf("MQTT") + bytes([4, flags]) + struct.pack(">H", 60)
        self._send(0x10, vh + payload)
        deadline = time.time() + 8
        while time.time() < deadline:
            pkt = self._read_packet()
            if pkt and pkt[0] >> 4 == 2:
                rc = pkt[1][1] if len(pkt[1]) >= 2 else 255
                if rc:
                    raise MqttError("Broker lehnt ab (Code %d%s)" % (
                        rc, ": Benutzer/Passwort falsch" if rc in (4, 5) else ""))
                self.last_rx = time.time()
                return
        raise MqttError("keine Antwort vom Broker")

    def _send(self, first, body):
        self.sock.sendall(bytes([first]) + _remaining(len(body)) + body)
        self.last_tx = time.time()

    def publish(self, topic, payload, retain=False):
        if isinstance(payload, str):
            payload = payload.encode()
        self._send(0x30 | (1 if retain else 0), _utf(topic) + payload)

    def subscribe(self, topic):
        self.pid = self.pid % 65535 + 1
        self._send(0x82, struct.pack(">H", self.pid) + _utf(topic) + b"\x00")

    def _read_packet(self):
        """One complete packet (first byte, body) or None on read timeout."""
        while True:
            pkt = self._parse()
            if pkt:
                return pkt
            try:
                d = self.sock.recv(4096)
            except (socket.timeout, ssl.SSLWantReadError):
                return None
            if not d:
                raise MqttError("Verbindung vom Broker geschlossen")
            self.buf += d

    def _parse(self):
        b = self.buf
        if len(b) < 2:
            return None
        n, mult, i = 0, 1, 1
        while True:
            if i >= len(b):
                return None
            n += (b[i] & 127) * mult
            mult *= 128
            i += 1
            if not b[i - 1] & 128:
                break
            if i > 4:
                raise MqttError("ungültiges Paket")
        if len(b) < i + n:
            return None
        self.buf = b[i + n:]
        return b[0], b[i:i + n]

    def poll(self):
        """Return (topic, payload) for an incoming PUBLISH, else None.  Also
        keeps the connection alive."""
        if time.time() - self.last_tx > 30:
            self._send(0xC0, b"")
        pkt = self._read_packet()
        if pkt is None:
            if time.time() - self.last_rx > 120:
                raise MqttError("Broker antwortet nicht mehr")
            return None
        self.last_rx = time.time()
        if pkt[0] >> 4 == 3:
            body = pkt[1]
            tl = struct.unpack(">H", body[:2])[0]
            off = 2 + tl + (2 if (pkt[0] >> 1) & 3 else 0)
            return body[2:2 + tl].decode(errors="replace"), body[off:]
        return None

    def close(self):
        try:
            self._send(0xE0, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


def ha_mode(mode):
    return "auto" if mode == "auto" else "heat"


def device_payload(d, rooms):
    return {
        "name": d.get("name"), "kind": d.get("kind"),
        "room": rooms.get(d.get("room_id")),
        "temperature": d.get("measured"), "setpoint": d.get("setpoint"),
        "valve": d.get("valve"), "mode": d.get("mode"),
        "ha_mode": ha_mode(d.get("mode")),
        "open": None if d.get("is_open") is None else bool(d["is_open"]),
        "battery_low": None if d.get("battery_low") is None
        else bool(d["battery_low"]),
        "rssi": d.get("rssi"), "last_seen": d.get("last_seen"),
    }


class Bridge(threading.Thread):
    """Runs forever; (re)reads its settings from the DB when `reload()` is
    called and reconnects with backoff."""

    def __init__(self, db_path, worker, build_state, run_set):
        super().__init__(daemon=True)
        self.db_path, self.worker = db_path, worker
        self.build_state, self.run_set = build_state, run_set
        self.dirty = set()
        self.lock = threading.Lock()
        self.reload_flag = threading.Event()
        self.status = {"enabled": False, "connected": False, "error": "",
                       "published": 0, "since": None}
        worker.listeners.append(self.on_telegram)

    def reload(self):
        self.reload_flag.set()

    def on_telegram(self, addr):
        with self.lock:
            self.dirty.add(addr)

    # -------------------------------------------------------------- loop
    def run(self):
        backoff = 3
        while True:
            self.reload_flag.clear()
            cfg = load_config(self.db_path)
            self.status.update(enabled=bool(cfg["enabled"]), connected=False)
            if not cfg["enabled"] or not cfg["host"]:
                self.status["error"] = ""
                self.reload_flag.wait(5)
                continue
            try:
                self.session(cfg)
                backoff = 3
            except (MqttError, OSError) as e:
                self.status.update(connected=False, error=str(e) or repr(e))
                self.reload_flag.wait(backoff)
                backoff = min(backoff * 2, 60)

    def session(self, cfg):
        base = cfg["base"].strip("/") or "max"
        cli = MqttClient(cfg, "max-zentrale", base + "/status")
        cli.connect()
        try:
            cli.publish(base + "/status", "online", retain=True)
            cli.subscribe(base + "/+/set/#")
            self.status.update(connected=True, error="", since=time.strftime(
                "%Y-%m-%dT%H:%M:%S"))
            last, announced = {}, set()
            full = time.time()
            self.on_telegram(None)
            while not self.reload_flag.is_set():
                msg = cli.poll()
                if msg:
                    self.handle_command(base, *msg)
                with self.lock:
                    dirty, self.dirty = self.dirty, set()
                if time.time() - full > 300:          # periodic refresh
                    full, dirty = time.time(), dirty | {None}
                if dirty:
                    st = self.build_state(self.db_path, self.worker)
                    rooms = {r["id"]: r["name"] for r in st["rooms"]}
                    for d in st["devices"]:
                        if None not in dirty and d["addr"] not in dirty:
                            continue
                        if cfg["discovery"] and d["addr"] not in announced:
                            self.announce(cli, cfg, base, d)
                            announced.add(d["addr"])
                        body = json.dumps(device_payload(d, rooms),
                                          sort_keys=True)
                        if None in dirty or last.get(d["addr"]) != body:
                            cli.publish("%s/%s/state" % (base, d["addr"]), body,
                                        retain=True)
                            last[d["addr"]] = body
                            self.status["published"] += 1
        finally:
            cli.close()

    # ---------------------------------------------------------- commands
    def handle_command(self, base, topic, payload):
        parts = topic[len(base) + 1:].split("/")
        if len(parts) != 3 or parts[1] != "set":
            return
        addr, what, val = parts[0].upper(), parts[2], payload.decode(
            errors="replace").strip()
        threading.Thread(target=self._do, args=(addr, what, val),
                         daemon=True).start()

    def _do(self, addr, what, val):
        st = self.build_state(self.db_path, self.worker)
        d = next((x for x in st["devices"] if x["addr"] == addr), None)
        if not d or d["kind"] not in THERMOSTATS:
            return
        try:
            if what == "temperature":
                t = float(val)
                if 4.5 <= t <= 30.5:
                    self.run_set(self.worker, addr, round(t * 2) / 2, False)
            elif what == "mode":
                if val.lower() == "auto":
                    self.run_set(self.worker, addr, None, True)
                elif val.lower() in ("heat", "manual") and d.get("setpoint"):
                    self.run_set(self.worker, addr, d["setpoint"], False)
        except ValueError:
            pass

    # --------------------------------------------------------- discovery
    def announce(self, cli, cfg, base, d):
        pre, addr = cfg["discovery_prefix"].strip("/") or "homeassistant", d["addr"]
        name = d.get("name") or "MAX %s" % addr
        dev = {"identifiers": ["max_" + addr], "name": name,
               "manufacturer": "eQ-3 / ELV", "model": d.get("kind") or "MAX!"}
        st = "%s/%s/state" % (base, addr)
        avail = {"availability_topic": base + "/status"}

        def send(comp, oid, conf):
            conf.update(avail, unique_id="max_%s_%s" % (addr, oid), device=dev)
            cli.publish("%s/%s/max_%s_%s/config" % (pre, comp, addr, oid),
                        json.dumps(conf), retain=True)
        kind = d.get("kind")
        if kind in THERMOSTATS:
            send("climate", "climate", {
                "name": None, "current_temperature_topic": st,
                "current_temperature_template": "{{ value_json.temperature }}",
                "temperature_state_topic": st,
                "temperature_state_template": "{{ value_json.setpoint }}",
                "temperature_command_topic": "%s/%s/set/temperature" % (base, addr),
                "mode_state_topic": st,
                "mode_state_template": "{{ value_json.ha_mode }}",
                "mode_command_topic": "%s/%s/set/mode" % (base, addr),
                "modes": ["auto", "heat"], "min_temp": 4.5, "max_temp": 30.5,
                "temp_step": 0.5, "precision": 0.1})
            if kind != "WallThermostat":
                send("sensor", "valve", {
                    "name": "Ventil", "state_topic": st, "unit_of_measurement": "%",
                    "value_template": "{{ value_json.valve }}",
                    "state_class": "measurement"})
        elif kind == "ShutterContact":
            send("binary_sensor", "window", {
                "name": "Fenster", "device_class": "window", "state_topic": st,
                "value_template": "{{ 'ON' if value_json.open else 'OFF' }}"})
        if kind:
            send("binary_sensor", "battery", {
                "name": "Batterie", "device_class": "battery", "state_topic": st,
                "entity_category": "diagnostic",
                "value_template": "{{ 'ON' if value_json.battery_low else 'OFF' }}"})
            send("sensor", "rssi", {
                "name": "Empfang", "state_topic": st, "device_class": "signal_strength",
                "unit_of_measurement": "dBm", "entity_category": "diagnostic",
                "value_template": "{{ value_json.rssi }}"})
