# GENERATED from jmcorgan/fips v0.4.0 (780dbadea096). Do not hand-edit.
# Regenerate via "python3 generate.py --fips-root <path> --ref <ref>"

"""FIPS protocol constants. Generated from jmcorgan/fips v0.4.0."""

FIPS_REF = "jmcorgan/fips v0.4.0 (780dbadea096)"

# --- Base sizes (from src/noise/mod.rs) ---
TAG_SIZE = 16
PUBKEY_SIZE = 33
EPOCH_SIZE = 8
EPOCH_ENCRYPTED_SIZE = 24  # EPOCH_SIZE + TAG_SIZE = 8 + 16
MAX_MESSAGE_SIZE = 65535
REPLAY_WINDOW_SIZE = 2048

# --- Noise handshake sizes (from src/noise/mod.rs) ---
HANDSHAKE_MSG1_SIZE = 106   # PUBKEY_SIZE + PUBKEY_SIZE + TAG_SIZE + EPOCH_ENCRYPTED_SIZE
HANDSHAKE_MSG2_SIZE = 57    # PUBKEY_SIZE + EPOCH_ENCRYPTED_SIZE
XK_HANDSHAKE_MSG1_SIZE = 33  # PUBKEY_SIZE
XK_HANDSHAKE_MSG2_SIZE = 57  # PUBKEY_SIZE + EPOCH_ENCRYPTED_SIZE
XK_HANDSHAKE_MSG3_SIZE = 73  # PUBKEY_SIZE + TAG_SIZE + EPOCH_ENCRYPTED_SIZE

# --- Protocol version (from src/protocol/mod.rs) ---
PROTOCOL_VERSION = 1

# --- FMP framing (from src/node/wire.rs) ---
FMP_VERSION = 0
COMMON_PREFIX_SIZE = 4
ESTABLISHED_HEADER_SIZE = 16
ENCRYPTED_MIN_SIZE = 32     # ESTABLISHED_HEADER_SIZE + TAG_SIZE = 16 + 16
INNER_HEADER_SIZE = 5
MSG1_WIRE_SIZE = 114        # COMMON_PREFIX_SIZE + 4 + HANDSHAKE_MSG1_SIZE = 4 + 4 + 106
MSG2_WIRE_SIZE = 69         # COMMON_PREFIX_SIZE + 4 + 4 + HANDSHAKE_MSG2_SIZE = 4 + 4 + 4 + 57

# --- FMP phases ---
PHASE_ESTABLISHED = 0x00
PHASE_MSG1 = 0x01
PHASE_MSG2 = 0x02

# --- FMP flags ---
FLAG_KEY_EPOCH = 0x01
FLAG_CE = 0x02
FLAG_SP = 0x04

# --- FSP session (from src/node/session_wire.rs) ---
FSP_VERSION = 0
FSP_COMMON_PREFIX_SIZE = 4
FSP_HEADER_SIZE = 12
FSP_INNER_HEADER_SIZE = 6
FSP_ENCRYPTED_MIN_SIZE = 28  # FSP_HEADER_SIZE + TAG_SIZE = 12 + 16
FSP_PORT_HEADER_SIZE = 4
FSP_PORT_IPV6_SHIM = 256

# --- FSP phases ---
FSP_PHASE_ESTABLISHED = 0x00
FSP_PHASE_MSG1 = 0x01
FSP_PHASE_MSG2 = 0x02
FSP_PHASE_MSG3 = 0x03

# --- FSP flags ---
FSP_FLAG_CP = 0x01
FSP_FLAG_K = 0x02
FSP_FLAG_U = 0x04
FSP_INNER_FLAG_SP = 0x01

# --- Link message types (from src/protocol/link.rs) ---
LINK_MSG_SESSION_DATAGRAM = 0x00
LINK_MSG_SENDER_REPORT = 0x01
LINK_MSG_RECEIVER_REPORT = 0x02
LINK_MSG_TREE_ANNOUNCE = 0x10
LINK_MSG_FILTER_ANNOUNCE = 0x20
LINK_MSG_LOOKUP_REQUEST = 0x30
LINK_MSG_LOOKUP_RESPONSE = 0x31
LINK_MSG_HEARTBEAT = 0x51
LINK_MSG_DISCONNECT = 0x50

LINK_MESSAGE_TYPES = {
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

DISCONNECT_REASONS = {
    0x00: "Shutdown",
    0x01: "Restart",
    0x02: "ProtocolError",
    0x03: "TransportFailure",
    0x04: "ResourceExhaustion",
    0x05: "SecurityViolation",
    0x06: "ConfigurationChange",
    0x07: "Timeout",
    0xFF: "Other",
}

# --- MMP report sizes (from src/mmp/mod.rs) ---
SENDER_REPORT_BODY_SIZE = 47
RECEIVER_REPORT_BODY_SIZE = 67
SENDER_REPORT_WIRE_SIZE = 52
RECEIVER_REPORT_WIRE_SIZE = 72
SESSION_SENDER_REPORT_SIZE = 46
SESSION_RECEIVER_REPORT_SIZE = 66

# --- MMP timing (from src/mmp/mod.rs) ---
COLD_START_SAMPLES = 5
DEFAULT_COLD_START_INTERVAL_MS = 200
MIN_REPORT_INTERVAL_MS = 1000
MAX_REPORT_INTERVAL_MS = 5000
DEFAULT_OWD_WINDOW_SIZE = 32
MIN_SESSION_REPORT_INTERVAL_MS = 500
MAX_SESSION_REPORT_INTERVAL_MS = 10000
SESSION_COLD_START_INTERVAL_MS = 1000

# --- MMP algorithm constants ---
SRTT_ALPHA_SHIFT = 3
RTTVAR_BETA_SHIFT = 2
JITTER_ALPHA_SHIFT = 4

# --- Session protocol sizes (from src/protocol/session.rs) ---
SESSION_DATAGRAM_HEADER_SIZE = 36
COORDS_REQUIRED_SIZE = 34
MTU_EXCEEDED_SIZE = 36
PATH_MTU_NOTIFICATION_SIZE = 2

# --- Other ---
WIRE_SIZE = 32
DEFAULT_LOG_INTERVAL_SECS = 30
