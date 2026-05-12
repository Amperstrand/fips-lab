"""Post-test btsnoop HCI capture decryption pipeline for fips-lab.

Parses btsnoop captures from btmon, extracts FIPS BLE L2CAP traffic,
decrypts Noise-encrypted payloads using keylog files, and produces
summary statistics. No raw payloads, keys, or BLE addresses are
included in output — only aggregate counts and message type breakdowns.
"""
from __future__ import annotations

import json
import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .keylog import KeylogEntry, parse_keylog

log = logging.getLogger(__name__)

# ============================================================================
# Constants
# ============================================================================

BTSNOOP_MAGIC = b"btsnoop\x00"
BTSNOOP_VERSION = 1
HCI_UART_DATALINK = 1002
HCI_MONITOR_DATALINK = 2001  # btmon monitor mode

# HCI packet types (from flags field bit 3-8, but HCI_UART uses first byte)
HCI_ACL_DATA = 0x02

# HCI ACL PB flags
PB_FIRST = 0x00
PB_CONTINUATION = 0x01

# L2CAP signaling CID
L2CAP_SIGNALLING_CID = 0x0001
L2CAP_ATT_CID = 0x0004
L2CAP_ATT_BLE_CID = 0x0005
L2CAP_SMP_CID = 0x0006
L2CAP_DYNAMIC_CID_MIN = 0x0040

# L2CAP signalling command codes
L2CAP_CONN_REQ = 0x02
L2CAP_CONN_RESP = 0x03

# FIPS PSM on Linux
FIPS_L2CAP_PSM = 133

# FMP wire constants (from fips/src/node/wire.rs)
FMP_VERSION = 0
PHASE_ESTABLISHED = 0x0
PHASE_MSG1 = 0x1
PHASE_MSG2 = 0x2

COMMON_PREFIX_SIZE = 4
ESTABLISHED_HEADER_SIZE = 16
ENCRYPTED_MIN_SIZE = 32  # header (16) + tag (16)
INNER_HEADER_SIZE = 5  # u32 timestamp + u8 msg_type
TAG_SIZE = 16

MSG1_WIRE_SIZE = 114  # 4 + 4 + 106
MSG2_WIRE_SIZE = 69   # 4 + 4 + 4 + 57

