-- GENERATED from jmcorgan/fips v0.4.0 (d5ee526). Do not hand-edit.
--
-- FIPS Messaging Protocol (FMP) Wireshark Lua dissector.
--
-- Usage:
--   tshark -X lua_script:fips_dissector.lua -f "udp port 2121"
--
-- Or in Wireshark: place this file in the Lua plugins directory, then
-- open a capture on UDP port 2121 or BLE L2CAP PSM 0x0085.
--
-- This dissector parses the FMP common prefix and branches on the phase
-- nibble. It does NOT decrypt — the encrypted payload in ESTABLISHED
-- frames is shown as opaque ciphertext.

-- ===========================================================================
-- Proto definition
-- ===========================================================================

local fips = Proto("fips", "FIPS Messaging Protocol (FMP)")

-- --- Common prefix fields (first 4 bytes of every FMP packet) ---
local pf = {
    -- Byte 0: version (high nibble) + phase (low nibble)
    version     = ProtoField.uint8 ("fmp.version",      "Version",      base.DEC, nil, 0xF0),
    phase       = ProtoField.uint8 ("fmp.phase",        "Phase",        base.HEX,
        {
            [0x0] = "ESTABLISHED (encrypted)",
            [0x1] = "MSG1 (Noise IK initiation)",
            [0x2] = "MSG2 (Noise IK response)",
        }, 0x0F),

    -- Byte 1: flags
    flags       = ProtoField.uint8 ("fmp.flags",        "Flags",        base.HEX),
    flag_key_epoch = ProtoField.bool("fmp.flags.key_epoch", "Key Epoch (K)", 8, nil, 0x01),
    flag_ce        = ProtoField.bool("fmp.flags.ce",        "Congestion Experienced (CE)", 8, nil, 0x02),
    flag_sp        = ProtoField.bool("fmp.flags.sp",        "Spin Bit (SP)", 8, nil, 0x04),

    -- Bytes 2-3: payload length (LE u16)
    payload_len = ProtoField.uint16("fmp.payload_len",  "Payload Length", base.DEC, nil, nil, "littleendian"),

    -- --- ESTABLISHED phase (phase 0x0) ---
    receiver_idx = ProtoField.uint32("fmp.receiver_idx", "Receiver Index", base.HEX, nil, nil, "littleendian"),
    counter      = ProtoField.uint64("fmp.counter",      "Counter",        base.DEC, nil, nil, "littleendian"),
    ciphertext   = ProtoField.bytes ("fmp.ciphertext",   "Encrypted Payload (ciphertext + AEAD tag)"),

    -- --- MSG1 phase (phase 0x1) ---
    msg1_sender_idx  = ProtoField.uint32("fmp.msg1.sender_idx", "Sender Index", base.HEX, nil, nil, "littleendian"),
    msg1_noise       = ProtoField.bytes ("fmp.msg1.noise",      "Noise IK Message 1"),

    -- --- MSG2 phase (phase 0x2) ---
    msg2_sender_idx  = ProtoField.uint32("fmp.msg2.sender_idx", "Sender Index",   base.HEX, nil, nil, "littleendian"),
    msg2_receiver_idx= ProtoField.uint32("fmp.msg2.receiver_idx","Receiver Index", base.HEX, nil, nil, "littleendian"),
    msg2_noise       = ProtoField.bytes ("fmp.msg2.noise",      "Noise IK Message 2"),
}

fips.fields = pf

-- ===========================================================================
-- Phase / flag name lookups for info column
-- ===========================================================================

local phase_names = {
    [0x0] = "ESTABLISHED",
    [0x1] = "MSG1",
    [0x2] = "MSG2",
}

local link_msg_names = {
    [0x00] = "SessionDatagram",
    [0x01] = "SenderReport",
    [0x02] = "ReceiverReport",
    [0x10] = "TreeAnnounce",
    [0x20] = "FilterAnnounce",
    [0x30] = "LookupRequest",
    [0x31] = "LookupResponse",
    [0x50] = "Disconnect",
    [0x51] = "Heartbeat",
}

-- ===========================================================================
-- Constants (mirrors of fips_protocol_types.rs)
-- ===========================================================================

