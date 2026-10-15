#!/usr/bin/env python3
"""Offline decryptor for FIPS bench captures: pcap + FIPS_LAB_KEYLOG -> plaintext.

Peels the FMP link layer (Noise IK transport) of captured FIPS traffic when
paired with a keylog produced by a daemon running with FIPS_LAB_KEYLOG set
(the amperstrand/lab-keylog instrumentation). The AEAD tag is the oracle:
each established frame is tried against every key generation and direction
recorded in the keylog, and exactly one combination authenticates.

Usage:
    python3 -m lib.fips_decrypt --pcap loopback.pcap --keylog keylog.jsonl
    python3 -m lib.fips_decrypt --btsnoop ble.snoop --keylog keylog.jsonl
    python3 -m lib.fips_decrypt ... --json            # machine-readable
    python3 -m lib.fips_decrypt ... --emit-decrypted D # write payloads to D

The btsnoop input (btmon -w, Android snoop) carries the FMP frames as
L2CAP payloads on the FIPS connection; ACL fragments are reassembled and
every completed L2CAP payload is fed through the same frame classifier,
whose version/phase validation rejects non-FIPS traffic, so no CID
filtering is needed.

Wire format (docs/reference/wire-formats.md, upstream d208a9db era):
  Common prefix (4B): version<<4|phase, flags, payload_len LE16.
  Established (phase 0): outer header 16B = prefix + receiver_idx LE32 +
  counter LE64, the whole 16B is the AEAD AAD; ciphertext payload_len;
  Poly1305 tag 16B. Nonce = 4 zero bytes + counter LE64. Plaintext starts
  with timestamp LE32 + msg_type u8. Phases 1/2 are IK handshake messages
  (not decryptable post-hoc; classified only).

Dependencies: python3 stdlib + `cryptography` (ChaCha20-Poly1305).
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from collections import Counter
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

MSG_TYPES = {
    0x00: "SessionDatagram",
    0x01: "SenderReport",
    0x02: "ReceiverReport",
    0x10: "TreeAnnounce",
    0x20: "FilterAnnounce",
    0x30: "LookupRequest",
    0x31: "LookupResponse",
    0x50: "Disconnect",
    0x51: "Heartbeat",
}

FSP_MSG_TYPES = {
    0x10: "Data",
    0x11: "SenderReport",
    0x12: "ReceiverReport",
    0x13: "PathMtuNotification",
    0x14: "CoordsWarmup",
    0x20: "CoordsRequired",
    0x21: "PathBroken",
    0x22: "MtuExceeded",
}

FSP_PHASE_NAMES = {1: "SessionSetup", 2: "SessionAck", 3: "SessionMsg3"}


def load_keylog(path: Path) -> list[dict]:
    """Group keylog lines into handshake generations keyed by handshake hash.

    Returns one entry per generation with the two transport keys and the
    role->peer mapping. k1 is always initiator->responder, k2 the reverse
    (split() is symmetric), so either line of a pair carries both.
    """
    lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    by_hash: dict[str, dict] = {}
    for line in lines:
        gen = by_hash.setdefault(
            line["h"],
            {"h": line["h"], "pattern": line["pattern"], "sides": []},
        )
        gen["sides"].append(
            {"role": line["role"], "peer": line["peer"],
             "k1": line["k1"], "k2": line["k2"], "ts": line["ts"]}
        )
    generations = []
    for i, gen in enumerate(
        sorted(by_hash.values(), key=lambda g: min(s["ts"] for s in g["sides"]))
    ):
        initiator = next((s for s in gen["sides"] if s["role"] == "initiator"), None)
        if initiator is None:
            continue  # one-sided record; the counterpart daemon had no keylog
        generations.append(
            {
                "index": len(generations),
                "h": gen["h"],
                "pattern": gen["pattern"],
                "first_ts": min(s["ts"] for s in gen["sides"]),
                # peer as recorded by the initiator = the responder's key
                "responder_xkey": initiator["peer"][2:],
                "k_init_to_resp": bytes.fromhex(initiator["k1"]),
                "k_resp_to_init": bytes.fromhex(initiator["k2"]),
            }
        )
    return generations


def iter_udp_payloads(pcap: Path):
    """Yield (ts, src, dst, sport, dport, payload) for UDP packets in a pcap.

    Supports the link types the bench produces: 0 (NULL/loopback) and 1
    (Ethernet). IPv4 and IPv6 are both handled.
    """
    data = pcap.read_bytes()
    if data[:4] == b"\xd4\xc3\xb2\xa1":
        endian, nano = "<", False
    elif data[:4] == b"\xa1\xb2\xc3\xd4":
        endian, nano = ">", False
    elif data[:4] == b"\x4d\x3c\xb2\xa1":
        endian, nano = "<", True
    elif data[:4] == b"\xa1\xb2\x3c\x4d":
        endian, nano = ">", True
    else:
        raise SystemExit(f"not a pcap file: {pcap}")
    linktype = struct.unpack(endian + "I", data[20:24])[0] & 0xFFFF
    off = 24
    while off + 16 <= len(data):
        ts_sec, ts_frac, incl_len = struct.unpack(endian + "II I", data[off : off + 12])[0:3]
        ts = ts_sec + (ts_frac / 1e9 if nano else ts_frac / 1e6)
        off += 16
        packet = data[off : off + incl_len]
        off += incl_len
        ip = _strip_link(linktype, packet)
        if ip is None:
            continue
        for record in _iter_udp(ip):
            yield (ts, *record)


BTSNOOP_MAGIC = b"btsnoop\x00"
# Microseconds between year 0 (btsnoop epoch) and the Unix epoch.
BTSNOOP_UNIX_OFFSET_US = 0x00E03AB44A676000


def iter_btsnoop_payloads(path: Path):
    """Yield (ts, src, dst, sport, dport, payload) for L2CAP frames in a
    btsnoop capture (btmon -w / Android), ACL-fragment reassembly included.

    btmon writes datalink 2001 (BlueZ monitor mode), whose records carry a
    pseudo-header; those are extracted via tshark, which reads the format
    natively. Plain H4 captures (datalink 1002) are parsed in-process.

    `src`/`dst` carry the ACL handle and direction, `sport` the L2CAP CID, so
    downstream reporting reads like the UDP case.
    """
    data = path.read_bytes()
    if data[:8] != BTSNOOP_MAGIC:
        raise SystemExit(f"not a btsnoop file: {path}")
    datalink = struct.unpack(">I", data[12:16])[0]
    if datalink in (2001, 2002):
        yield from _iter_monitor_l2cap(path)
        return
    off = 16
    reassembly: dict[int, bytearray] = {}
    while off + 24 <= len(data):
        orig_len, incl_len, flags, _drops, ts_raw = struct.unpack(
            ">IIIIQ", data[off : off + 24]
        )
        off += 24
        packet = data[off : off + incl_len]
        off += incl_len
        ts = (ts_raw - BTSNOOP_UNIX_OFFSET_US) / 1e6
        direction = "rx" if flags & 1 else "tx"
        if not packet or packet[0] != 0x02:
            continue
        handle_flags, data_len = struct.unpack("<HH", packet[1:5])
        handle = handle_flags & 0x0FFF
        pb = (handle_flags >> 12) & 0x3
        fragment = packet[5 : 5 + data_len]
        if pb == 0:
            buf = reassembly.get(handle)
            if buf is not None:
                buf.extend(fragment)
                if len(buf) >= 4:
                    l2len = struct.unpack("<H", buf[:2])[0]
                    if len(buf) >= 4 + l2len:
                        yield _l2cap_record(ts, direction, handle, bytes(buf[: 4 + l2len]))
                        reassembly.pop(handle, None)
            continue
        buf = bytearray(fragment)
        if len(buf) < 4:
            reassembly[handle] = buf
            continue
        l2len = struct.unpack("<H", buf[:2])[0]
        if len(buf) >= 4 + l2len:
            yield _l2cap_record(ts, direction, handle, bytes(buf[: 4 + l2len]))
        else:
            reassembly[handle] = buf


def _iter_monitor_l2cap(path: Path):
    """tshark-backed L2CAP extraction for monitor-mode btsnoop (requires
    tshark on the host — the bench boxes ship wireshark-cli)."""
    import subprocess

    out = subprocess.run(
        ["tshark", "-r", str(path), "-Y", "btl2cap", "-T", "fields",
         "-e", "frame.time_epoch", "-e", "btl2cap.cid", "-e", "btl2cap.payload"],
        check=True, capture_output=True, text=True,
    ).stdout
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or not parts[2]:
            continue
        ts = float(parts[0]) if parts[0] else 0.0
        cid = parts[1] or "?"
        payload = bytes.fromhex(parts[2].replace(":", ""))
        yield (ts, "ble", "peer", cid, 0, payload)


def _l2cap_record(ts: float, direction: str, handle: int, l2cap: bytes):
    l2len = struct.unpack("<H", l2cap[:2])[0]
    cid = struct.unpack("<H", l2cap[2:4])[0]
    payload = l2cap[4 : 4 + l2len]
    src = f"ble-{direction}"
    return (ts, src, "peer", handle, cid, payload)


def _strip_link(linktype: int, packet: bytes) -> bytes | None:
    if linktype == 0:
        return packet[4:]  # NULL/loopback: 4-byte address family
    if linktype == 1:
        return packet[14:]  # Ethernet
    if linktype == 113:
        return packet[16:]  # Linux cooked (SLL), tcpdump -i any
    if linktype == 276:
        return packet[20:]  # Linux cooked v2 (SLL2)
    return None


def _iter_udp(ip: bytes):
    version = ip[0] >> 4
    if version == 4:
        ihl = (ip[0] & 0x0F) * 4
        proto = ip[9]
        if proto != 17:
            return
        src = ".".join(str(b) for b in ip[12:16])
        dst = ".".join(str(b) for b in ip[16:20])
        total_len = struct.unpack("!H", ip[2:4])[0]
        transport = ip[ihl:total_len]
    elif version == 6:
        if ip[6] != 17:
            return
        src = ":".join(f"{ip[8 + i * 2]:02x}{ip[9 + i * 2]:02x}" for i in range(8))
        dst = ":".join(f"{ip[24 + i * 2]:02x}{ip[25 + i * 2]:02x}" for i in range(8))
        transport = ip[40:]
    else:
        return
    sport, dport = struct.unpack("!HH", transport[:4])
    length = struct.unpack("!H", transport[4:6])[0]
    yield src, dst, sport, dport, transport[8 : 8 + max(length - 8, 0)]


def classify(datagram: bytes) -> dict:
    prefix = datagram[:4]
    payload_len = struct.unpack("<H", prefix[2:4])[0]
    return {
        "version": prefix[0] >> 4,
        "phase": prefix[0] & 0x0F,
        "flags": prefix[1],
        "payload_len": payload_len,
    }


def iter_frames(datagram: bytes):
    """Yield (offset, meta) for each FMP frame in a datagram.

    The bench captures loopback traffic, where UDP GRO coalesces small
    datagrams into one pcap record (e.g. a data frame followed by a
    heartbeat), so a datagram can carry several frames back to back.
    Established frames are 16 + payload_len + tag 16; handshake msg1/msg2
    carry their Noise message unencrypted, so they are 4 + payload_len.
    Walking stops at the first prefix that does not parse.

    The MCU (microfips) L2CAP dialect prefixes each FMP frame with a
    u16-LE total-length word (length excluding the word itself); such
    frames are unwrapped automatically.
    """
    offset = 0
    while offset + 4 <= len(datagram):
        if (
            offset == 0
            and len(datagram) >= 6
            and (datagram[0] & 0xF0) != 0
            and int.from_bytes(datagram[0:2], "little") == len(datagram) - 2
        ):
            offset = 2
        meta = classify(datagram[offset:])
        if meta["version"] != 0 or meta["phase"] > 2:
            return
        if meta["phase"] == 0:
            frame_len = 16 + meta["payload_len"] + 16
        else:
            frame_len = 4 + meta["payload_len"]
        if offset + frame_len > len(datagram):
            return
        yield offset, meta
        offset += frame_len


def decrypt_fsp(frame: bytes, generations: list[dict]) -> dict | None:
    """Peel one FSP session-layer frame with the xk keylog generations.

    Layout: prefix(4) + counter LE64 = the 12-byte AEAD-AAD header;
    cleartext coordinates follow when the CP flag (bit 0) is set;
    ciphertext||tag after them. The U flag (bit 2) marks plaintext.
    """
    if len(frame) < 12 or frame[0] >> 4 != 0:
        return None
    phase = frame[0] & 0x0F
    flags = frame[1]
    payload_len = struct.unpack("<H", frame[2:4])[0]
    if phase in FSP_PHASE_NAMES:
        return {"phase": phase, "phase_name": FSP_PHASE_NAMES[phase]}
    if phase != 0:
        return None
    unencrypted = bool(flags & 0x04)
    header = frame[:12]
    counter = struct.unpack("<Q", header[4:12])[0]
    offset = 12
    coords_len = None
    if flags & 0x01:
        coords_len = _cleartext_coords_len(frame, offset)
        if coords_len is None:
            return None
        offset += coords_len
    body = frame[offset : offset + payload_len + (0 if unencrypted else 16)]
    if len(body) < payload_len:
        return None
    if unencrypted:
        plain = body[:payload_len]
        gen_index = direction = None
    else:
        nonce = b"\x00\x00\x00\x00" + struct.pack("<Q", counter)
        plain = None
        for gen in generations:
            if gen["pattern"] != "xk":
                continue
            for direction, key in (
                ("init->resp", gen["k_init_to_resp"]),
                ("resp->init", gen["k_resp_to_init"]),
            ):
                try:
                    plain = ChaCha20Poly1305(key).decrypt(nonce, body, header)
                    gen_index = gen["index"]
                    break
                except InvalidTag:
                    continue
            if plain is not None:
                break
        if plain is None:
            return None
    if len(plain) < 6:
        return None
    msg_type = plain[4]
    return {
        "phase": 0,
        "phase_name": "Established",
        "unencrypted": unencrypted,
        "cp_coords": coords_len,
        "generation": gen_index,
        "direction": direction,
        "counter": counter,
        "timestamp_ms": struct.unpack("<I", plain[:4])[0],
        "msg_type": msg_type,
        "msg_type_name": FSP_MSG_TYPES.get(msg_type, f"unknown-0x{msg_type:02x}"),
    }


def _cleartext_coords_len(frame: bytes, offset: int) -> int | None:
    def one_side(off: int) -> int | None:
        if off + 2 > len(frame):
            return None
        n = struct.unpack("<H", frame[off : off + 2])[0]
        end = off + 2 + 16 * n
        return end if end <= len(frame) else None

    src_end = one_side(offset)
    if src_end is None:
        return None
    dest_end = one_side(src_end)
    return None if dest_end is None else dest_end - offset


def peel_session_datagram(fmp_plaintext: bytes) -> bytes | None:
    """Return the FSP frame embedded in a decrypted SessionDatagram.

    FMP plaintext: timestamp(4) + the link message, and for a
    SessionDatagram that message is the 36-byte fixed header
    (msg_type, ttl, path_mtu, src, dest) followed by the FSP frame —
    the type byte at plaintext[4] is the SD header's own first field.
    """
    if len(fmp_plaintext) < 4 + 36:
        return None
    if fmp_plaintext[4] != 0x00:
        return None
    return fmp_plaintext[4 + 36 :]


def decrypt_established(datagram: bytes, generations: list[dict]) -> dict | None:
    header = datagram[:16]
    counter = struct.unpack("<Q", header[8:16])[0]
    # The AEAD input is everything after the header: ciphertext with the
    # Poly1305 tag appended (the length precheck guaranteed the +16).
    ciphertext_with_tag = datagram[16:]
    nonce = b"\x00\x00\x00\x00" + struct.pack("<Q", counter)
    for gen in generations:
        for direction, key in (
            ("init->resp", gen["k_init_to_resp"]),
            ("resp->init", gen["k_resp_to_init"]),
        ):
            try:
                plain = ChaCha20Poly1305(key).decrypt(nonce, ciphertext_with_tag, header)
            except InvalidTag:
                continue
            return {
                    "generation": gen["index"],
                    "h": gen["h"],
                    "pattern": gen["pattern"],
                    "direction": direction,
                    "counter": counter,
                    "plaintext": plain,
                    "timestamp_ms": struct.unpack("<I", plain[:4])[0],
                    "msg_type": plain[4],
                    "msg_type_name": MSG_TYPES.get(plain[4], f"unknown-0x{plain[4]:02x}"),
                }
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcar", "--pcap", dest="pcap", type=Path)
    ap.add_argument("--btsnoop", type=Path)
    ap.add_argument("--keylog", required=True, type=Path)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--emit-decrypted", type=Path)
    args = ap.parse_args()
    if bool(args.pcap) == bool(args.btsnoop):
        raise SystemExit("give exactly one of --pcap or --btsnoop")
    source = (
        iter_udp_payloads(args.pcap) if args.pcap else iter_btsnoop_payloads(args.btsnoop)
    )

    generations = load_keylog(args.keylog)
    per_pattern = Counter(g["pattern"] for g in generations)

    stats = Counter()
    msg_types = Counter()
    fsp_msg_types: dict[str, int] = {}
    per_generation = Counter()
    samples: list[dict] = []
    emit_dir = args.emit_decrypted
    if emit_dir:
        emit_dir.mkdir(parents=True, exist_ok=True)

    for ts, src, dst, sport, dport, payload in source:
        stats["udp_packets"] += 1
        if sport == 5353 or dport == 5353:
            # mDNS: DNS headers nibble-parse as FMP version-0 phase-0 and
            # pollute the ratio denominator; never carries FMP.
            stats["mdns_skipped"] = stats.get("mdns_skipped", 0) + 1
            continue
        frames = list(iter_frames(payload))
        if not frames:
            stats["too_short"] += 1
            continue
        if len(frames) > 1:
            stats["coalesced_datagrams"] += 1
        for offset, meta in frames:
            if meta["phase"] in (1, 2):
                stats[f"handshake_msg{meta['phase']}"] += 1
                continue
            frame = payload[offset : offset + 16 + meta["payload_len"] + 16]
            stats["established"] += 1
            record = decrypt_established(frame, generations)
            if record is None:
                stats["undecryptable"] += 1
                continue
            stats["decrypted"] += 1
            msg_types[record["msg_type_name"]] += 1
            per_generation[record["generation"]] += 1
            fsp_record = None
            if record["msg_type"] == 0x00:
                fsp_frame = peel_session_datagram(record["plaintext"])
                fsp_record = decrypt_fsp(fsp_frame, generations) if fsp_frame else None
                if fsp_record is not None:
                    if fsp_record["phase"] != 0:
                        stats[f"fsp_{fsp_record['phase_name']}"] = (
                            stats.get(f"fsp_{fsp_record['phase_name']}", 0) + 1
                        )
                    else:
                        stats["fsp_established"] = stats.get("fsp_established", 0) + 1
                        if fsp_record.get("unencrypted"):
                            stats["fsp_unencrypted"] = stats.get("fsp_unencrypted", 0) + 1
                            fsp_msg_types[f"{fsp_record['msg_type_name']} (U)"] = (
                                fsp_msg_types.get(f"{fsp_record['msg_type_name']} (U)", 0) + 1
                            )
                        elif fsp_record.get("generation") is not None:
                            stats["fsp_decrypted"] = stats.get("fsp_decrypted", 0) + 1
                            fsp_msg_types[fsp_record["msg_type_name"]] = (
                                fsp_msg_types.get(fsp_record["msg_type_name"], 0) + 1
                            )
                        else:
                            stats["fsp_undecryptable"] = stats.get("fsp_undecryptable", 0) + 1
                        if fsp_record.get("cp_coords"):
                            stats["fsp_cp_frames"] = stats.get("fsp_cp_frames", 0) + 1
                else:
                    stats["fsp_unparseable"] = stats.get("fsp_unparseable", 0) + 1
            record["fsp"] = fsp_record
            if emit_dir:
                (emit_dir / f"{ts:.6f}_gen{record['generation']}_{record['direction']}_c{record['counter']}.bin").write_bytes(record["plaintext"])
            if len(samples) < 8:
                samples.append(
                    {
                        "ts": ts,
                        "src": f"{src}:{sport}",
                        "dst": f"{dst}:{dport}",
                        "generation": record["generation"],
                        "direction": record["direction"],
                        "counter": record["counter"],
                        "msg_type": record["msg_type_name"],
                        "inner_ts_ms": record["timestamp_ms"],
                    }
                )

    report = {
        "capture": str(args.pcap or args.btsnoop),
        "keylog": str(args.keylog),
        "key_generations": len(generations),
        "generations_by_pattern": dict(per_pattern),
        "stats": dict(stats),
        "msg_types": dict(msg_types.most_common()),
        "fsp_msg_types": fsp_msg_types,
        "packets_per_generation": {str(k): v for k, v in sorted(per_generation.items())},
        "decrypt_ratio": (
            round(stats["decrypted"] / stats["established"], 4) if stats["established"] else None
        ),
        "samples": samples,
    }
    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print(f"capture: {report['capture']}  keylog: {report['keylog']}")
        print(f"key generations: {report['key_generations']} {dict(per_pattern)}")
        for k, v in sorted(stats.items()):
            print(f"  {k:20s} {v}")
        ratio = report["decrypt_ratio"]
        print(f"  decrypt ratio       {ratio if ratio is None else f'{ratio:.1%}'}")
        print("  message types:", ", ".join(f"{k}={v}" for k, v in msg_types.most_common()))
        if fsp_msg_types or any(k.startswith("fsp_") for k in stats):
            fsp_est = stats.get("fsp_established", 0)
            fsp_dec = stats.get("fsp_decrypted", 0)
            fsp_ratio = f"{fsp_dec / fsp_est:.1%}" if fsp_est else "n/a"
            print(f"  fsp established     {fsp_est}  decrypted {fsp_dec}  ratio {fsp_ratio}")
            print("  fsp message types:", ", ".join(f"{k}={v}" for k, v in sorted(fsp_msg_types.items(), key=lambda kv: -kv[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