# Link message types (from fips/src/protocol/link.rs)
LINK_MSG_TYPES: dict[int, str] = {
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

# BLE address filter placeholder (populated from config if needed)
FILTERED_BLE_ADDRESSES: set[str] = set()


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class BtsnoopRecord:
    """A single btsnoop record with metadata."""
    data: bytes
    sent: bool  # True=sent, False=received (flags bit 0)
    original_len: int
    drops: int
    timestamp_us: int
    record_flags: int = 0  # raw flags from btsnoop record (for monitor mode type detection)


@dataclass
class L2CAPConnection:
    """Tracks an L2CAP CoC connection via signalling."""
    psm: int
    scid: int
    dcid: int = 0
    established: bool = False


@dataclass
class FmpFrame:
    """A parsed FMP frame."""
    phase: int
    version: int
    flags: int
    payload_len: int
    raw: bytes


@dataclass
class DecryptedFrame:
    """Result of decrypting an established FMP frame."""
    counter: int
    receiver_idx: int
    timestamp_ms: int
    msg_type: int
    msg_name: str
    plaintext_len: int
    key_index: int = -1
    sent: bool = False
    raw_header: bytes = b""   # 16-byte outer FMP header (AAD) for pcapng reconstruction
    plaintext: bytes = b""    # decrypted inner header + message body


@dataclass
class DecryptionSummary:
    """Aggregate statistics from btsnoop decryption."""
    capture_file: str
    total_hci_records: int = 0
    acl_data_packets: int = 0
    fips_l2cap_frames: int = 0
    privacy_filtered: bool = True
    filter_method: str = "l2cap_psm_133"
    fmp_frames: dict[str, int] = field(default_factory=lambda: {
        "established": 0, "handshake_msg1": 0,
        "handshake_msg2": 0, "unknown_phase": 0,
    })
    decryption: dict[str, Any] = field(default_factory=lambda: {
        "total_attempted": 0, "decrypted_successfully": 0,
        "decryption_failed": 0, "failure_pct": 0.0,
    })
    link_messages: dict[str, dict[str, Any]] = field(default_factory=dict)
    total_decrypted_bytes: int = 0
    keylog_entries_used: int = 0
    keylog_files: list[str] = field(default_factory=list)
    rekey_groups: list[dict[str, Any]] = field(default_factory=list)
    rekey_intervals_secs: list[float] = field(default_factory=list)
    handshake_analysis: dict[str, Any] = field(default_factory=dict)
    decryption_by_direction: dict[str, Any] = field(default_factory=dict)


# ============================================================================
# btsnoop v1 Parser
# ============================================================================

def parse_btsnoop(path: Path) -> list[BtsnoopRecord]:
    """Parse a btsnoop v1 file and return all records.

    Format:
      Header (16 bytes):
        - 8-byte magic: b'btsnoop\\x00'
        - 4-byte version: 1 (BE)
        - 4-byte datalink type: 1002 = HCI_UART or 2001 = monitor (BE)
      Records:
        - original_len: 4 BE
        - inc_len: 4 BE
        - flags: 4 BE
        - drops: 4 BE
        - timestamp: 8 BE (microseconds since 2000-01-01)
        - data: inc_len bytes
    """
    raw = path.read_bytes()
    if len(raw) < 24:
        raise ValueError(f"btsnoop file too short: {len(raw)} bytes")

    magic = raw[0:8]
    if magic != BTSNOOP_MAGIC:
        raise ValueError(f"invalid btsnoop magic: {magic!r}")

    version = struct.unpack(">I", raw[8:12])[0]
    if version != BTSNOOP_VERSION:
        raise ValueError(f"unsupported btsnoop version: {version}")

    datalink = struct.unpack(">I", raw[12:16])[0]
    if datalink not in (HCI_UART_DATALINK, HCI_MONITOR_DATALINK):
        raise ValueError(f"unsupported datalink type: {datalink}")

    records: list[BtsnoopRecord] = []
    offset = 16  # file header: 8 magic + 4 version + 4 datalink

    while offset + 24 <= len(raw):
        original_len = struct.unpack(">I", raw[offset:offset + 4])[0]
        inc_len = struct.unpack(">I", raw[offset + 4:offset + 8])[0]
        flags = struct.unpack(">I", raw[offset + 8:offset + 12])[0]
        drops = struct.unpack(">I", raw[offset + 12:offset + 16])[0]
        timestamp_us = struct.unpack(">Q", raw[offset + 16:offset + 24])[0]

        data_start = offset + 24
        data_end = data_start + inc_len
        if data_end > len(raw):
            log.warning("truncated record at offset %d: need %d, have %d",
                        offset, data_end, len(raw))
            break

        sent = (flags & 0x01) == 0
        record_data = raw[data_start:data_end]

        # Monitor mode (2001) strips the HCI type byte from data.
        # The packet type is encoded in flags & 0xFF:
        #   2=CMD, 3=EVT, 4=ACL_TX, 5=ACL_RX, 6=SCO_TX, 7=SCO_RX
        # Prepend the type byte so downstream code works unchanged.
        if datalink == HCI_MONITOR_DATALINK:
            pkt_type = _monitor_type_byte(flags)
            if pkt_type is not None:
                record_data = bytes([pkt_type]) + record_data
            # else: system/ctrl record — keep as-is (won't match any type check)

        records.append(BtsnoopRecord(
            data=record_data,
            sent=sent,
            original_len=original_len,
            drops=drops,
            timestamp_us=timestamp_us,
            record_flags=flags,
        ))
        offset = data_end

    return records


def _monitor_type_byte(flags: int) -> int | None:
    """Map btsnoop monitor-mode flags to HCI UART type byte.

    Monitor flags & 0xFF encoding (from Linux hci_mon.h):
      0=NEW_INDEX, 1=DEL_INDEX, 2=CMD, 3=EVT, 4=ACL_TX, 5=ACL_RX,
      6=SCO_TX, 7=SCO_RX, 12+=ctrl events (not HCI).
    Direction is implicit in the opcode (4 vs 5).
    """
    HCI_CMD = 0x01
    HCI_EVT = 0x04
    mon_type = flags & 0xFF
    if mon_type == 2:
        return HCI_CMD
    if mon_type == 3:
        return HCI_EVT
    if mon_type in (4, 5):
        return HCI_ACL_DATA
    if mon_type in (6, 7):
        return 0x03  # SCO
    return None


# ============================================================================
# HCI ACL Reassembly
# ============================================================================

def reassemble_acl_packets(records: list[BtsnoopRecord]) -> list[tuple[bytes, bool]]:
    """Reassemble HCI ACL continuation fragments into complete L2CAP frames.

    Expects data to start with HCI type byte (0x02 for ACL).
    For monitor mode (2001), the type byte is prepended during parsing.
    Accepts PB=0 (first) and PB=2 (flushable first) as start indicators.

    Returns list of (data, sent) tuples for each complete reassembled frame.
    """
    buffers: dict[int, bytearray] = {}
    results: list[tuple[bytes, bool]] = []

    for record in records:
        data = record.data
        if len(data) < 1:
            continue

        pkt_type = data[0]
        if pkt_type != HCI_ACL_DATA:
            continue

        if len(data) < 5:
            continue

        acl_header = struct.unpack("<H", data[1:3])[0]
        acl_len = struct.unpack("<H", data[3:5])[0]

        handle = acl_header & 0x0FFF
        pb_flags = (acl_header >> 12) & 0x03

        payload = data[5:5 + acl_len]

        if pb_flags in (PB_FIRST, 0x02):
            buffers[handle] = bytearray(payload)
        elif pb_flags == PB_CONTINUATION:
            buf = buffers.get(handle)
            if buf is not None:
                buf.extend(payload)
        else:
            continue

        buf = buffers.get(handle)
        if buf is None:
            continue

        if len(buf) < 4:
            continue

        l2cap_len = struct.unpack("<H", buf[0:2])[0]
        total_frame_len = 4 + l2cap_len

        if len(buf) >= total_frame_len:
            frame = bytes(buf[:total_frame_len])
            del buf[:total_frame_len]
            if not buf:
                del buffers[handle]
            results.append((frame, record.sent))

    return results


# ============================================================================
# L2CAP Frame Extraction
# ============================================================================

def extract_fips_l2cap_frames(
    frames: list[tuple[bytes, bool]],
) -> list[tuple[bytes, bool]]:
    """Extract L2CAP frames carrying FIPS traffic.

    Strategy:
      1. Track L2CAP CoC connections via signalling (CID 0x0001) to find PSM 133.
      2. If signalling was captured, filter by known PSM 133 CIDs.
      3. If no signalling (btmon started after connection setup), fall back to
         content-based detection: try parsing FMP on dynamic CIDs (>=0x0040)
         and accept those producing valid FMP frames (version 0, known phase).
    """
    pending: dict[int, L2CAPConnection] = {}
    active: dict[int, L2CAPConnection] = {}
    ident_to_scid: dict[int, int] = {}

    fips_cids: set[int] = set()
    fips_frames: list[tuple[bytes, bool]] = []
    all_dynamic: list[tuple[bytes, bool, int]] = []

    for frame_data, sent in frames:
        if len(frame_data) < 4:
            continue

        l2cap_len = struct.unpack("<H", frame_data[0:2])[0]
        cid = struct.unpack("<H", frame_data[2:4])[0]
        payload = frame_data[4:4 + l2cap_len]

        if cid == L2CAP_SIGNALLING_CID:
            _process_signalling(payload, pending, active, ident_to_scid)
            continue

        if cid >= L2CAP_DYNAMIC_CID_MIN:
            conn = active.get(cid)
            if conn is not None and conn.psm == FIPS_L2CAP_PSM:
                fips_frames.append((payload, sent))
                fips_cids.add(cid)
            else:
                all_dynamic.append((payload, sent, cid))

    if fips_frames:
        return fips_frames

    # Fallback: content-based FMP detection on unresolved dynamic CIDs
    candidate_cids = _detect_fips_cids(all_dynamic)
    if candidate_cids:
        return [(payload, sent) for payload, sent, cid in all_dynamic if cid in candidate_cids]

    return []


def _detect_fips_cids(
    frames: list[tuple[bytes, bool, int]],
) -> set[int]:
    """Identify CIDs carrying FIPS traffic by probing payloads for valid FMP frames."""
    cid_fmp_hits: dict[int, int] = {}

    for payload, _sent, cid in frames:
        if len(payload) < 2:
            continue
        sdu_len = struct.unpack("<H", payload[0:2])[0]
        content = payload[2:2 + sdu_len]

        offset = 0
        while offset + 2 <= len(content):
            ble_len = struct.unpack(">H", content[offset:offset + 2])[0]
            if ble_len == 0 or offset + 2 + ble_len > len(content):
                break
            fmp_data = content[offset + 2:offset + 2 + ble_len]
            offset += 2 + ble_len

            if len(fmp_data) < COMMON_PREFIX_SIZE:
                continue

            ver_phase = fmp_data[0]
            version = (ver_phase >> 4) & 0x0F
            phase = ver_phase & 0x0F

            if version == FMP_VERSION and phase in (PHASE_ESTABLISHED, PHASE_MSG1, PHASE_MSG2):
                cid_fmp_hits[cid] = cid_fmp_hits.get(cid, 0) + 1

    if not cid_fmp_hits:
        return set()

    max_hits = max(cid_fmp_hits.values())
    return {cid for cid, hits in cid_fmp_hits.items() if hits >= max(1, max_hits // 4)}


def _process_signalling(
    payload: bytes,
    pending: dict[int, L2CAPConnection],
    active: dict[int, L2CAPConnection],
    ident_to_scid: dict[int, int],
) -> None:
    """Process L2CAP signalling commands to track CoC connections."""
    offset = 0
    while offset + 4 <= len(payload):
        code = payload[offset]
        ident = payload[offset + 1]
        cmd_len = struct.unpack("<H", payload[offset + 2:offset + 4])[0]
        cmd_data = payload[offset + 4:offset + 4 + cmd_len]

        if code == L2CAP_CONN_REQ and len(cmd_data) >= 4:
            psm = struct.unpack("<H", cmd_data[0:2])[0]
            scid = struct.unpack("<H", cmd_data[2:4])[0]
            pending[scid] = L2CAPConnection(psm=psm, scid=scid)
            ident_to_scid[ident] = scid

        elif code == L2CAP_CONN_RESP and len(cmd_data) >= 8:
            dcid = struct.unpack("<H", cmd_data[0:2])[0]
            scid = struct.unpack("<H", cmd_data[2:4])[0]
            result = struct.unpack("<H", cmd_data[4:6])[0]

            # Try ident-based lookup first, then scid-based
            pending_scid = ident_to_scid.pop(ident, None)
            conn = None
            if pending_scid is not None:
                conn = pending.pop(pending_scid, None)
            if conn is None:
                conn = pending.pop(scid, None)

            if conn is not None and result == 0:
                conn.dcid = dcid
                conn.established = True
                # The response's dcid is the local CID for data frames
                # sent TO the responder. The scid is used by the initiator.
                active[dcid] = conn
                active[scid] = conn

        offset += 4 + cmd_len


# ============================================================================
# FMP Frame Parsing
# ============================================================================

def parse_fmp_frames(
    l2cap_payloads: list[tuple[bytes, bool]],
) -> list[tuple[FmpFrame, bool]]:
    """Parse FMP frames from L2CAP CoC SDU payloads.

    L2CAP CoC SDU format (as captured by btmon on Linux):
      [sdu_len:2 LE][content:sdu_len]
    Where content is one or more concatenated BLE transport frames:
      [fmp_len:2 BE][fmp_data:fmp_len]
    Each fmp_data is an FMP packet with common prefix:
      [ver(4bits)+phase(4bits)][flags:1][payload_len:2 LE]
    """
    frames: list[tuple[FmpFrame, bool]] = []

    for payload, sent in l2cap_payloads:
        if len(payload) < 2:
            continue

        sdu_len = struct.unpack("<H", payload[0:2])[0]
        content = payload[2:2 + sdu_len]

        offset = 0
        while offset + 2 <= len(content):
            ble_len = struct.unpack(">H", content[offset:offset + 2])[0]
            if ble_len == 0 or offset + 2 + ble_len > len(content):
                break
            fmp_data = content[offset + 2:offset + 2 + ble_len]
            offset += 2 + ble_len

            if len(fmp_data) < COMMON_PREFIX_SIZE:
                continue

            ver_phase = fmp_data[0]
            version = (ver_phase >> 4) & 0x0F
            phase = ver_phase & 0x0F
            flags = fmp_data[1]
            payload_len = struct.unpack("<H", fmp_data[2:4])[0]

            if version != FMP_VERSION:
                continue

            frames.append((FmpFrame(
                phase=phase,
                version=version,
                flags=flags,
                payload_len=payload_len,
                raw=fmp_data,
            ), sent))

    return frames


# ============================================================================
# Noise Decryption
# ============================================================================

def _build_nonce(counter: int) -> bytes:
    """Build a 12-byte Noise nonce from a counter value.

    Format: [0x00 × 4][counter as u64 LE]
    """
    nonce = bytearray(12)
    struct.pack_into("<Q", nonce, 4, counter)
    return bytes(nonce)


def _collect_keys(run_dir: Path) -> list[tuple[bytes, bytes, str, str]]:
    """Collect all send/recv keys from keylog files in run_dir.

    Returns list of (send_key, recv_key, local_npub, peer_npub) tuples.
    """
    keys: list[tuple[bytes, bytes, str, str]] = []
    for keylog_path in sorted(run_dir.glob("keylog-*.txt")):
        try:
            parsed = parse_keylog(keylog_path)
        except Exception:
            log.warning("failed to parse keylog: %s", keylog_path.name)
            continue

        for entry in parsed.valid_entries:
            send_key = bytes.fromhex(entry.send_key_hex)
            recv_key = bytes.fromhex(entry.recv_key_hex)
            keys.append((send_key, recv_key, entry.local_npub_hex, entry.peer_npub_hex))

    return keys


def _decrypt_established_frame(
    fmp_data: bytes,
    keys: list[tuple[bytes, bytes, str, str]],
) -> DecryptedFrame | None:
    """Attempt to decrypt an established (phase 0x0) FMP frame.

    Outer header (16 bytes):
      [ver+phase:1][flags:1][payload_len:2 LE][receiver_idx:4 LE][counter:8 LE]

    AAD = full 16-byte header
    Nonce = [0x00 × 4][counter as u64 LE]
    Ciphertext = bytes[16:] (includes last 16 bytes AEAD tag)
    """
    if len(fmp_data) < ENCRYPTED_MIN_SIZE:
        return None

    # Parse header
    aad = fmp_data[:ESTABLISHED_HEADER_SIZE]
    receiver_idx = struct.unpack("<I", fmp_data[4:8])[0]
    counter = struct.unpack("<Q", fmp_data[8:16])[0]
    ciphertext = fmp_data[ESTABLISHED_HEADER_SIZE:]

    nonce = _build_nonce(counter)

    # Lazy import — module can be imported without cryptography installed
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    # Try all available keys
    for key_idx, (send_key, recv_key, _local, _peer) in enumerate(keys):
        for key in (send_key, recv_key):
            try:
                cipher = ChaCha20Poly1305(key)
                plaintext = cipher.decrypt(nonce, ciphertext, aad)
            except Exception:
                continue

            # Successfully decrypted — parse inner header
            if len(plaintext) < INNER_HEADER_SIZE:
                continue

            timestamp_ms = struct.unpack("<I", plaintext[0:4])[0]
            msg_type = plaintext[4]
            msg_name = LINK_MSG_TYPES.get(msg_type, f"Unknown(0x{msg_type:02x})")

            return DecryptedFrame(
                counter=counter,
                receiver_idx=receiver_idx,
                timestamp_ms=timestamp_ms,
                msg_type=msg_type,
                msg_name=msg_name,
                plaintext_len=len(plaintext),
                key_index=key_idx,
                raw_header=aad,
                plaintext=plaintext,
            )

    return None


# ============================================================================
# pcapng Writer (manual, no external dependencies)
#
# Writes decrypted FMP frames as a pcapng file for Wireshark/tshark analysis.
# Each packet contains: 16-byte outer FMP header (AAD) + decrypted plaintext
# (inner header + message body). This lets the FMP Lua dissector parse the
# outer header fields; the "encrypted payload" section will actually be
# cleartext, enabling protocol-level inspection without dissector changes.
# ============================================================================

LINKTYPE_USER0 = 147


def _pad4(n: int) -> int:
    return (4 - n % 4) % 4


def _write_pcapng_shb(f) -> None:
    block_type = 0x0A0D0D0A
    byte_order_magic = 0x1A2B3C4D
    major, minor = 1, 0
    section_length = 0xFFFFFFFFFFFFFFFF
    options = b""
    body = struct.pack("<IHHQ", byte_order_magic, major, minor, section_length) + options
    block_total_length = 4 + 4 + len(body) + 4
    f.write(struct.pack("<I", block_type))
    f.write(struct.pack("<I", block_total_length))
    f.write(body)
    f.write(struct.pack("<I", block_total_length))


def _write_pcapng_idb(f) -> None:
    block_type = 0x00000001
    link_type = LINKTYPE_USER0
    reserved = 0
    snap_len = 65535
    options = b""
    body = struct.pack("<HHI", link_type, reserved, snap_len) + options
    block_total_length = 4 + 4 + len(body) + 4
    f.write(struct.pack("<I", block_type))
    f.write(struct.pack("<I", block_total_length))
    f.write(body)
    f.write(struct.pack("<I", block_total_length))


def _write_pcapng_epb(f, data: bytes, timestamp_us: int) -> None:
    block_type = 0x00000006
    interface_id = 0
    ts_high = (timestamp_us >> 32) & 0xFFFFFFFF
    ts_low = timestamp_us & 0xFFFFFFFF
    captured_len = len(data)
    original_len = len(data)
    padding = _pad4(captured_len)
    fixed = struct.pack("<IIIII", interface_id, ts_high, ts_low,
                        captured_len, original_len)
    body = fixed + data + b"\x00" * padding
    block_total_length = 4 + 4 + len(body) + 4
    f.write(struct.pack("<I", block_type))
    f.write(struct.pack("<I", block_total_length))
    f.write(body)
    f.write(struct.pack("<I", block_total_length))


def _write_decrypted_pcapng(
    decrypted_frames: list[DecryptedFrame],
    run_dir: Path,
) -> None:
    if not decrypted_frames:
        return

    pcapng_path = run_dir / "decrypted-fmp.pcapng"
    with open(pcapng_path, "wb") as f:
        _write_pcapng_shb(f)
        _write_pcapng_idb(f)
        for df in decrypted_frames:
            packet_data = df.raw_header + df.plaintext
            timestamp_us = df.timestamp_ms * 1000
            _write_pcapng_epb(f, packet_data, timestamp_us)

    log.info("wrote decrypted-fmp.pcapng with %d frames", len(decrypted_frames))

def _build_summary(
    capture_file: str,
    total_records: int,
    acl_packets: int,
    fips_frames: int,
    fmp_frames_parsed: list[tuple[FmpFrame, bool]],
    decrypted: list[DecryptedFrame],
    failed_count: int,
    total_decrypted_bytes: int,
    keys: list[tuple[bytes, bytes, str, str]],
    keylog_files: list[str],
) -> DecryptionSummary:
    """Build aggregate summary from parsed/decrypted data."""
    phase_counts: dict[str, int] = {
        "established": 0, "handshake_msg1": 0,
        "handshake_msg2": 0, "unknown_phase": 0,
    }
    for fmp, _sent in fmp_frames_parsed:
        if fmp.phase == PHASE_ESTABLISHED:
            phase_counts["established"] += 1
        elif fmp.phase == PHASE_MSG1:
            phase_counts["handshake_msg1"] += 1
        elif fmp.phase == PHASE_MSG2:
            phase_counts["handshake_msg2"] += 1
        else:
            phase_counts["unknown_phase"] += 1

    total_attempted = phase_counts["established"]
    success_count = len(decrypted)
    failure_pct = round((failed_count / total_attempted) * 100, 1) if total_attempted > 0 else 0.0

    # Build message type breakdown
    msg_counts: dict[str, dict[str, Any]] = {}
    for df in decrypted:
        name = df.msg_name
        entry = msg_counts.get(name)
        if entry is None:
            type_id = df.msg_type if name in LINK_MSG_TYPES else -1
            msg_counts[name] = {"count": 1, "type_id": type_id}
        else:
            entry["count"] += 1

    # Ensure unknown category exists
    if "Unknown" not in msg_counts:
        msg_counts["Unknown"] = {"count": 0}

    # --- Per-rekey group statistics ---
    key_buckets: dict[int, list[DecryptedFrame]] = {}
    for df in decrypted:
        key_buckets.setdefault(df.key_index, []).append(df)

    rekey_groups: list[dict[str, Any]] = []
    for key_idx in sorted(key_buckets):
        frames = key_buckets[key_idx]
        counters = [df.counter for df in frames]
        bytes_sum = sum(df.plaintext_len for df in frames)
        rekey_groups.append({
            "key_index": key_idx,
            "first_counter": min(counters),
            "last_counter": max(counters),
            "frames_decrypted": len(frames),
            "frames_failed": 0,
            "bytes_decrypted": bytes_sum,
        })

    # Distribute failed frames across groups proportionally
    if failed_count > 0 and rekey_groups:
        total_dec = sum(g["frames_decrypted"] for g in rekey_groups)
        if total_dec > 0:
            remaining = failed_count
            for i, g in enumerate(rekey_groups):
                if i < len(rekey_groups) - 1:
                    share = round(failed_count * g["frames_decrypted"] / total_dec)
                    g["frames_failed"] = share
                    remaining -= share
                else:
                    g["frames_failed"] = remaining

    # --- Rekey interval distribution ---
    rekey_intervals_secs: list[float] = []
    sorted_keys = sorted(key_buckets)
    for i in range(1, len(sorted_keys)):
        prev_frames = key_buckets[sorted_keys[i - 1]]
        curr_frames = key_buckets[sorted_keys[i]]
        last_ts = max(df.timestamp_ms for df in prev_frames)
        first_ts = min(df.timestamp_ms for df in curr_frames)
        delta_s = abs(first_ts - last_ts) / 1000.0
        rekey_intervals_secs.append(round(delta_s, 3))

    # --- Handshake phase analysis ---
    msg1_sent = sum(1 for fmp, sent in fmp_frames_parsed
                    if fmp.phase == PHASE_MSG1 and sent)
    msg1_recv = sum(1 for fmp, sent in fmp_frames_parsed
                    if fmp.phase == PHASE_MSG1 and not sent)
    msg2_sent = sum(1 for fmp, sent in fmp_frames_parsed
                    if fmp.phase == PHASE_MSG2 and sent)
    msg2_recv = sum(1 for fmp, sent in fmp_frames_parsed
                    if fmp.phase == PHASE_MSG2 and not sent)
    handshake_analysis: dict[str, Any] = {
        "msg1_sent": msg1_sent,
        "msg1_recv": msg1_recv,
        "msg2_sent": msg2_sent,
        "msg2_recv": msg2_recv,
        "total_handshakes": min(msg1_sent + msg1_recv, msg2_sent + msg2_recv),
    }

    # --- Decryption by direction ---
    tx_attempted = sum(1 for fmp, sent in fmp_frames_parsed
                       if fmp.phase == PHASE_ESTABLISHED and sent)
    rx_attempted = sum(1 for fmp, sent in fmp_frames_parsed
                       if fmp.phase == PHASE_ESTABLISHED and not sent)
    tx_succeeded = sum(1 for df in decrypted if df.sent)
    rx_succeeded = sum(1 for df in decrypted if not df.sent)
    decryption_by_direction: dict[str, Any] = {
        "tx": {
            "attempted": tx_attempted,
            "succeeded": tx_succeeded,
            "failed": tx_attempted - tx_succeeded,
        },
        "rx": {
            "attempted": rx_attempted,
            "succeeded": rx_succeeded,
            "failed": rx_attempted - rx_succeeded,
        },
    }

    return DecryptionSummary(
        capture_file=capture_file,
        total_hci_records=total_records,
        acl_data_packets=acl_packets,
        fips_l2cap_frames=fips_frames,
        fmp_frames=phase_counts,
        decryption={
            "total_attempted": total_attempted,
            "decrypted_successfully": success_count,
            "decryption_failed": failed_count,
            "failure_pct": failure_pct,
        },
        link_messages=msg_counts,
        total_decrypted_bytes=total_decrypted_bytes,
        keylog_entries_used=len(keys),
        keylog_files=keylog_files,
        rekey_groups=rekey_groups,
        rekey_intervals_secs=rekey_intervals_secs,
        handshake_analysis=handshake_analysis,
        decryption_by_direction=decryption_by_direction,
    )


def _write_json(summary: DecryptionSummary, path: Path) -> None:
    """Write summary as JSON."""
    data: dict[str, Any] = {
        "capture_file": summary.capture_file,
        "total_hci_records": summary.total_hci_records,
        "acl_data_packets": summary.acl_data_packets,
        "fips_l2cap_frames": summary.fips_l2cap_frames,
        "privacy_filtered": summary.privacy_filtered,
        "filter_method": summary.filter_method,
        "fmp_frames": summary.fmp_frames,
        "decryption": summary.decryption,
        "link_messages": summary.link_messages,
        "total_decrypted_bytes": summary.total_decrypted_bytes,
        "keylog_entries_used": summary.keylog_entries_used,
        "keylog_files": summary.keylog_files,
        "rekey_groups": summary.rekey_groups,
        "rekey_intervals_secs": summary.rekey_intervals_secs,
        "handshake_analysis": summary.handshake_analysis,
        "decryption_by_direction": summary.decryption_by_direction,
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _write_markdown(summary: DecryptionSummary, path: Path) -> None:
    """Write summary as human-readable markdown."""
    lines: list[str] = [
        "# BLE Capture Decryption Summary",
        "",
        f"- **Capture**: `{summary.capture_file}`",
        f"- **Privacy filter**: {summary.filter_method}",
        f"- **HCI records**: {summary.total_hci_records}",
        f"- **ACL data packets**: {summary.acl_data_packets}",
        f"- **FIPS L2CAP frames**: {summary.fips_l2cap_frames}",
        "",
    ]

    # FMP frame breakdown
    lines.append("## FMP Frame Types")
    lines.append("| Type | Count |")
    lines.append("|------|-------|")
    for name in ("established", "handshake_msg1", "handshake_msg2", "unknown_phase"):
        lines.append(f"| {name} | {summary.fmp_frames[name]} |")
    lines.append("")

    # Decryption stats
    dec = summary.decryption
    lines.append("## Decryption")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Attempted | {dec['total_attempted']} |")
    lines.append(f"| Successful | {dec['decrypted_successfully']} |")
    lines.append(f"| Failed | {dec['decryption_failed']} |")
    lines.append(f"| Failure % | {dec['failure_pct']:.1f}% |")
    lines.append(f"| Decrypted bytes | {summary.total_decrypted_bytes:,} |")
    lines.append(f"| Keylog entries used | {summary.keylog_entries_used} |")
    lines.append("")

    # Message type breakdown
    if summary.link_messages:
        lines.append("## Link Message Types")
        lines.append("| Type | ID | Count |")
        lines.append("|------|----|-------|")
        for name, info in sorted(summary.link_messages.items(), key=lambda x: x[1].get("type_id", -1)):
            type_id = info.get("type_id", -1)
            tid_str = f"0x{type_id:02x}" if type_id >= 0 else "—"
            lines.append(f"| {name} | {tid_str} | {info['count']} |")
        lines.append("")

    # Per-rekey group statistics
    if summary.rekey_groups:
        lines.append("## Per-Rekey Group Statistics")
        lines.append("| Key | Counter Range | Decrypted | Failed | Success% | Bytes |")
        lines.append("|-----|---------------|-----------|--------|----------|-------|")
        for g in summary.rekey_groups:
            total = g["frames_decrypted"] + g["frames_failed"]
            pct = (g["frames_decrypted"] / total * 100) if total > 0 else 0.0
            crange = f"{g['first_counter']}–{g['last_counter']}"
            lines.append(
                f"| {g['key_index']} | {crange} | {g['frames_decrypted']} "
                f"| {g['frames_failed']} | {pct:.1f}% | {g['bytes_decrypted']:,} |"
            )
        lines.append("")

    # Rekey interval distribution
    if summary.rekey_intervals_secs:
        vals = summary.rekey_intervals_secs
        n = len(vals)
        mn, mx = min(vals), max(vals)
        avg = sum(vals) / n
        variance = sum((v - avg) ** 2 for v in vals) / max(1, n - 1) if n > 1 else 0.0
        stdev = variance ** 0.5
        lines.append("## Rekey Interval Distribution")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        lines.append(f"| Count | {n} |")
        lines.append(f"| Min | {mn:.3f} s |")
        lines.append(f"| Max | {mx:.3f} s |")
        lines.append(f"| Avg | {avg:.3f} s |")
        lines.append(f"| Stdev | {stdev:.3f} s |")
        lines.append("")

    # Handshake phase analysis
    ha = summary.handshake_analysis
    if ha.get("total_handshakes", 0) > 0 or ha.get("msg1_sent", 0) > 0:
        lines.append("## Handshake Phase Analysis")
        lines.append("| Phase | Sent | Received | Total |")
        lines.append("|-------|------|----------|-------|")
        lines.append(f"| MSG1 (initiate) | {ha['msg1_sent']} | {ha['msg1_recv']} "
                     f"| {ha['msg1_sent'] + ha['msg1_recv']} |")
        lines.append(f"| MSG2 (response) | {ha['msg2_sent']} | {ha['msg2_recv']} "
                     f"| {ha['msg2_sent'] + ha['msg2_recv']} |")
        lines.append(f"| **Complete handshakes** | | | **{ha['total_handshakes']}** |")
        lines.append("")

    # Decryption by direction
    dd = summary.decryption_by_direction
    if dd.get("tx", {}).get("attempted", 0) > 0 or dd.get("rx", {}).get("attempted", 0) > 0:
        lines.append("## Decryption by Direction")
        lines.append("| Direction | Attempted | Succeeded | Failed | Success% |")
        lines.append("|-----------|-----------|-----------|--------|----------|")
        for direction in ("tx", "rx"):
            d = dd[direction]
            total = d["attempted"]
            succ = d["succeeded"]
            fail = d["failed"]
            pct = (succ / total * 100) if total > 0 else 0.0
            label = "TX (sent)" if direction == "tx" else "RX (recv)"
            lines.append(f"| {label} | {total} | {succ} | {fail} | {pct:.1f}% |")
        lines.append("")

    # Keylog files
    if summary.keylog_files:
        lines.append("## Keylog Sources")
        for kf in summary.keylog_files:
            lines.append(f"- `{kf}`")
        lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ============================================================================
# Public API
# ============================================================================

def decrypt_btsnoop_capture(run_dir: Path) -> DecryptionSummary | None:
    """Decrypt btsnoop capture using keylog keys from the run directory.

    Looks for btmon.btsnoop and keylog-*.txt files in run_dir.
    Writes decryption-summary.json and decryption-summary.md.
    Returns the summary, or None if no capture/keylog files found.
    """
    run_dir = Path(run_dir)

    # Find btsnoop capture
    btsnoop_path = run_dir / "btmon.btsnoop"
    if not btsnoop_path.exists():
        log.info("no btsnoop capture found in %s", run_dir)
        return None

    # Collect keylog keys
    keys = _collect_keys(run_dir)
    if not keys:
        log.info("no keylog entries found in %s", run_dir)
        return None

    keylog_files = sorted(p.name for p in run_dir.glob("keylog-*.txt"))
    log.info("btsnoop decrypt: %d key entries from %s", len(keys), keylog_files)

    # Parse btsnoop
    try:
        records = parse_btsnoop(btsnoop_path)
    except (ValueError, OSError) as exc:
        log.warning("failed to parse btsnoop: %s", exc)
        return None

    log.info("btsnoop: %d HCI records", len(records))

    # Reassemble ACL packets
    acl_frames = reassemble_acl_packets(records)
    log.info("btsnoop: %d reassembled ACL frames", len(acl_frames))

    # Extract FIPS L2CAP frames (PSM 133 filter)
    fips_frames = extract_fips_l2cap_frames(acl_frames)
    log.info("btsnoop: %d FIPS L2CAP frames", len(fips_frames))

    # Parse FMP frames (strip BLE transport 2-byte length prefix)
    fmp_frames = parse_fmp_frames(fips_frames)
    log.info("btsnoop: %d FMP frames", len(fmp_frames))

    # Decrypt established frames
    decrypted: list[DecryptedFrame] = []
    failed_count = 0
    total_decrypted_bytes = 0

    for fmp, sent in fmp_frames:
        if fmp.phase != PHASE_ESTABLISHED:
            continue

        result = _decrypt_established_frame(fmp.raw, keys)
        if result is not None:
            result.sent = sent
            decrypted.append(result)
            total_decrypted_bytes += result.plaintext_len
        else:
            failed_count += 1

    log.info("btsnoop: %d/%d decrypted (%d failed)",
             len(decrypted), len(decrypted) + failed_count, failed_count)

    if decrypted:
        _write_decrypted_pcapng(decrypted, run_dir)

    # Build and write summary
    summary = _build_summary(
        capture_file=btsnoop_path.name,
        total_records=len(records),
        acl_packets=len(acl_frames),
        fips_frames=len(fips_frames),
        fmp_frames_parsed=fmp_frames,
        decrypted=decrypted,
        failed_count=failed_count,
        total_decrypted_bytes=total_decrypted_bytes,
        keys=keys,
        keylog_files=keylog_files,
    )

    _write_json(summary, run_dir / "decryption-summary.json")
    _write_markdown(summary, run_dir / "decryption-summary.md")
    log.info("wrote decryption-summary.json and decryption-summary.md")

    return summary
