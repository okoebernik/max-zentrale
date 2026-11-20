"""User accounts, login sessions and a small settings store (SQLite).

Roles:  admin    - everything, incl. users and MQTT settings
        operator - may control thermostats, rooms, profiles, pairing
        viewer   - read only
"""
import hashlib
import hmac
import secrets
import sqlite3
import time
from datetime import datetime

ROLES = ("admin", "operator", "viewer")
SESSION_TTL = 30 * 24 * 3600
COOKIE = "max_session"

AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
    pw_hash TEXT NOT NULL, role TEXT NOT NULL, created TEXT, last_login TEXT);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
"""


def hash_password(pw, salt=None):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$%s$%s" % (salt.hex(), h.hex())


def check_password(pw, stored):
    try:
        _, salt, _ = stored.split("$")
        return hmac.compare_digest(hash_password(pw, bytes.fromhex(salt)), stored)
    except ValueError:
        return False


def _tok_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, db_path):
        self.db_path = db_path
        self.fails = {}          # ip -> [timestamps of failed logins]
        c = self._c()
        c.executescript(AUTH_SCHEMA)
        c.commit()
        c.close()

    def _c(self):
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    # ---------------------------------------------------------------- users
    def user_count(self):
        c = self._c()
        try:
            return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        finally:
            c.close()

    def list_users(self):
        c = self._c()
        try:
            return [dict(r) for r in c.execute(
                "SELECT id, username, role, created, last_login FROM users "
                "ORDER BY username COLLATE NOCASE")]
        finally:
            c.close()

    @staticmethod
    def _valid(username, password, role):
        username = (username or "").strip()
        if not 1 <= len(username) <= 40 or not username.isprintable():
            raise ValueError("Benutzername: 1-40 Zeichen")
        if role not in ROLES:
            raise ValueError("unbekannte Rolle")
        if password is not None and len(password) < 8:
            raise ValueError("Passwort: mindestens 8 Zeichen")
        return username

    def create_user(self, username, password, role):
        username = self._valid(username, password, role)
        c = self._c()
        try:
            if c.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE",
                         (username,)).fetchone():
                raise ValueError("Benutzer existiert bereits")
            c.execute("INSERT INTO users(username,pw_hash,role,created) "
                      "VALUES(?,?,?,?)",
                      (username, hash_password(password), role,
                       datetime.now().isoformat(timespec="seconds")))
            c.commit()
        except sqlite3.IntegrityError:
            raise ValueError("Benutzer existiert bereits")
        finally:
            c.close()

    def _admins_left(self, c, excluding):
        return c.execute("SELECT COUNT(*) FROM users WHERE role='admin' "
                         "AND id<>?", (excluding,)).fetchone()[0]

    def delete_user(self, uid):
        c = self._c()
        try:
            row = c.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
            if not row:
                raise ValueError("unbekannter Benutzer")
            if row["role"] == "admin" and not self._admins_left(c, uid):
                raise ValueError("der letzte Administrator kann nicht gelöscht werden")
            c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            c.execute("DELETE FROM users WHERE id=?", (uid,))
            c.commit()
        finally:
            c.close()

    def set_role(self, uid, role):
        if role not in ROLES:
            raise ValueError("unbekannte Rolle")
        c = self._c()
        try:
            row = c.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
            if not row:
                raise ValueError("unbekannter Benutzer")
            if row["role"] == "admin" and role != "admin" \
                    and not self._admins_left(c, uid):
                raise ValueError("der letzte Administrator behält seine Rolle")
            c.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
            c.commit()
        finally:
            c.close()

    def set_password(self, uid, password, keep_token=None):
        if len(password or "") < 8:
            raise ValueError("Passwort: mindestens 8 Zeichen")
        c = self._c()
        try:
            c.execute("UPDATE users SET pw_hash=? WHERE id=?",
                      (hash_password(password), uid))
            # other devices have to log in again
            c.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?",
                      (uid, _tok_hash(keep_token or "")))
            c.commit()
        finally:
            c.close()

    # ------------------------------------------------------------- sessions
    def throttled(self, ip):
        now = time.time()
        recent = [t for t in self.fails.get(ip, []) if now - t < 300]
        self.fails[ip] = recent
        return len(recent) >= 5

    def login(self, ip, username, password):
        """Return a session token or None (wrong credentials / throttled)."""
        if self.throttled(ip):
            raise PermissionError("zu viele Fehlversuche, bitte 5 Minuten warten")
        c = self._c()
        try:
            row = c.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE",
                            ((username or "").strip(),)).fetchone()
            # always do the expensive hash so timing doesn't reveal the user
            ok = check_password(password or "", row["pw_hash"] if row else
                                hash_password("x"))
            if not (row and ok):
                self.fails.setdefault(ip, []).append(time.time())
                return None
            self.fails.pop(ip, None)
            return self._new_session(c, row["id"])
        finally:
            c.close()

    def _new_session(self, c, uid):
        token = secrets.token_urlsafe(32)
        c.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
        c.execute("INSERT INTO sessions(token_hash,user_id,expires) VALUES(?,?,?)",
                  (_tok_hash(token), uid, time.time() + SESSION_TTL))
        c.execute("UPDATE users SET last_login=? WHERE id=?",
                  (datetime.now().isoformat(timespec="seconds"), uid))
        c.commit()
        return token

    def start_session(self, uid):
        c = self._c()
        try:
            return self._new_session(c, uid)
        finally:
            c.close()

    def user_for_token(self, token):
        if not token:
            return None
        c = self._c()
        try:
            row = c.execute(
                "SELECT u.id, u.username, u.role FROM sessions s JOIN users u "
                "ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>?",
                (_tok_hash(token), time.time())).fetchone()
            return dict(row) if row else None
        finally:
            c.close()

    def logout(self, token):
        c = self._c()
        try:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (_tok_hash(token or ""),))
            c.commit()
        finally:
            c.close()

    def verify(self, uid, password):
        c = self._c()
        try:
            row = c.execute("SELECT pw_hash FROM users WHERE id=?", (uid,)).fetchone()
            return bool(row) and check_password(password or "", row["pw_hash"])
        finally:
            c.close()


def get_setting(db_path, key, default=None):
    c = sqlite3.connect(db_path)
    try:
        r = c.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
        return r[0] if r else default
    finally:
        c.close()


def put_setting(db_path, key, value):
    c = sqlite3.connect(db_path)
    try:
        c.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO "
                  "UPDATE SET v=excluded.v", (key, value))
        c.commit()
    finally:
        c.close()
