#!/usr/bin/env python3
"""MAX! RF logger for a CUL/culfw device (e.g. MAX! Cube flashed with culfw).

Listens for MAX! (Moritz) telegrams, decodes them, prints them and stores
them in SQLite.  Receive-only: it never transmits.

Usage:  python max_logger.py [--port COM3] [--db max.db] [--duration SECONDS]
"""
import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime

import serial

MSG_TYPES = {
    0x00: "PairPing", 0x01: "PairPong", 0x02: "Ack", 0x03: "TimeInformation",
    0x10: "ConfigWeekProfile", 0x11: "ConfigTemperatures", 0x12: "ConfigValve",
    0x20: "AddLinkPartner", 0x21: "RemoveLinkPartner", 0x22: "SetGroupId",
    0x23: "RemoveGroupId", 0x30: "ShutterContactState", 0x40: "SetTemperature",
    0x42: "WallThermostatControl", 0x43: "SetComfortTemperature",
    0x44: "SetEcoTemperature", 0x50: "PushButtonState", 0x60: "ThermostatState",
    0x70: "WallThermostatState",
    0x82: "SetDisplayActualTemperature", 0xF0: "WakeUp", 0xF1: "Reset",
}
DEVICE_TYPES = {0: "Cube", 1: "HeatingThermostat", 2: "HeatingThermostatPlus",
                3: "WallThermostat", 4: "ShutterContact", 5: "PushButton"}
# kind of device inferred from the telegram type it sends
KIND_BY_TYPE = {0x60: "HeatingThermostat", 0x42: "WallThermostat",
                0x70: "WallThermostat",
                0x30: "ShutterContact", 0x50: "PushButton"}
MODES = {0: "auto", 1: "manual", 2: "vacation", 3: "boost"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS telegrams (
    id INTEGER PRIMARY KEY, ts TEXT, raw TEXT, msgcnt INTEGER, flags INTEGER,
    type INTEGER, type_name TEXT, src TEXT, dst TEXT, grp INTEGER,
    payload TEXT, rssi_dbm REAL);
CREATE TABLE IF NOT EXISTS devices (
    addr TEXT PRIMARY KEY, kind TEXT, serial TEXT, first_seen TEXT,
    last_seen TEXT, telegrams INTEGER DEFAULT 0, battery_low INTEGER);
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY, ts TEXT, addr TEXT, kind TEXT,
    measured REAL, setpoint REAL, valve INTEGER, mode TEXT,
    is_open INTEGER, battery_low INTEGER, rf_error INTEGER);
