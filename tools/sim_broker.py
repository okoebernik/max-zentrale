#!/usr/bin/env python3
"""Tiny MQTT 3.1.1 broker stand-in for demos: accepts clients, acknowledges
CONNECT/SUBSCRIBE/PING and prints every PUBLISH it receives.

    python tools/sim_broker.py [--port 1883]
"""
import argparse
import socket
import struct
import threading


def client(c, quiet):
    buf = b""
    while True:
        d = c.recv(4096)
        if not d:
            return
        buf += d
        while len(buf) >= 2:
            n, m, i = 0, 1, 1
            while True:
                n += (buf[i] & 127) * m
                m *= 128
                i += 1
                if not buf[i - 1] & 128:
                    break
            if len(buf) < i + n:
                break
            t, body, buf = buf[0] >> 4, buf[i:i + n], buf[i + n:]
            if t == 1:
                c.sendall(b"\x20\x02\x00\x00")
            elif t == 8:
                c.sendall(b"\x90\x03" + body[:2] + b"\x00")
            elif t == 12:
                c.sendall(b"\xd0\x00")
            elif t == 3 and not quiet:
                tl = struct.unpack(">H", body[:2])[0]
                print(body[2:2 + tl].decode(),
                      body[2 + tl:].decode(errors="replace")[:160], flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", a.port))
    s.listen(4)
    print("MQTT stand-in broker on 127.0.0.1:%d" % a.port, flush=True)
    while True:
        c, _ = s.accept()
        threading.Thread(target=lambda c=c: client(c, a.quiet), daemon=True).start()


if __name__ == "__main__":
    main()
