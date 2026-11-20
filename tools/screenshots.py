#!/usr/bin/env python3
"""Regenerate the documentation screenshots from the fictitious demo setup.

Starts the culfw simulator, an MQTT stand-in broker and the server with a demo
database, then drives the web UI with Playwright (uses an installed Edge or
Chrome, no browser download needed):

    pip install playwright
    python tools/screenshots.py [--out docs/screenshots]
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HTTP, CUL, MQTT = 18080, 12323, 11883
BASE = "http://127.0.0.1:%d" % HTTP
PASSWORD = "demo-passwort-1"


def wait_http(url, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("server did not start: " + url)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "screenshots"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="max-demo-")
    db = os.path.join(tmp, "demo.db")
    subprocess.run([sys.executable, os.path.join(HERE, "make_demo_db.py"), db], check=True)
    env = dict(os.environ, MAX_PORT="tcp://127.0.0.1:%d" % CUL, MAX_BACKUP_DIR=os.path.join(tmp, "bk"))
    procs = [
        subprocess.Popen([sys.executable, os.path.join(HERE, "sim_culfw.py"), "--port", str(CUL)]),
        subprocess.Popen([sys.executable, os.path.join(HERE, "sim_broker.py"), "--port", str(MQTT), "--quiet"]),
    ]
    time.sleep(1)
    procs.append(subprocess.Popen([sys.executable, os.path.join(ROOT, "max_server.py"), "--db", db,
                                   "--http", str(HTTP)], env=env, cwd=ROOT))
    try:
        wait_http(BASE + "/api/auth")
        shoot(args.out)
    finally:
        for p in procs:
            p.terminate()


def shoot(out):
    from playwright.sync_api import sync_playwright

    def save(page, name, full=False):
        page.wait_for_timeout(700)
        page.screenshot(path=os.path.join(out, name + ".png"), full_page=full)
        print("  ", name)

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="msedge")
        except Exception:
            browser = pw.chromium.launch(channel="chrome")

        def context(scheme="dark", w=1440, h=900, mobile=False):
            ctx = browser.new_context(viewport={"width": w, "height": h}, color_scheme=scheme,
                                      device_scale_factor=1.5 if not mobile else 2, locale="de-DE",
                                      timezone_id="Europe/Berlin")
            return ctx

        # --- first start: create the administrator
        ctx = context()
        page = ctx.new_page()
        page.goto(BASE + "/")
        page.wait_for_selector("#loginform")
        page.fill("#lu", "admin")
        page.fill("#lp", PASSWORD)
        page.fill("#lp2", PASSWORD)
        save(page, "01-einrichtung")
        page.click("#loginbtn")
        page.wait_for_selector("#nav", state="visible")
        page.wait_for_selector(".kpi")

        api = ctx.request

        def post(path, body):
            r = api.post(BASE + path, data=json.dumps(body), headers={"Content-Type": "application/json"})
            return r.json()
        post("/api/users", {"action": "create", "username": "anna", "password": "anna-passwort-1", "role": "operator"})
        post("/api/users", {"action": "create", "username": "gast", "password": "gast-passwort-1", "role": "viewer"})
        post("/api/backup", {"action": "create"})
        post("/api/backup", {"action": "create"})
        post("/api/mqtt", {"enabled": True, "host": "127.0.0.1", "port": MQTT, "base": "max",
                           "discovery": True, "discovery_prefix": "homeassistant"})
        page.wait_for_timeout(5000)             # let the simulator deliver telegrams / MQTT connect

        def go(h, wait=None):
            page.goto(BASE + "/#" + h)
            page.reload()
            if wait:
                page.wait_for_selector(wait)
            page.wait_for_timeout(1200)

        go("home", ".kpi")
        save(page, "02-uebersicht", full=True)
        go("rooms", "#rooms .card")
        save(page, "03-raeume", full=True)
        go("programs/1A0001", "#profbox .tabs, #profbox button")
        save(page, "04-programme", full=True)
        go("history", "#histsel")
        page.select_option("#histsel", "1A0001")
        page.wait_for_timeout(1200)
        save(page, "05-verlauf")
        go("find")
        page.click("#findbtn")
        save(page, "06-geraet-finden")
        go("pairing")
        page.click("#pairbtn")
        save(page, "07-pairing")
        go("telegrams", "#log tr")
        save(page, "08-funktelegramme")
        go("console")
        page.wait_for_timeout(9000)
        save(page, "09-konsole")
        go("account")
        save(page, "10-konto")
        go("users", "#usersbox table")
        save(page, "11-benutzer")
        go("backup", "#backupbox table")
        save(page, "12-backup")
        go("mqtt", "#mqttbox")
        page.wait_for_timeout(1500)
        save(page, "13-mqtt")

        # a single room card, dark and light
        go("rooms", "#rooms .card")
        # --- light theme
        lctx = context("light")
        lctx.add_cookies(ctx.cookies())
        lp = lctx.new_page()
        lp.goto(BASE + "/#home")
        lp.wait_for_selector(".kpi")
        lp.wait_for_timeout(1500)
        save(lp, "14-hell-uebersicht")
        lp.goto(BASE + "/#rooms")
        lp.reload()
        lp.wait_for_selector("#rooms .card")
        save(lp, "15-hell-raeume")

        # --- login page (not signed in)
        gctx = context()
        gp = gctx.new_page()
        gp.goto(BASE + "/")
        gp.wait_for_selector("#loginform")
        gp.fill("#lu", "admin")
        save(gp, "16-anmeldung")

        # --- phone
        mctx = context("dark", 390, 844, mobile=True)
        mctx.add_cookies(ctx.cookies())
        mp = mctx.new_page()
        mp.goto(BASE + "/#home")
        mp.wait_for_selector(".kpi")
        mp.wait_for_timeout(1200)
        save(mp, "17-handy-uebersicht")
        browser.close()


if __name__ == "__main__":
    main()
