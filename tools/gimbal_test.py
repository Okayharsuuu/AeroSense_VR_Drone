#!/usr/bin/env python3
"""Talk to the SIYI A8 mini over its SDK and print every reply.

Run on the Jetson with gimbal_controller.py stopped:
    python3 gimbal_test.py
"""
import socket
import struct
import time

GIMBAL = ("192.168.144.25", 37260)


def _make_table():
    table = []
    for i in range(256):
        c = i << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) if c & 0x8000 else (c << 1)
        table.append(c & 0xFFFF)
    return table


TAB = _make_table()


def crc16(data):
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ TAB[((crc >> 8) ^ b) & 0xFF]
    return crc


seq = 0


def packet(cmd, data=b""):
    global seq
    p = struct.pack("<HBHHB", 0x6655, 1, len(data), seq, cmd) + data
    seq += 1
    return p + struct.pack("<H", crc16(p))


sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("0.0.0.0", 0))
sock.settimeout(1.5)


def send(label, cmd, data=b""):
    pkt = packet(cmd, data)
    print(f"\n>> {label}")
    print(f"   sent:  {pkt.hex(' ')}")
    sock.sendto(pkt, GIMBAL)
    try:
        reply, _ = sock.recvfrom(256)
    except socket.timeout:
        print("   NO REPLY from gimbal")
        return None
    print(f"   reply: {reply.hex(' ')}")
    if len(reply) < 10 or reply[0:2] != b"\x55\x66":
        print("   reply does not look like a SIYI packet")
        return None
    dlen = struct.unpack_from("<H", reply, 3)[0]
    body = reply[8:8 + dlen]
    ok = crc16(reply[:8 + dlen]) == struct.unpack_from("<H", reply, 8 + dlen)[0]
    print(f"   cmd 0x{reply[7]:02X}, {dlen} data bytes, CRC {'ok' if ok else 'BAD'}")
    return body


def show_attitude():
    body = send("Read current angles", 0x0D)
    if body and len(body) >= 6:
        yaw, pitch, roll = struct.unpack_from("<hhh", body, 0)
        print(f"   yaw={yaw / 10:.1f}  pitch={pitch / 10:.1f}  roll={roll / 10:.1f}")


body = send("Firmware version", 0x01)
if body and len(body) >= 8:
    cam, gim = struct.unpack_from("<II", body, 0)
    print(f"   camera fw 0x{cam:08X}, gimbal fw 0x{gim:08X}")

show_attitude()

send("Move to yaw 30, pitch 0", 0x0E, struct.pack("<hh", 300, 0))
time.sleep(2)
show_attitude()

send("Move to yaw 0, pitch -30", 0x0E, struct.pack("<hh", 0, -300))
time.sleep(2)
show_attitude()

send("Spin right slowly (speed mode)", 0x07, struct.pack("<bb", 40, 0))
time.sleep(1.5)
send("Stop spinning", 0x07, struct.pack("<bb", 0, 0))

send("Center", 0x08, b"\x01")
time.sleep(2)
show_attitude()
