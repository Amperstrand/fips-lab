#!/usr/bin/env python3
"""No-reset USB-serial console tap.

pyserial asserts DTR on open, which resets an ESP32-S3 — this uses
os.open + termios directly with no TIOCM touches (bench-testing
playbook pattern #3). Run as a subprocess; it appends raw bytes to
OUTFILE until killed.

Usage: raw_tap.py <port> <outfile>
"""

import os
import select
import sys
import termios

port, outfile = sys.argv[1], sys.argv[2]
fd = os.open(port, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
attrs = termios.tcgetattr(fd)
attrs[0] = 0  # iflag
attrs[1] = 0  # oflag
attrs[2] = termios.CS8  # cflag
attrs[3] = 0  # lflag: raw
attrs[6][termios.VMIN] = 0
attrs[6][termios.VTIME] = 0
termios.tcsetattr(fd, termios.TCSANOW, attrs)

with open(outfile, "ab", buffering=0) as out:
    while True:
        r, _, _ = select.select([fd], [], [], 5)
        if fd not in r:
            continue
        try:
            data = os.read(fd, 4096)
        except BlockingIOError:
            continue
        except OSError:
            break
        if data:
            out.write(data)
