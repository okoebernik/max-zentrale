#!/usr/bin/env python3
"""Create a demo database with the fictitious installation from demo_data.py
(names, rooms, 3 days of history, a weekly profile).

    python tools/make_demo_db.py demo.db
"""
import json
import math
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
from demo_data import DEVICES, ROOMS                   # noqa: E402
from max_auth import AUTH_SCHEMA                       # noqa: E402
from max_logger import SCHEMA                          # noqa: E402
from max_server import EXTRA_SCHEMA                    # noqa: E402

KIND = {"heat": "HeatingThermostat", "wall": "WallThermostat", "window": "ShutterContact"}


def main(path):
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA + EXTRA_SCHEMA + AUTH_SCHEMA)
    random.seed(7)
    now = datetime.now().replace(second=0, microsecond=0)
    for r in ROOMS:
        db.execute("INSERT INTO rooms(name) VALUES(?)", (r,))
    rid = {n: i for i, n in db.execute("SELECT id, name FROM rooms")}
    for d in DEVICES:
        first = (now - timedelta(days=30)).isoformat(timespec="seconds")
        db.execute("INSERT INTO devices(addr,kind,first_seen,last_seen,telegrams,battery_low)"
                   " VALUES(?,?,?,?,?,?)", (d["addr"], KIND[d["kind"]], first,
                                            now.isoformat(timespec="seconds"), 4000,
                                            1 if d.get("battery_low") else 0))
        db.execute("INSERT INTO names VALUES(?,?)", (d["addr"], d["name"]))
        db.execute("INSERT INTO device_room VALUES(?,?)", (d["addr"], rid[d["room"]]))
        t = now - timedelta(days=3)
        while t <= now:                     # three days of readings, every 10 minutes
            h = t.hour + t.minute / 60
            ts = t.isoformat(timespec="seconds")
            if d["kind"] == "window":
                op = 1 if (t.hour in (7, 19) and t.minute < 20) else 0
                if d.get("open") and t > now - timedelta(minutes=25):
                    op = 1
                db.execute("INSERT INTO readings(ts,addr,kind,is_open,battery_low) VALUES(?,?,?,?,?)",
                           (ts, d["addr"], KIND[d["kind"]], op, 1 if d.get("battery_low") else 0))
            else:
                comfort = 6 <= h < 22
                sp = d["setpoint"] if comfort else d["setpoint"] - 3.5
                if d["room"] == "Schlafzimmer":
                    sp = 17.0
                meas = sp - 0.7 + 1.1 * math.sin((h - 8) / 24 * 2 * math.pi) + random.uniform(-.15, .15)
                valve = (max(0, min(100, int((sp - meas) * 55 + random.uniform(-4, 4))))
                         if d["kind"] == "heat" else None)
                db.execute("INSERT INTO readings(ts,addr,kind,measured,setpoint,valve,mode,battery_low)"
                           " VALUES(?,?,?,?,?,?,?,0)",
                           (ts, d["addr"], KIND[d["kind"]], round(meas, 1), sp, valve, "auto"))
            t += timedelta(minutes=10)
    wk = [{"temp": 17, "until": "06:00"}, {"temp": 21, "until": "08:30"},
          {"temp": 18, "until": "16:00"}, {"temp": 21, "until": "22:30"},
          {"temp": 17, "until": "24:00"}]
    we = [{"temp": 17, "until": "07:30"}, {"temp": 21, "until": "23:00"},
          {"temp": 17, "until": "24:00"}]
    stamp = now.isoformat(timespec="seconds")
    for day in ("Mon", "Tue", "Wed", "Thu", "Fri"):
        db.execute("INSERT INTO profiles(addr,day,points,sent_at) VALUES(?,?,?,?)",
                   ("1A0001", day, json.dumps(wk), stamp))
    for day in ("Sat", "Sun"):
        db.execute("INSERT INTO profiles(addr,day,points,sent_at) VALUES(?,?,?,?)",
                   ("1A0001", day, json.dumps(we), stamp))
    db.execute("INSERT INTO device_config VALUES(?,?,?)", ("1A0001", json.dumps(
        {"comfort": 21.0, "eco": 17.0, "max": 30.5, "min": 4.5, "offset": 0.0,
         "window_temp": 12.0, "window_min": 15}), stamp))
    db.commit()
    db.close()
    print("demo database written to", path)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo.db")