local COMMON_PREFIX_SIZE     = 4
local ESTABLISHED_HEADER_SIZE = 16
local FMP_VERSION            = 0

-- ===========================================================================
-- Dissector function
-- ===========================================================================

function fips.dissector(tvb, pinfo, tree)
    local len = tvb:len()
    if len < COMMON_PREFIX_SIZE then
        return 0  -- too short to be FMP
    end

    pinfo.cols.protocol = "FMP"

    -- --- Parse common prefix ---
    local byte0       = tvb(0, 1):uint()
    local version     = math.floor(byte0 / 16)   -- high nibble (version)
    local phase       = byte0 % 16                -- low nibble (phase)
    local flags       = tvb(1, 1):uint()
    local payload_len = tvb(2, 2):le_uint()

    local phase_name = phase_names[phase] or ("UNKNOWN(0x" .. string.format("%X", phase) .. ")")
    pinfo.cols.info = "FMP " .. phase_name .. " ver=" .. version .. " len=" .. payload_len

    -- --- Add common prefix subtree ---
    local subtree = tree:add(fips, tvb(), "FIPS Messaging Protocol — " .. phase_name)
    local cp = subtree:add(tvb(0, COMMON_PREFIX_SIZE), "Common Prefix")
    cp:add(pf.version,     tvb(0, 1))
    cp:add(pf.phase,       tvb(0, 1))
    cp:add(pf.flags,       tvb(1, 1))
    cp:add(pf.flag_key_epoch, tvb(1, 1))
    cp:add(pf.flag_ce,        tvb(1, 1))
    cp:add(pf.flag_sp,        tvb(1, 1))
    cp:add(pf.payload_len, tvb(2, 2))

    -- --- Branch on phase ---
    if phase == 0x0 then
        -- ESTABLISHED (encrypted frame)
        -- [ver+phase:1][flags:1][payload_len:2][receiver_idx:4 LE][counter:8 LE][ciphertext+tag]
        if len < ESTABLISHED_HEADER_SIZE then
            subtree:add_expert_info(tvb(), "Truncated ESTABLISHED frame (need >= 16 bytes)")
            return len
        end
        local hdr = subtree:add(tvb(0, ESTABLISHED_HEADER_SIZE), "Established Header (16 bytes, used as AAD)")
        hdr:add(pf.receiver_idx, tvb(4, 4))
        hdr:add(pf.counter,      tvb(8, 8))
        if len > ESTABLISHED_HEADER_SIZE then
            hdr:add(pf.ciphertext, tvb(ESTABLISHED_HEADER_SIZE))
        end

    elseif phase == 0x1 then
        -- MSG1 (Noise IK initiation)
        -- [0x01][0x00][payload_len:2][sender_idx:4 LE][noise_msg1:106]
        if len >= 8 then
            local m1 = subtree:add(tvb(4), "MSG1 Fields")
            m1:add(pf.msg1_sender_idx, tvb(4, 4))
            if len > 8 then
                m1:add(pf.msg1_noise, tvb(8))
            end
        end

    elseif phase == 0x2 then
        -- MSG2 (Noise IK response)
        -- [0x02][0x00][payload_len:2][sender_idx:4 LE][receiver_idx:4 LE][noise_msg2:57]
        if len >= 12 then
            local m2 = subtree:add(tvb(4), "MSG2 Fields")
            m2:add(pf.msg2_sender_idx,   tvb(4, 4))
            m2:add(pf.msg2_receiver_idx, tvb(8, 4))
            if len > 12 then
                m2:add(pf.msg2_noise, tvb(12))
            end
        end

    else
        subtree:add(tvb(), "Unknown phase " .. phase_name)
    end

    return len
end

-- ===========================================================================
-- Registration
-- ===========================================================================

-- UDP port 2121 (fips default)
DissectorTable.get("udp.port"):add(2121, fips)

-- BLE L2CAP PSM 0x0085 (fips BLE transport)
-- Wireshark exposes L2CAP PSMs via the "btatt" or "btl2cap.psm" table.
-- For dynamic PSMs, use the btl2cap.psm dissector table.
local l2cap_psm_table = DissectorTable.get("btl2cap.psm")
if l2cap_psm_table then
    l2cap_psm_table:add(0x0085, fips)
end
