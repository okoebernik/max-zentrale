"""Alarme: schwache Batterie.

Ein Alarm entsteht, sobald ein Gerät "Batterie schwach" meldet, und endet, wenn es
das nicht mehr tut (neue Batterie).  Offene Alarme erscheinen in der Oberfläche
(und über MQTT als Batterie-Sensor).  Optional wird eine E-Mail geschickt - beim
Entstehen und danach als Erinnerung im eingestellten Abstand.
"""
import json
import smtplib
import ssl
import sqlite3
import threading
import time
from datetime import datetime
from email.message import EmailMessage

from max_auth import get_setting

ALERT_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, addr TEXT NOT NULL, kind TEXT NOT NULL,
    since TEXT NOT NULL, last_notified TEXT, cleared TEXT);
CREATE INDEX IF NOT EXISTS alerts_open ON alerts(kind, addr, cleared);
"""
DEFAULTS = {"repeat_hours": 24,
            "email": {"enabled": False, "host": "", "port": 587,
                      "security": "starttls", "username": "", "password": "",
                      "sender": "", "to": ""}}


def load(db_path):
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        stored = json.loads(get_setting(db_path, "alerts", "{}"))
    except ValueError:
        stored = {}
    cfg["repeat_hours"] = stored.get("repeat_hours", cfg["repeat_hours"])
    cfg["email"].update(stored.get("email", {}))
    return cfg


def _q(db_path, sql, args=()):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in c.execute(sql, args)]
        c.commit()
        return rows
    finally:
        c.close()


def open_alerts(db_path):
    return _q(db_path, "SELECT a.id, a.addr, a.kind, a.since, a.last_notified, "
                       "n.name, r.name AS room FROM alerts a "
                       "LEFT JOIN names n ON n.addr=a.addr "
                       "LEFT JOIN device_room dr ON dr.addr=a.addr "
                       "LEFT JOIN rooms r ON r.id=dr.room_id "
                       "WHERE a.cleared IS NULL ORDER BY a.since")


def history(db_path, limit=50):
    return _q(db_path, "SELECT a.id, a.addr, a.kind, a.since, a.cleared, n.name "
                       "FROM alerts a LEFT JOIN names n ON n.addr=a.addr "
                       "WHERE a.cleared IS NOT NULL ORDER BY a.id DESC LIMIT ?", (limit,))


def send_mail(cfg, subject, body):
    e = cfg["email"]
    if not e["host"] or not e["to"]:
        raise ValueError("SMTP-Server und Empfänger fehlen")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = e["sender"] or e["username"] or "max-zentrale@localhost"
    msg["To"] = e["to"]
    msg.set_content(body)
    port = int(e["port"])
    if e["security"] == "ssl":
        s = smtplib.SMTP_SSL(e["host"], port, timeout=20,
                             context=ssl.create_default_context())
    else:
        s = smtplib.SMTP(e["host"], port, timeout=20)
    try:
        if e["security"] == "starttls":
            s.starttls(context=ssl.create_default_context())
        if e["username"]:
            s.login(e["username"], e["password"])
        s.send_message(msg)
    finally:
        try:
            s.quit()
        except Exception:
            pass


def describe(a):
    name = a.get("name") or "Unbenannt"
    room = (", " + a["room"]) if a.get("room") else ""
    return "%s (%s%s)" % (name, a["addr"], room)


class Alerter(threading.Thread):
    def __init__(self, db_path):
        super().__init__(daemon=True)
        self.db_path = db_path
        self.status = {"last_error": "", "last_mail": None}
        self.retry_at = {}
        self.wake = threading.Event()
        _q(db_path, "SELECT 1")
        c = sqlite3.connect(db_path)
        c.executescript(ALERT_SCHEMA)
        c.close()

    def run(self):
        while True:
            try:
                self.check()
            except Exception as e:               # keep the thread alive
                self.status["last_error"] = str(e)
            self.wake.wait(30)
            self.wake.clear()

    def check(self):
        now = datetime.now()
        stamp = now.isoformat(timespec="seconds")
        low = {r["addr"] for r in _q(self.db_path, "SELECT addr FROM devices "
                                                   "WHERE battery_low=1")}
        opened = {a["addr"]: a for a in open_alerts(self.db_path) if a["kind"] == "battery"}
        for addr in low - opened.keys():
            _q(self.db_path, "INSERT INTO alerts(addr,kind,since) VALUES(?,?,?)",
               (addr, "battery", stamp))
        for addr in opened.keys() - low:
            _q(self.db_path, "UPDATE alerts SET cleared=? WHERE id=?",
               (stamp, opened[addr]["id"]))
        cfg = load(self.db_path)
        if not cfg["email"]["enabled"]:
            return
        due = []
        for a in open_alerts(self.db_path):
            if a["kind"] != "battery":
                continue
            last = a["last_notified"]
            repeat = max(1, int(cfg["repeat_hours"])) * 3600
            if (last is None or (now - datetime.fromisoformat(last)).total_seconds() >= repeat) \
                    and time.time() >= self.retry_at.get(a["id"], 0):
                due.append(a)
        if not due:
            return
        lines = ["Batterie schwach bei:", ""] + ["  - %s – seit %s" % (
            describe(a), a["since"].replace("T", " ")) for a in due]
        lines += ["", "Bitte die Batterien tauschen. Den aktuellen Stand siehst du unter "
                      "„Alarme“ in der MAX! Zentrale."]
        try:
            send_mail(cfg, "MAX! Zentrale: Batterie schwach (%d)" % len(due), "\n".join(lines))
        except Exception as e:
            self.status["last_error"] = str(e) or repr(e)
            for a in due:
                self.retry_at[a["id"]] = time.time() + 900     # retry in 15 min
            return
        self.status.update(last_error="", last_mail=stamp)
        for a in due:
            _q(self.db_path, "UPDATE alerts SET last_notified=? WHERE id=?", (stamp, a["id"]))
