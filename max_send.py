#!/usr/bin/env python3
"""Send a SetTemperature command to a MAX! thermostat via a culfw device.

Usage:
  python max_send.py 1A0001 21.5                # manual mode, 21.5 C
  python max_send.py 1A0001 --auto              # back to the weekly program
  python max_send.py 1A0001 21.5 --dry-run      # only show the telegram

Stop max_logger.py first - only one program can hold the serial port.

Telegram (see FHEM 14_CUL_MAX.pm):  Zs <len> <cnt> <flags> <type> <src> <dst>
<group> <payload>, len = byte count of everything after the len byte.
The thermostat only obeys a sender it is paired with.  Your devices were paired
with the original Cube, whose RF address shows up as the destination of their
time requests (10B240 here), so that address is used as sender by default.
"""
import argparse
import os
import random
import re
import sys
import time
from datetime import datetime

import serial

from max_logger import MODES, parse_telegram

TYPE_SET_TEMPERATURE = 0x40
TYPE_TIME_INFORMATION = 0x03
ACK_TIMEOUT = 4.0     # seconds to wait for the Ack per attempt
MAX_ATTEMPTS = 3


def build_raw(src, dst, type_, payload=b"", flags=0x00, cnt=None):
    """Return (hex string after 'Zs' incl. length byte, message counter)."""
    cnt = random.randrange(256) if cnt is None else cnt
    body = (bytes([cnt, flags, type_]) + bytes.fromhex(src)
            + bytes.fromhex(dst) + b"\x00" + payload)
    return bytes([len(body)]).hex().upper() + body.hex().upper(), cnt


def build_packet(src, dst, temp, mode, cnt=None):
    """SetTemperature telegram (mode 'auto' or 'manual')."""
    if mode == "auto":
        temp_bits = 0                      # 0 = follow the weekly program
    else:
        if not 4.5 <= temp <= 30.5:
            raise ValueError("temperature must be 4.5 .. 30.5 (30.5 = on)")
        temp_bits = int(round(temp * 2))
    mode_bits = {"auto": 0, "manual": 1}[mode]
    return build_raw(src, dst, TYPE_SET_TEMPERATURE,
                     bytes([(mode_bits << 6) | temp_bits]), cnt=cnt)


def time_payload(now=None):
    """5-byte TimeInformation payload (local time), as in FHEM CUL_MAX."""
    now = now or datetime.now()
    mon = now.month
    return bytes([now.year - 2000, now.day, now.hour,
                  now.minute | ((mon & 0x0C) << 4),
                  now.second | ((mon & 0x03) << 6)])


def build_time_packet(src, dst, cnt=None):
    return build_raw(src, dst, TYPE_TIME_INFORMATION, time_payload(),
                     flags=0x04, cnt=cnt)


TYPE_CONFIG_WEEK_PROFILE = 0x10
# MAX! numbers weekdays starting with Saturday
DAY_ID = {"Sat": 0, "Sun": 1, "Mon": 2, "Tue": 3, "Wed": 4, "Thu": 5, "Fri": 6}
MAX_POINTS = 13
_PAD_POINT = 0x4520            # 17.0 C until 24:00 (unused control point)


def parse_until(s):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(s))
    if not m:
        raise ValueError(f"invalid time {s!r}")
    h, mi = int(m.group(1)), int(m.group(2))
    if (h, mi) > (24, 0) or mi > 59 or mi % 5:
        raise ValueError(f"time {s!r}: use steps of 5 minutes, 00:05 .. 24:00")
    return h * 60 + mi


