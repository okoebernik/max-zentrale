#!/usr/bin/env python3
"""Fake culfw (CUL / MAX! Cube) for demos and tests without hardware.

    python tools/sim_culfw.py [--port 2323]
    python max_server.py --port tcp://127.0.0.1:2323

Emits periodic MAX! telegrams for the fictitious devices in demo_data.py and
acknowledges SetTemperature commands like a real thermostat would.
"""
import argparse
import random
import socket
import threading
import time

from demo_data import DEVICES, SRC


def telegram(cnt, type_, src, dst, payload, rssi_dbm=-62.0, flags=0x00):
    body = bytes([cnt & 255, flags, type_]) + bytes.fromhex(src) + \
        bytes.fromhex(dst) + b"\x00" + payload
    rssi = int(round((rssi_dbm + 74) * 2)) & 255
    return "Z%02X%s%02X" % (len(body), body.hex().upper(), rssi)


class Sim:
    def __init__(self):
        self.dev = {d["addr"]: dict(d) for d in DEVICES}
        self.credit = 900
        self.cnt = random.randrange(256)

    def state_telegram(self, d):
        self.cnt += 1
        rssi = random.uniform(-78, -48)
        if d["kind"] == "heat":
            m = int(round(d["measured"] * 10))
            flags = (1 if d.get("manual") else 0) | (0x80 if d.get("battery_low") else 0)
            p = bytes([flags, d["valve"], int(d["setpoint"] * 2), (m >> 8) & 1, m & 255])
            return telegram(self.cnt, 0x60, d["addr"], "000000", p, rssi)
        if d["kind"] == "wall":
            m = int(round(d["measured"] * 10))
            p = bytes([0x00, 0x04, int(d["setpoint"] * 2), (m >> 8) & 1, m & 255])
            return telegram(self.cnt, 0x70, d["addr"], "000000", p, rssi)
        p = bytes([(0x02 if d["open"] else 0) | (0x80 if d.get("battery_low") else 0)])
        return telegram(self.cnt, 0x30, d["addr"], "000000", p, rssi)

    def jitter(self):
        for d in self.dev.values():
            if "measured" in d:
                d["measured"] = round(d["measured"] + random.uniform(-0.1, 0.1), 1)

    def handle_send(self, hexstr):
        """A 'Zs' command: acknowledge it like the addressed thermostat."""
        b = bytes.fromhex(hexstr)
        cnt, type_, dst = b[1], b[3], b[7:10].hex().upper()
        d = self.dev.get(dst)
        self.credit = max(0, self.credit - 106)
        if not d or d["kind"] == "window":
            return []
        if type_ == 0x40 and len(b) > 11:
            mode, temp = b[11] >> 6, (b[11] & 0x3F) / 2.0
            d["manual"] = mode == 1
            if mode == 1:
                d["setpoint"] = temp
        payload = bytes([1, 1 if d.get("manual") else 0, d.get("valve") or 0,
                         int(d["setpoint"] * 2)])
        return [telegram(cnt, 0x02, dst, SRC, payload)]


def serve(conn, sim):
    conn.settimeout(0.2)

    def out(s):
        conn.sendall((s + "\r\n").encode())
    buf, last, order, i = b"", time.time() + 1.0, list(sim.dev), 0
    try:
        while True:
            try:
                d = conn.recv(256)
                if not d:
                    return
                buf += d
            except socket.timeout:
                pass
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                cmd = line.strip().decode(errors="replace")
                if cmd == "V":
                    out("V 1.26.08 a-culfw SIMULATOR (demo, no radio)")
                elif cmd == "X":
                    out("21  %d" % sim.credit)
                elif cmd.startswith("Zs"):
                    for t in sim.handle_send(cmd[2:]):
                        time.sleep(0.4)
                        out(t)
            now = time.time()
            if now - last > 1.2:
                last = now
                sim.credit = min(1000, sim.credit + 1)
                sim.jitter()
                out(sim.state_telegram(sim.dev[order[i % len(order)]]))
                i += 1
    except OSError:
        pass
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=2323)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    sim = Sim()
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((a.host, a.port))
    srv.listen(4)
    print("culfw simulator on %s:%d" % (a.host, a.port), flush=True)
    while True:
        c, _ = srv.accept()
        threading.Thread(target=serve, args=(c, sim), daemon=True).start()


if __name__ == "__main__":
    main()
