#!/usr/bin/env python3
"""pyserial-based console tap for real-UART adapters (FTDI atoms).

raw_tap.py (os.open + termios, built for the S3 USB-JTAG port) loses
live RX on FTDI adapters: it drains the pre-open backlog then goes
silent while pyserial on the same port streams continuously (observed
2026-09-02 during the L2CAP bring-up graduation, artifacts in
results/20260902-133857-l2cap-bringup). Until the termios root cause
is understood, FTDI taps use pyserial.

dtr/rts are forced false BEFORE open to keep the reset circuit quiet;
opening a port with auto-reset wiring (M5 Atom) can still pulse DTR via
the driver and reboot the board — scenarios treat that as a feature:
a deterministic fresh boot with the tap already attached.

Usage: ftdi_tap.py <port> <outfile> <baud>
"""

import sys

import serial

port, outfile, baud = sys.argv[1], sys.argv[2], int(sys.argv[3])
s = serial.Serial()
s.port = port
s.baudrate = baud
s.timeout = 1
s.dtr = False
s.rts = False
s.open()
with open(outfile, "ab", buffering=0) as out:
    while True:
        data = s.read(4096)
        if data:
            out.write(data)