def encode_day(points):
    """points: [{'temp': 17.0, 'until': '06:00'}, ...] -> list of 13 uint16.

    Each point is the temperature that holds until the given time; the last
    point must end at 24:00 (the device ends the day at the first 00:00)."""
    if not 1 <= len(points) <= MAX_POINTS:
        raise ValueError(f"1 to {MAX_POINTS} switching points per day")
    out, last = [], 0
    for p in points:
        temp, until = float(p["temp"]), parse_until(p["until"])
        if not 4.5 <= temp <= 30.5 or (temp * 2) % 1:
            raise ValueError(f"temperature {temp}: 4.5 .. 30.5 in 0.5 steps")
        if until <= last:
            raise ValueError("times must be strictly increasing")
        last = until
        out.append((int(temp * 2) << 9) | (until // 5))
    if last != 24 * 60:
        raise ValueError("the last point must end at 24:00")
    return out + [_PAD_POINT] * (MAX_POINTS - len(out))


def week_profile_packets(src, dst, day, points):
    """Telegrams (hex, cnt) to write one day: part 0 = points 1-7, part 1 = 8-13
    (only sent when more than 7 points are used) - as FHEM does."""
    values = encode_day(points)
    used = len(points)
    packets = []
    for part, chunk in ((0, values[:7]), (1, values[7:])):
        if part == 1 and used <= 7:
            continue
        payload = bytes([(part << 4) | DAY_ID[day]])
        payload += b"".join(v.to_bytes(2, "big") for v in chunk)
        packets.append(build_raw(src, dst, TYPE_CONFIG_WEEK_PROFILE, payload))
    return packets


def read_credit(ser):
    ser.reset_input_buffer()
    ser.write(b"X\n")
    end = time.time() + 1.5
    buf = ""
    while time.time() < end:
        buf += ser.read(64).decode(errors="replace")
        m = re.search(r"^\s*\d+\s+(\d+)\s*$", buf, re.M)
        if m:
            return int(m.group(1))
    return None


def wait_ack(ser, src, dst, cnt, timeout):
    """Return the decoded Ack dict, or None."""
    end = time.time() + timeout
    buf = ""
    while time.time() < end:
        buf += ser.read(256).decode(errors="replace")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            t = parse_telegram(line)
            if t and t["type"] == 0x02 and t["src"] == dst and t["dst"] == src:
                p = t["payload"]
                if len(p) >= 4:
                    return {"ok": p[0] == 1, "mode": MODES[p[1] & 3],
                            "valve": p[2], "setpoint": (p[3] & 0x7F) / 2.0,
                            "battery_low": (p[1] >> 7) & 1, "cnt": t["msgcnt"]}
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("addr", help="thermostat RF address, e.g. 1A0001")
    ap.add_argument("temp", nargs="?", type=float, help="target temperature")
    ap.add_argument("--auto", action="store_true",
                    help="switch to auto mode (weekly program)")
    ap.add_argument("--port", default="COM3" if os.name == "nt" else "/dev/ttyACM0")
    ap.add_argument("--src", default="10B240",
                    help="sender address (default: original Cube)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    dst = args.addr.upper()
    if not re.fullmatch(r"[0-9A-F]{6}", dst) or not re.fullmatch(
            r"[0-9A-Fa-f]{6}", args.src):
        sys.exit("addresses must be 6 hex digits")
    if args.auto:
        mode, temp = "auto", 0.0
    elif args.temp is None:
        sys.exit("give a temperature or --auto")
    else:
        mode, temp = "manual", args.temp
    src = args.src.upper()

    packet, cnt = build_packet(src, dst, temp, mode)
    print(f"Telegram: Zs{packet}  ({mode}"
          f"{'' if mode == 'auto' else f' {temp:.1f} C'} -> {dst}, from {src})")
    if args.dry_run:
        return

    ser = serial.Serial(args.port, 38400, timeout=0.5)
    try:
        ser.write(b"Zr\n")
        time.sleep(0.3)
        ser.reset_input_buffer()
        need = 100 + len(packet) * 4 // 10 + 1   # 1 s preamble + payload
        for attempt in range(1, MAX_ATTEMPTS + 1):
            waited = 0
            while True:
                credit = read_credit(ser)
                if credit is None:
                    sys.exit("culfw did not report its send credit")
                if credit >= need:
                    break
                if waited > 120:
                    sys.exit("not enough send credit (1% duty cycle)")
                print(f"  waiting for send credit ({credit}/{need}) ...")
                time.sleep(5)
                waited += 5
            print(f"Attempt {attempt}: sending (credit {credit}) ...")
            ser.reset_input_buffer()
            ser.write(f"Zs{packet}\n".encode())
            ack = wait_ack(ser, src, dst, cnt, ACK_TIMEOUT + 1.5)
            if ack:
                state = "OK" if ack["ok"] else "REJECTED"
                print(f"Ack {state}: mode={ack['mode']} setpoint="
                      f"{ack['setpoint']:.1f} C valve={ack['valve']}%"
                      f"{' BATTERY LOW' if ack['battery_low'] else ''}")
                sys.exit(0 if ack["ok"] else 2)
            print("  no Ack")
        print("No acknowledgement received - the thermostat did not accept "
              "the command (not paired with this sender address, or out of "
              "range).")
        sys.exit(1)
    finally:
        try:
            ser.write(b"Zx\n")
            ser.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
