"""Deliberate adversarial events for bench scenarios — the unknown-unknowns
elevator (playbook: chaos operators are hypothesis generators; once a fault
is found, it graduates into a named scenario).

Frame storms target BOTH protocol endpoints:
- daemon-side: the fips daemon's UDP input path (it must reject, not crash);
- node-side: OUR firmware's input path (microfips #77 DoS-hardening class).

Every storm is rate-limited by design — this is a correctness probe, not a
bandwidth DoS (a volume test would need its own assertions and safety).
"""

from __future__ import annotations

import random
import socket
import struct
import threading
import time
from dataclasses import dataclass, field


@dataclass
class StormStats:
    sent: int = 0
    by_class: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"sent": self.sent, "by_class": dict(self.by_class)}


def _frame_classes(rng: random.Random) -> dict[str, bytes]:
    """One candidate frame per malformation class, FMP-shaped where noted."""
    junk = bytes(rng.getrandbits(8) for _ in range(rng.randint(1, 200)))
    bad_version = bytes([0xF0 | rng.randint(0, 3)]) + junk[:64]
    msg1_shaped = bytes([0x01, 0x00]) + bytes(
        rng.getrandbits(8) for _ in range(112)
    )
    oversized = bytes(rng.getrandbits(8) for _ in range(2100))
    return {
        "junk": junk,
        "bad_version": bad_version,
        "msg1_shaped": msg1_shaped,
        "oversized": oversized,
        "empty": b"",
    }


class FrameStorm:
    """Background thread sending malformed frames to one UDP endpoint."""

    def __init__(self, host: str, port: int, rate_hz: float = 12.0, seed: int = 7):
        self.target = (host, port)
        self.rate_hz = rate_hz
        self.rng = random.Random(seed)
        self.stats = StormStats()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "FrameStorm":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        interval = 1.0 / max(self.rate_hz, 0.1)
        while not self._stop.is_set():
            frames = _frame_classes(self.rng)
            name = self.rng.choice(list(frames))
            try:
                sock.sendto(frames[name], self.target)
                self.stats.sent += 1
                self.stats.by_class[name] = self.stats.by_class.get(name, 0) + 1
            except OSError:
                self.stats.by_class.setdefault(f"errors_{name}", 0)
                self.stats.by_class[f"errors_{name}"] += 1
            self._stop.wait(interval)
        sock.close()

    def stop(self) -> StormStats:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        return self.stats
