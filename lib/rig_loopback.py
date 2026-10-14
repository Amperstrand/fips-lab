#!/usr/bin/env python3
"""Run the two-daemon loopback rig end to end against the pinned lab daemon.

One command, no boards required: generates fresh identities, writes node
configs, runs two daemon instances on loopback UDP with FIPS_LAB_KEYLOG and
tcpdump, drives a probe (IK link + XK session), soaks for rekeys, tears
down cleanly, then runs lib.fips_decrypt over the capture and writes a
RESULTS.md into a dated run directory. Exit is non-zero if any layer's
decrypt ratio drops below --min-ratio.

Usage:
    python3 -m lib.rig_loopback [--secs 300] [--min-ratio 0.99]
    python3 -m lib.rig_loopback --daemon-dir /path/to/release [--out-base DIR]

The daemon defaults to the pinned bench worktree
(/home/ubuntu/bench/fips-daemon, DAEMON_BUILD records its commit).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

DEFAULT_DAEMON_DIR = Path("/home/ubuntu/bench/fips-daemon")
PORT_A, PORT_B = 23121, 23122


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--secs", type=int, default=300)
    ap.add_argument("--min-ratio", type=float, default=0.99)
    ap.add_argument("--daemon-dir", type=Path, default=DEFAULT_DAEMON_DIR)
    ap.add_argument("--out-base", type=Path, default=Path("/home/ubuntu/bench"))
    args = ap.parse_args()

    fips = args.daemon_dir / "target/release/fips"
    fipsctl = args.daemon_dir / "target/release/fipsctl"
    missing = [str(p) for p in (fips, fipsctl) if not p.is_file()]
    if missing:
        raise SystemExit(f"daemon binaries missing: {missing}")
    build_note = (args.daemon_dir / "DAEMON_BUILD").read_text() if (args.daemon_dir / "DAEMON_BUILD").is_file() else "(no DAEMON_BUILD)"

    run_dir = args.out_base / f"rig-loopback-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    (run_dir / "node-a").mkdir(parents=True)
    (run_dir / "node-b").mkdir(parents=True)
    keylog = run_dir / "keylog.jsonl"
    pcap = run_dir / "loopback.pcap"

    def gen_keys() -> tuple[str, str]:
        out = subprocess.run(
            [str(fipsctl), "keygen", "-s"], check=True, capture_output=True, text=True
        ).stdout.strip().splitlines()
        nsec = next(line for line in out if line.startswith("nsec1"))
        npub = next(line for line in out if line.startswith("npub1"))
        return nsec, npub

    nsec_a, npub_a = gen_keys()
    nsec_b, npub_b = gen_keys()

    def config(nsec: str, own_port: int, peer_npub: str, peer_port: int, ble: bool) -> str:
        ble_block = (
            "  ble:\n    adapter: \"hci0\"\n    advertise: true\n    scan: true\n"
            "    auto_connect: true\n    accept_connections: true\n" if ble else ""
        )
        return f"""node:
  identity:
    nsec: "{nsec}"
  control:
    socket_path: {run_dir}/{'node-a' if ble else 'node-b'}/control.sock
  rekey:
    after_secs: 25
    after_messages: 10000000
tun:
  enabled: false
dns:
  enabled: false
transports:
  udp:
    bind_addr: "127.0.0.1:{own_port}"
{ble_block}peers:
  - npub: "{peer_npub}"
    alias: "peer"
    addresses:
      - transport: udp
        addr: "127.0.0.1:{peer_port}"
    connect_policy: auto_connect
"""

    (run_dir / "node-a/fips.yaml").write_text(config(nsec_a, PORT_A, npub_b, PORT_B, ble=True))
    (run_dir / "node-b/fips.yaml").write_text(config(nsec_b, PORT_B, npub_a, PORT_A, ble=False))

    env = dict(os.environ, FIPS_LAB_KEYLOG=str(keylog))
    procs = []
    cap = subprocess.Popen(
        ["sudo", "-n", "tcpdump", "-i", "lo", "-s", "0", "-w", str(pcap),
         f"udp port {PORT_A} or udp port {PORT_B}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for node in ("node-a", "node-b"):
        log = open(run_dir / node / "fips.log", "wb")
        procs.append(subprocess.Popen(
            [str(fips), "-c", str(run_dir / node / "fips.yaml")],
            env=env, stdout=log, stderr=subprocess.STDOUT,
        ))

    def shutdown():
        for p in procs:
            p.send_signal(signal.SIGINT)
        for p in procs:
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                p.kill()
        cap.send_signal(signal.SIGINT)
        cap.wait(timeout=10)

    try:
        deadline = time.time() + 30
        sock_a = run_dir / "node-a/control.sock"
        while time.time() < deadline and not sock_a.exists():
            time.sleep(0.5)
        if not sock_a.exists():
            raise SystemExit("node-a control socket never appeared")
        probe = subprocess.run(
            [str(fipsctl), "-s", str(sock_a), "probe", npub_b],
            capture_output=True, text=True, timeout=120,
        )
        print(probe.stdout)
        print(f"soaking {args.secs}s (rekey every 25s)...")
        time.sleep(args.secs)
    finally:
        shutdown()

    from lib import fips_decrypt

    summary = json.loads(subprocess.run(
        [sys.executable, "-m", "lib.fips_decrypt", "--pcap", str(pcap),
         "--keylog", str(keylog), "--json"],
        check=True, capture_output=True, text=True,
    ).stdout)
    decrypt_ratio = summary["decrypt_ratio"]
    fsp_est = summary["stats"].get("fsp_established", 0)
    fsp_dec = summary["stats"].get("fsp_decrypted", 0)
    fsp_ratio = (fsp_dec / fsp_est) if fsp_est else None
    fsp_line = f"{fsp_dec}/{fsp_est} = {fsp_ratio:.1%}" if fsp_ratio is not None else "n/a"

    results = run_dir / "RESULTS.md"
    results.write_text(
        f"# Loopback rig run {run_dir.name}\n\n"
        f"daemon: {build_note.strip()}\nsoak: {args.secs}s, rekey 25s\n\n"
        f"- key generations: {summary['key_generations']} {summary['generations_by_pattern']}\n"
        f"- link decrypt: {summary['stats'].get('decrypted', 0)}/"
        f"{summary['stats'].get('established', 0)} = {decrypt_ratio:.1%}\n"
        f"- session decrypt: {fsp_line}\n"
        f"- message types: {summary['msg_types']}\n"
        f"- fsp message types: {summary['fsp_msg_types']}\n"
    )
    print(results)
    ok = decrypt_ratio is not None and decrypt_ratio >= args.min_ratio
    if fsp_ratio is not None and fsp_ratio < args.min_ratio:
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