CREATE INDEX IF NOT EXISTS readings_addr_ts ON readings(addr, ts);
"""


def rssi_dbm(b):
    v = b - 256 if b >= 128 else b
    return v / 2.0 - 74.0


def parse_telegram(line):
    """Parse 'Z0C81044213B9C41356DE002AD916' -> dict, or None."""
    line = line.strip()
    if not line.startswith("Z") or len(line) < 21:
        return None
    try:
        raw = bytes.fromhex(line[1:])
    except ValueError:
        return None
    length = raw[0]
    if len(raw) < length + 1 or length < 10:
        return None
    body = raw[1:length + 1]
    rssi = rssi_dbm(raw[length + 1]) if len(raw) > length + 1 else None
    return {
        "raw": line, "msgcnt": body[0], "flags": body[1], "type": body[2],
        "src": body[3:6].hex().upper(), "dst": body[6:9].hex().upper(),
        "grp": body[9], "payload": body[10:], "rssi": rssi,
    }


def _setpoint(b):
    return (b & 0x7F) / 2.0


def decode(t):
    """Return (description, reading-dict-or-None, extra-device-info)."""
    p, ty = t["payload"], t["type"]
    r = None
    if ty == 0x42 and len(p) >= 2:                     # wall thermostat
        measured = (((p[0] & 0x80) << 1) + p[1]) / 10.0
        r = {"kind": "WallThermostat", "setpoint": _setpoint(p[0]),
             "measured": measured}
    elif ty == 0x70 and len(p) >= 5:                   # wall thermostat state
        r = {"kind": "WallThermostat", "mode": MODES[p[0] & 3],
             "battery_low": (p[0] >> 7) & 1, "rf_error": (p[0] >> 6) & 1,
             "setpoint": _setpoint(p[2]),
             "measured": (((p[3] & 1) << 8) + p[4]) / 10.0}
    elif ty == 0x60 and len(p) >= 3:                   # radiator thermostat
        b = p[0]
        r = {"kind": "HeatingThermostat", "mode": MODES[b & 3],
             "battery_low": (b >> 7) & 1, "rf_error": (b >> 6) & 1,
             "valve": p[1], "setpoint": _setpoint(p[2])}
        if (b & 3) != 2 and len(p) >= 5 and (p[3] or p[4]):
            r["measured"] = (((p[3] & 1) << 8) + p[4]) / 10.0
    elif ty == 0x30 and len(p) >= 1:                   # window contact
        b = p[0]
        r = {"kind": "ShutterContact", "is_open": (b >> 1) & 1,
             "rf_error": (b >> 6) & 1, "battery_low": (b >> 7) & 1}
    elif ty == 0x02 and len(p) >= 4 and t["src"] != "000000":
        # Ack from a thermostat: ok-byte, flags, valve, setpoint
        r = {"kind": "HeatingThermostat", "mode": MODES[p[1] & 3],
             "battery_low": (p[1] >> 7) & 1, "rf_error": (p[1] >> 6) & 1,
             "valve": p[2], "setpoint": _setpoint(p[3])}
        if p[0] != 1:
            r = None  # negative ack, ignore as reading
    elif ty == 0x40 and len(p) >= 1:                   # SetTemperature
        return (f"set {_setpoint(p[0] & 0x3F):.1f} C mode={MODES[p[0] >> 6]}",
                None, {})
    elif ty == 0x00 and len(p) >= 13:                  # PairPing
        # firmware(1) devicetype(1) selftest(1) serial(10 ASCII)
        serial_no = p[3:13].decode("ascii", "replace")
        dev = DEVICE_TYPES.get(p[1], f"type{p[1]}")
        return f"pair ping {dev} serial={serial_no}", None, {
            "kind": dev, "serial": serial_no}

    if r is None:
        return f"payload={p.hex().upper()}", None, {}
    bits = []
    if "measured" in r: bits.append(f"ist={r['measured']:.1f}C")
    if "setpoint" in r: bits.append(f"soll={r['setpoint']:.1f}C")
    if "valve" in r: bits.append(f"ventil={r['valve']}%")
    if "mode" in r: bits.append(r["mode"])
    if "is_open" in r: bits.append("OFFEN" if r["is_open"] else "zu")
    if r.get("battery_low"): bits.append("BATTERIE LEER")
    if r.get("rf_error"): bits.append("rf-error")
    return " ".join(bits), r, {}


def store(db, ts, t, desc, reading, extra):
    name = MSG_TYPES.get(t["type"], f"0x{t['type']:02X}")
    db.execute(
        "INSERT INTO telegrams (ts,raw,msgcnt,flags,type,type_name,src,dst,grp,"
        "payload,rssi_dbm) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ts, t["raw"], t["msgcnt"], t["flags"], t["type"], name, t["src"],
         t["dst"], t["grp"], t["payload"].hex().upper(), t["rssi"]))
    if t["src"] != "000000":
        # Only a device's *own* telegram types identify it.  Acks (and
        # SetTemperature) are sent by wall thermostats and radiators alike, so
        # they must never decide the device kind.
        kind = extra.get("kind") or KIND_BY_TYPE.get(t["type"])
        db.execute(
            "INSERT INTO devices (addr,kind,serial,first_seen,last_seen,telegrams)"
            " VALUES (?,?,?,?,?,1) ON CONFLICT(addr) DO UPDATE SET "
            "last_seen=excluded.last_seen, telegrams=telegrams+1,"
            "kind=COALESCE(excluded.kind,kind),"
            "serial=COALESCE(excluded.serial,serial)",
            (t["src"], kind, extra.get("serial"), ts, ts))
        if reading and t["type"] == 0x02:
            # An Ack carries valve/setpoint only for radiator thermostats.
            row = db.execute("SELECT kind FROM devices WHERE addr=?",
                             (t["src"],)).fetchone()
            if not row or row[0] not in ("HeatingThermostat",
                                         "HeatingThermostatPlus"):
                reading = None
        if reading:
            if reading.get("battery_low") is not None:
                db.execute("UPDATE devices SET battery_low=? WHERE addr=?",
                           (reading["battery_low"], t["src"]))
            db.execute(
                "INSERT INTO readings (ts,addr,kind,measured,setpoint,valve,mode,"
                "is_open,battery_low,rf_error) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts, t["src"], reading["kind"], reading.get("measured"),
                 reading.get("setpoint"), reading.get("valve"),
                 reading.get("mode"), reading.get("is_open"),
                 reading.get("battery_low"), reading.get("rf_error")))
    if t["type"] == 0x40 and t["src"] != "000000":
        # A device that sends SetTemperature (knob turned) but has not shown
        # its own state telegram yet is most likely a radiator thermostat.
        db.execute("UPDATE devices SET kind='HeatingThermostat' "
                   "WHERE addr=? AND kind IS NULL", (t["src"],))
    db.commit()
    return name


def open_port(port):
    ser = serial.Serial(port, 38400, timeout=1)
    ser.write(b"V\n")
    time.sleep(0.5)
    version = ser.read_all().decode(errors="replace").strip()
    print(f"Connected to {port}: {version}")
    ser.write(b"Zr\n")   # enable MAX!/Moritz receive mode
    return ser


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", default="COM3" if os.name == "nt" else "/dev/ttyACM0")
    ap.add_argument("--db", default="max.db")
    ap.add_argument("--duration", type=float, default=0,
                    help="stop after N seconds (default: run forever)")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.executescript(SCHEMA)
    end = time.time() + args.duration if args.duration else None
    ser = None
    buf = ""
    count = 0
    try:
        while end is None or time.time() < end:
            try:
                if ser is None:
                    ser = open_port(args.port)
                chunk = ser.read(256).decode(errors="replace")
            except (serial.SerialException, OSError) as e:
                print(f"Serial error: {e} - retrying in 5 s", file=sys.stderr)
                if ser:
                    try: ser.close()
                    except Exception: pass
                ser = None
                time.sleep(5)
                continue
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                t = parse_telegram(line)
                if not t:
                    continue
                ts = datetime.now().isoformat(timespec="seconds")
                desc, reading, extra = decode(t)
                name = store(db, ts, t, desc, reading, extra)
                count += 1
                rssi = f"{t['rssi']:.0f}dBm" if t["rssi"] is not None else "?"
                print(f"{ts[11:]} {t['src']} -> {t['dst']} {name:<22} {rssi:>7}  {desc}")
    except KeyboardInterrupt:
        pass
    finally:
        if ser:
            try:
                ser.write(b"Zx\n")
                ser.close()
            except Exception:
                pass
        print(f"\n{count} telegrams stored in {args.db}")
        for row in db.execute(
                "SELECT addr, COALESCE(kind,'?'), telegrams, last_seen,"
                " battery_low FROM devices ORDER BY addr"):
            print("  device %s  %-18s %4d telegrams, last %s, batt_low=%s" % row)
        db.close()


if __name__ == "__main__":
    main()
