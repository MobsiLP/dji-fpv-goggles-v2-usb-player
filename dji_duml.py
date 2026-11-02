#!/usr/bin/env python3
"""
DJI DUML v1 - Framing + Befehlstabelle
======================================

DUML ist das DJI-Kommunikationsformat fuer Steuerbefehle (nicht fuer den
Video-Stream!). Der USB-Video-Out der Goggles nutzt stattdessen das simple
RMVT-Bulk-Protokoll aus `dji_usb.py`.

Dieses Modul ist trotzdem nuetzlich, weil die DJI Fly APK 447 Befehle
enthaelt, die ueber DUML laufen - z.B.:

    0x01 0x00 0x91   uav_general_set_rndis_status_req
    0x01 0x06 0x3A   uav_rc_set_usb_mode_2014rc_req
    0x01 0x15 0x35   uav_goggles_app_to_glass_push_data_push
    0x01 0x02 0x4C   uav_camera_set_video_out_para_req
    0x01 0x02 0xB3   uav_camera_get_app_request_i_frame_req

Paketaufbau (DUML v1), little-endian:

    Offset  Groesse  Feld
    0       1       0x55 (SOF)
    1       2       length (Bits 0-9) | protocol_version (Bits 10-15)
    3       1       Header-CRC8 (ueber Byte 0..2, Seed 0x77)
    4       1       Sender
    5       1       Empfaenger
    6       2       Sequence Counter
    8       1       Flags: Encrypt (0-2), ACK (5-6), Typ (7)
    9       1       Command Set
    10      1       Command ID
    11      n       Payload
    n+11    2       CRC16 (ueber alle Bytes davor, Seed 0x3692)

CRC-Tabellen/Seeds stammen aus den verifizierten DJI-Protokoll-Tools
(o-gs/dji-firmware-tools, comm_dat2pcap.py).
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from dataclasses import dataclass
from typing import Optional

SOF = 0x55
CRC8_SEED = 0x77
CRC16_SEED = 0x3692

# Sender/Empfaenger (Auszug)
DEVICE_NAMES = {
    0: "Invalid/Any",
    1: "Camera",
    2: "App",
    3: "Flight Controller",
    4: "Gimbal",
    5: "Center Board",
    6: "Remote Controller",
    7: "Wi-Fi Air",
    9: "HD Link MCU Air",
    10: "PC",
    11: "Battery",
    12: "ESC",
    21: "Glass / Goggles",
}

CMD_SET_NAMES = {
    0x00: "General",
    0x01: "Special",
    0x02: "Camera",
    0x03: "Flight Control",
    0x04: "Gimbal",
    0x05: "Center Board",
    0x06: "Remote Control",
    0x07: "Wi-Fi",
    0x09: "HD Link",
    0x0C: "ESC",
    0x0D: "Battery",
    0x15: "Goggles / Glass",
    0x1C: "RM",
}


# --------------------------------------------------------------------------
# CRC
# --------------------------------------------------------------------------

def _make_crc8_table() -> list[int]:
    # CRC-8/MAXIM, reflektiert (Poly 0x8C)
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
        table.append(crc & 0xFF)
    return table


def _make_crc16_table() -> list[int]:
    # Reflektiertes CRC-16 (Poly 0x8408 / 0x1021)
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
        table.append(crc & 0xFFFF)
    return table


_CRC8_TABLE = _make_crc8_table()
_CRC16_TABLE = _make_crc16_table()


def crc8(data: bytes, seed: int = CRC8_SEED) -> int:
    chk = seed
    for b in data:
        chk = _CRC8_TABLE[(b ^ chk) & 0xFF]
    return chk


def crc16(data: bytes, seed: int = CRC16_SEED) -> int:
    chk = seed
    for b in data:
        chk = (chk >> 8) ^ _CRC16_TABLE[(b ^ chk) & 0xFF]
    return chk & 0xFFFF


# --------------------------------------------------------------------------
# Frame
# --------------------------------------------------------------------------

@dataclass
class DumlFrame:
    cmd_set: int
    cmd_id: int
    payload: bytes = b""
    sender: int = 2          # App
    receiver: int = 21       # Glass / Goggles
    seq: int = 0
    version: int = 1
    flags: int = 0x00        # z.B. 0x40 = ACK nach Ausfuehrung

    def pack(self) -> bytes:
        total = 11 + len(self.payload) + 2
        if total > 0x3FF:
            raise ValueError("DUML-Paket zu gross (max. 1023 Bytes)")

        out = bytearray()
        out.append(SOF)
        out += struct.pack("<H", (total & 0x3FF) | ((self.version & 0x3F) << 10))
        out.append(crc8(bytes(out)))
        out.append(self.sender & 0xFF)
        out.append(self.receiver & 0xFF)
        out += struct.pack("<H", self.seq & 0xFFFF)
        out.append(self.flags & 0xFF)
        out.append(self.cmd_set & 0xFF)
        out.append(self.cmd_id & 0xFF)
        out += self.payload
        out += struct.pack("<H", crc16(bytes(out)))
        return bytes(out)

    @classmethod
    def unpack(cls, data: bytes) -> "DumlFrame":
        if len(data) < 13 or data[0] != SOF:
            raise ValueError("Kein DUML-v1-Paket (SOF 0x55 fehlt)")
        raw_len, = struct.unpack_from("<H", data, 1)
        length = raw_len & 0x3FF
        version = (raw_len >> 10) & 0x3F
        if length > len(data):
            raise ValueError(f"Paket unvollstaendig ({length} > {len(data)})")

        hdr_crc = crc8(data[0:3])
        if hdr_crc != data[3]:
            raise ValueError(
                f"Header-CRC falsch (erwartet 0x{hdr_crc:02X}, "
                f"gefunden 0x{data[3]:02X})"
            )
        calc = crc16(data[:length - 2])
        got, = struct.unpack_from("<H", data, length - 2)
        if calc != got:
            raise ValueError(f"CRC16 falsch (erwartet 0x{calc:04X}, gefunden 0x{got:04X})")

        payload = bytes(data[11:length - 2])
        return cls(
            cmd_set=data[9],
            cmd_id=data[10],
            payload=payload,
            sender=data[4],
            receiver=data[5],
            seq=struct.unpack_from("<H", data, 6)[0],
            version=version,
            flags=data[8],
        )

    def describe(self) -> str:
        return (
            f"set=0x{self.cmd_set:02X} ({CMD_SET_NAMES.get(self.cmd_set, '?')})  "
            f"cmd=0x{self.cmd_id:02X}  len={len(self.payload)}  "
            f"sender={DEVICE_NAMES.get(self.sender, self.sender)}  "
            f"receiver={DEVICE_NAMES.get(self.receiver, self.receiver)}  "
            f"seq={self.seq}  flags=0x{self.flags:02X}"
        )


# --------------------------------------------------------------------------
# Befehlstabelle aus der APK
# --------------------------------------------------------------------------

def load_commands(path: Optional[str] = None) -> dict[tuple[int, int, int], str]:
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dji_commands.txt")
    commands: dict[tuple[int, int, int], str] = {}
    if not os.path.exists(path):
        return commands
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parts = line.strip().split("\t")
            if len(parts) < 4:
                continue
            try:
                key = (int(parts[0], 16), int(parts[1], 16), int(parts[2], 16))
            except ValueError:
                continue
            commands[key] = parts[3]
    return commands


def main() -> int:
    parser = argparse.ArgumentParser(description="DJI DUML v1 Helfer")
    parser.add_argument("--grep", default=None, help="Befehle filtern (Regex/Substring)")
    parser.add_argument("--demo", action="store_true", help="Beispielpaket bauen")
    args = parser.parse_args()

    commands = load_commands()
    if args.grep:
        needle = args.grep.lower()
        for (cs, cid, sub), name in sorted(commands.items()):
            if needle in name.lower():
                print(f"0x{cs:02X} 0x{cid:02X} 0x{sub:02X}  {name}")
        return 0

    if args.demo:
        frame = DumlFrame(
            cmd_set=0x01, cmd_id=0x00, payload=bytes([0x91, 0x01]),
            sender=2, receiver=21, seq=1, flags=0x40,
        )
        raw = frame.pack()
        print("Paket:", raw.hex(" "))
        print("Parsed:", DumlFrame.unpack(raw).describe())
        return 0

    print(f"{len(commands)} Befehle geladen (dji_commands.txt)")
    print("Beispiele:")
    for (cs, cid, sub), name in sorted(commands.items()):
        if any(k in name for k in ("usb", "goggle", "glass", "rndis", "video_out", "i_frame")):
            print(f"  0x{cs:02X} 0x{cid:02X} 0x{sub:02X}  {name}")
    print("\nFilter:  python dji_duml.py --grep usb")
    return 0


if __name__ == "__main__":
    sys.exit(main())
