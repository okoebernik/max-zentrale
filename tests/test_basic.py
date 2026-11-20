import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import max_alerts                                       # noqa: E402
import max_auth                                         # noqa: E402
import max_backup                                       # noqa: E402
from max_logger import SCHEMA, decode, parse_telegram   # noqa: E402
from max_send import build_raw, encode_day, time_payload, week_profile_packets  # noqa: E402
from max_server import EXTRA_SCHEMA, config_payload, validate_config, CFG_DEFAULT  # noqa: E402


class Protocol(unittest.TestCase):
    def test_parse_wall_thermostat(self):
        t = parse_telegram("Z0C81044213B9C41356DE002AD916")
        self.assertEqual((t["src"], t["dst"], t["type"]), ("13B9C4", "1356DE", 0x42))
        desc, reading, _ = decode(t)
        self.assertEqual(reading["kind"], "WallThermostat")
        self.assertAlmostEqual(reading["measured"], 21.7)
        self.assertAlmostEqual(reading["setpoint"], 21.0)

    def test_garbage_is_ignored(self):
        self.assertIsNone(parse_telegram("hello"))
        self.assertIsNone(parse_telegram("Z00"))

    def test_build_raw_roundtrip(self):
        hexs, cnt = build_raw("10B240", "1A0001", 0x40, bytes([0x42]), cnt=7)
        t = parse_telegram("Z" + hexs)
        self.assertEqual((t["msgcnt"], t["type"], t["src"], t["dst"]), (7, 0x40, "10B240", "1A0001"))

    def test_time_payload(self):
        p = time_payload(datetime(2026, 11, 20, 18, 30, 15))
        self.assertEqual(p[:3], bytes([26, 20, 18]))
        # month 11 = 0b1011: high bits in minute byte, low bits in second byte
        self.assertEqual(p[3], 30 | ((11 & 0x0C) << 4))
        self.assertEqual(p[4], 15 | ((11 & 0x03) << 6))

    def test_week_profile(self):
        pts = [{"temp": 17, "until": "06:00"}, {"temp": 21, "until": "22:00"}, {"temp": 17, "until": "24:00"}]
        self.assertEqual(len(encode_day(pts)), 13)
        self.assertEqual(len(week_profile_packets("10B240", "1A0001", "Mon", pts)), 1)

    def test_config_payload(self):
        self.assertEqual(config_payload(validate_config(CFG_DEFAULT)).hex().upper(), "2A223D09071803")
        with self.assertRaises(ValueError):
            validate_config(dict(CFG_DEFAULT, eco=25.0))


class Accounts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        self.auth = max_auth.Auth(self.db)

    def test_login_and_roles(self):
        self.auth.create_user("admin", "geheim-123", "admin")
        self.assertIsNone(self.auth.login("1.2.3.4", "admin", "falsch"))
        tok = self.auth.login("1.2.3.4", "admin", "geheim-123")
        self.assertEqual(self.auth.user_for_token(tok)["role"], "admin")
        self.auth.logout(tok)
        self.assertIsNone(self.auth.user_for_token(tok))

    def test_last_admin_protected(self):
        self.auth.create_user("admin", "geheim-123", "admin")
        uid = self.auth.list_users()[0]["id"]
        with self.assertRaises(ValueError):
            self.auth.delete_user(uid)
        with self.assertRaises(ValueError):
            self.auth.set_role(uid, "viewer")

    def test_validation_and_throttle(self):
        with self.assertRaises(ValueError):
            self.auth.create_user("x", "kurz", "admin")
        self.auth.create_user("a", "geheim-123", "admin")
        with self.assertRaises(ValueError):
            self.auth.create_user("A", "geheim-123", "viewer")     # case-insensitive duplicate
        for _ in range(5):
            self.auth.login("9.9.9.9", "a", "nope")
        with self.assertRaises(PermissionError):
            self.auth.login("9.9.9.9", "a", "geheim-123")


class Backup(unittest.TestCase):
    def test_create_restore(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "t.db")
        c = sqlite3.connect(db)
        c.executescript(SCHEMA + "CREATE TABLE names (addr TEXT PRIMARY KEY, name TEXT);")
        c.execute("INSERT INTO names VALUES('AAAAAA','alt')")
        c.commit()
        os.environ["MAX_BACKUP_DIR"] = os.path.join(tmp, "bk")
        name = max_backup.create(db, "manual")
        c.execute("UPDATE names SET name='neu'")
        c.commit()
        max_backup.restore(db, name)
        self.assertEqual(c.execute("SELECT name FROM names").fetchone()[0], "alt")
        self.assertTrue(any(b["name"].endswith("-vorher.db") for b in max_backup.listing(db)))
        with self.assertRaises(ValueError):
            max_backup.path_of(db, "../../etc/passwd")
        c.close()


class Alerts(unittest.TestCase):
    def test_battery_alarm_lifecycle(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "t.db")
        c = sqlite3.connect(db)
        c.executescript(SCHEMA + EXTRA_SCHEMA + max_auth.AUTH_SCHEMA)
        c.execute("INSERT INTO devices(addr,kind,battery_low) VALUES('1A0001','HeatingThermostat',0)")
        c.commit()
        a = max_alerts.Alerter(db)
        a.check()
        self.assertEqual(max_alerts.open_alerts(db), [])
        c.execute("UPDATE devices SET battery_low=1")
        c.commit()
        a.check()
        a.check()                                           # no duplicate alarm
        self.assertEqual(len(max_alerts.open_alerts(db)), 1)
        c.execute("UPDATE devices SET battery_low=0")
        c.commit()
        a.check()
        self.assertEqual(max_alerts.open_alerts(db), [])
        self.assertEqual(len(max_alerts.history(db)), 1)
        c.close()


if __name__ == "__main__":
    unittest.main()
