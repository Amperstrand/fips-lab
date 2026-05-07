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

# HCI packet types (from flags field bit 3-8, but HCI_UART uses first byte)
HCI_ACL_DATA = 0x02

# HCI ACL PB flags
PB_FIRST = 0x00
PB_CONTINUATION = 0x01

# L2CAP signaling CID
L2CAP_SIGNALLING_CID = 0x0001

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


# ============================================================================
# btsnoop v1 Parser
# ============================================================================

def parse_btsnoop(path: Path) -> list[BtsnoopRecord]:
    """Parse a btsnoop v1 file and return all records.

    Format:
      Header (24 bytes):
        - 8-byte magic: b'btsnoop\\x00'
        - 4-byte version: 1 (BE)
        - 4-byte datalink type: 1002 = HCI_UART (BE)
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
    if datalink != HCI_UART_DATALINK:
        raise ValueError(f"unsupported datalink type: {datalink}")

    records: list[BtsnoopRecord] = []
    offset = 24

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

        # For HCI_UART, flags bit 0: 0=sent, 1=received
        sent = (flags & 0x01) == 0

        records.append(BtsnoopRecord(
            data=raw[data_start:data_end],
            sent=sent,
            original_len=original_len,
            drops=drops,
            timestamp_us=timestamp_us,
        ))
        offset = data_end

    return records


# ============================================================================
# HCI ACL Reassembly
# ============================================================================

def reassemble_acl_packets(records: list[BtsnoopRecord]) -> list[tuple[bytes, bool]]:
    """Reassemble HCI ACL continuation fragments into complete L2CAP frames.

    HCI ACL Data Packet:
      [handle:12 + PB:2 + BC:2 (LE)][len:2 BE][data:len]
      PB flags: 0x00=first (start), 0x01=continuation

    Returns list of (data, sent) tuples for each complete reassembled frame.
    """
    # Per-connection-handle reassembly buffers
    buffers: dict[int, bytearray] = {}
    results: list[tuple[bytes, bool]] = []

    for record in records:
        data = record.data
        if len(data) < 1:
            continue

        # HCI_UART format: first byte is packet type
        pkt_type = data[0]
        if pkt_type != HCI_ACL_DATA:
            continue

        if len(data) < 5:
            continue

        # ACL header: 2 bytes (handle + PB/BC) + 2 bytes (length)
        acl_header = struct.unpack("<H", data[1:3])[0]
        acl_len = struct.unpack("<H", data[3:5])[0]

        handle = acl_header & 0x0FFF
        pb_flags = (acl_header >> 12) & 0x03

        payload = data[5:5 + acl_len]

        if pb_flags == PB_FIRST:
            # Start of a new L2CAP frame — discard any previous incomplete buffer
            buffers[handle] = bytearray(payload)
        elif pb_flags == PB_CONTINUATION:
            buf = buffers.get(handle)
            if buf is not None:
                buf.extend(payload)
            # If no buffer exists, this is an orphan continuation — skip
        else:
            # Other PB flags (broadcast) — not relevant for BLE
            continue

        # Check if we have a complete L2CAP frame
        buf = buffers.get(handle)
        if buf is None:
            continue

        # L2CAP frame: [length:2 LE][cid:2 LE][data:length]
        if len(buf) < 4:
            continue

        l2cap_len = struct.unpack("<H", buf[0:2])[0]
        total_frame_len = 4 + l2cap_len

        if len(buf) >= total_frame_len:
            # Complete frame — extract and remove from buffer
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
    """Extract L2CAP frames on FIPS PSM (133) using signalling tracking.

    L2CAP Basic Frame: [length:2 LE][cid:2 LE][data:length]
    Signalling (CID 0x0001):
      Conn Req:  [code=0x02][ident][len:2][psm:2][scid:2]
      Conn Resp: [code=0x03][ident][len:2][dcid:2][scid:2][result:2][status:2]

    Returns only frames on CIDs where PSM == 133 (FIPS).
    """
    # Track active L2CAP CoC connections: scid -> L2CAPConnection
    pending: dict[int, L2CAPConnection] = {}
    # Map: local_cid -> L2CAPConnection (after response)
    active: dict[int, L2CAPConnection] = {}
    # Map: ident -> scid (for matching request/response)
    ident_to_scid: dict[int, int] = {}

    fips_frames: list[tuple[bytes, bool]] = []

    for frame_data, sent in frames:
        if len(frame_data) < 4:
            continue

        l2cap_len = struct.unpack("<H", frame_data[0:2])[0]
        cid = struct.unpack("<H", frame_data[2:4])[0]
        payload = frame_data[4:4 + l2cap_len]

        if cid == L2CAP_SIGNALLING_CID:
            _process_signalling(payload, pending, active, ident_to_scid)
            continue

        # Check if this CID belongs to a FIPS connection
        conn = active.get(cid)
        if conn is not None and conn.psm == FIPS_L2CAP_PSM:
            fips_frames.append((payload, sent))

    return fips_frames


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
    """Parse FMP frames from L2CAP payloads.

    The BLE transport prepends a 2-byte BE length prefix to each FIPS
    packet (frame_payload() in io.rs). We strip this prefix to get
    the raw FMP packet.

    FMP common prefix (4 bytes):
      [ver(4bits)+phase(4bits)][flags:1][payload_len:2 LE]
    """
    frames: list[tuple[FmpFrame, bool]] = []

    for payload, sent in l2cap_payloads:
        # Strip the 2-byte BE length prefix from BLE transport framing
        if len(payload) < 2:
            continue
        frame_len = struct.unpack(">H", payload[0:2])[0]
        fmp_data = payload[2:2 + frame_len]

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
    for send_key, recv_key, _local, _peer in keys:
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
            )

    return None


# ============================================================================
# Summary Generation
# ============================================================================

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

    for fmp, _sent in fmp_frames:
        if fmp.phase != PHASE_ESTABLISHED:
            continue

        result = _decrypt_established_frame(fmp.raw, keys)
        if result is not None:
            decrypted.append(result)
            total_decrypted_bytes += result.plaintext_len
        else:
            failed_count += 1

    log.info("btsnoop: %d/%d decrypted (%d failed)",
             len(decrypted), len(decrypted) + failed_count, failed_count)

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
