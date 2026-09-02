# Bench Testing Playbook — Amperstrand embedded projects

> One playbook for repeatable, parametrizable hardware testing across
> microfips, fips, and tollgate. Lives here (fips-lab) because this is where
> the bench runs; tollgate-lab carries the shared library, PRta contributed
> the venue/reporting patterns. Cross-linked from each project's AGENTS.md.
>
> Written 2026-09-01 after a fully-interactive microfips hardware session
> (rekey soak, #183) that spent ~25 agent tool-calls and ~20 minutes of
> wall-clock sleeps proving something a 60-line pytest scenario now proves
> on every run. This doc exists so that never has to happen twice.

## The stack

```
pytest scenarios (tests/)          what to assert, parametrized
        ↑
fips_lab drivers + strategy        FIPS-specific: fipsctl, flash, service, serial
        ↑
tollgate-lab                       shared: SSH, ESP32/STM32 flash, hardware lock, reporting
        ↑
labgrid                            resource → driver → strategy abstraction
```

- **tollgate-lab** (`~/src/tollgate-lab`): the library every project imports. If a capability is project-agnostic (flashing, locks, serial capture), it belongs here.
- **fips-lab** (`~/src/fips-lab`): FIPS/microfips drivers + the pytest suite. The pytest env was repaired 2026-08-31 (`148e269`, issue #4 closed); `pytest --collect-only` must stay green — 91 tests at time of writing.
- **PRta** (`~/src/physical-router-test-automation`): the patterns to port, not code: multi-venue runs (physical / cloud / emulation), per-board file locks, `results/<run_id>/` artifact retention, plan docs (`TEST_SETUP.md`, coverage plans).
- **hackathon-tooling**: CI templates + checklists.

## The graduation principle (the token economy)

**Every interactive finding is a scenario that hasn't been written yet.**

| Phase | Who drives | Cost | Lifetime |
|---|---|---|---|
| 1. Hypothesis | Agent, interactive (flash → tap → grep → reason) | High: many tool round-trips, sleeps, log reading | Once per hypothesis |
| 2. Codification | Agent writes the pytest scenario from the session's evidence | One session per scenario | Once |
| 3. Regression | `pytest`, unattended (cron / CI / one command) | ~Zero agent tokens; one result artifact | Forever |

The interactive loop is **not wasted** — it is how new knowledge is found — but
it must never be repeated for the same knowledge. The session that finds a bug
pays for the scenario that guards it. If you find yourself grepping the same
log for the same string twice across sessions, that grep belongs in
`tests/test_issues.py`.

What an automated scenario costs the next agent: **one command, one verdict**.
What today's rekey soak cost interactively: enumerate boards, check daemon,
extract identities, env-pinned build, binary verification, flash, no-reset tap,
~20 min of sleeps, half a dozen greps, timeline reconstruction — then all of it
AGAIN for the fix. Ratio ≈ 50× tokens, ~40× wall-clock, and the automated
version never fat-fingers an env var.

## The patterns (each earned on the bench)

### 1. Board identity: VID:PID + serial, never ttyN
`/dev/ttyACM0` is a lottery. Enumerate by `uevent` PRODUCT + `ID_SERIAL_SHORT`
(the serial IS the MAC on Espressif USB-JTAG). Two same-chip boards differ
only by serial. This belongs in a tollgate-lab inventory fixture.

### 2. Port lifecycle discipline
One owner per port, always. Order matters and the wrong order hangs flashes:
kill the console reader FIRST, then `fuser -k`, then flash. A stuck flash
(timeout on espflash) is almost always a stale reader — check `pgrep`, not
just retries. (fips-lab #3 tracks embedding the raw-open + setsid patterns
into the drivers so this stops being manual.)

### 3. No-reset console tap
pyserial asserts DTR on open → resets an ESP32-S3. Use `os.open` + termios
with no TIOCM touches (`/tmp/opencode/raw_logger.py` pattern), launched via
`setsid` so tool-shell exits don't SIGTERM it. Anchor `pgrep -f` patterns.

### 4. Build-env hygiene + binary verification (the stale-pin trap)
`option_env!` values are invisible to cargo change detection: changing
`DEVICE_NPUB_HEX_*` / `DEVICE_NSEC_HEX_*` / WiFi env silently reuses the old
binary. Discipline:
- `cargo clean -p <crate> --release --target <target>` (without profile+target
  it removes 0 files), then build with the full env set.
- **Verify compiled-in values by scanning the binary**: ASCII grep for the
  SSID, `bytes.fromhex(...)` search for the pinned npub, tail-byte check for
  G·N nsecs. Ten seconds, catches the trap that costs an hour.
A `BuildMatrix` fixture should own this: given (board-identity, peer-npub,
wifi), produce a verified binary path.

### 5. Daemon death: choose goodbye vs silence deliberately
A graceful daemon stop (SIGTERM/systemctl) sends disconnect notifications —
the node sees a clean `PeerDC`, never the silence path. To model gateway
loss (the ESP-NOW failure mode), kill with SIGKILL: no goodbye, and the
node's RX-silence link-dead timeout is the only death signal. Both are
valid scenarios; asserting the wrong one tests the wrong thing (found by
`test_link_death`, 2026-09-01).

### 6. Hardware safety contract: boards.toml (from bolty-rs)
A registry of bench hardware with per-board allowed operations
(`flash`, `observe`) — anything not listed is REFUSED by construction
(`bench.require_board(serial, op)`). This is the port of bolty-rs's
`cards.toml` (per-UID ops: read/burn/wipe) to MCU benches, and it is
what keeps off-limits hardware (the M5 Stack) out of every scenario
without relying on documentation alone. Pair with a **preflight test**
(bolty-rs pattern): a cheap `hardware`-marked scenario asserting the rig
state — boards present, registry entries current — that fails fast
before mutation scenarios waste a flash cycle.

### 7. Daemon-side assertions
The node console is half the evidence; the daemon log + `fipsctl` are the
other half. Note that rekey is *silent* at INFO on the daemon — its absence
signature (e.g. the SecurityViolation disconnect cycle) is what a scenario
must assert on. `LabFipsServiceDriver` (microfips AGENTS Phase 1) owns
isolated config/port/identity per the security checklist there.

### 8. Artifacts: `results/<run_id>/` (PRta pattern)
Console capture, daemon log slice, verdict JSON, env/build hashes. An
assertion that can't be re-examined after the run is a rumor.

### 9. Parametrization
The whole point of fixtures: the same scenario over a matrix —
`board ∈ {s3-lab, cyd, atom-a}`, `wire ∈ {ik, xx}`, `rekey_after_secs ∈ {5, 120}`
(a fast lab value! the daemon config is ours) — is one
`@pytest.mark.parametrize` away once the build matrix exists. Interactive
testing cannot afford a matrix; automated testing can't afford not to.

## Scenario backlog (microfips bench, in priority order)

1. **`test_rekey_soak`** (NEW, from 2026-09-01 / microfips #183): flash pinned
   WiFi firmware → handshake → survive ≥2 daemon rekey cycles → assert node
   console shows `rekey msg1 received` ≥2, `cutover complete` ≥2,
   `drain complete` ≥2, zero session rebuilds; daemon log shows zero
   `disconnect notification` in the window. Parametrize `rekey_after_secs`
   down to 5s so a full soak is <60s. Full spec: fips-lab issue (see backlog).
2. `test_link_death` — bridge kill → RX-silence timeout → re-discovery.
3. `test_mdns_discovery` — pinned + open modes, advert tamper rejection.
4. `test_espnow_gw`, `test_hybrid_switch` — AGENTS Phase 2 list.
5. Existing BLE scenarios (bench-era) keep running under the same fixtures.

## Coordinator topology (locked down 2026-09-02)

`labgrid-coordinator` on the workstation binds **192.168.13.221:20408** only —
not localhost, not 0.0.0.0. Rationale: localhost would break the planned
multi-machine bench (ai-legion, ai-legion-small, mac laptop registering
hardware), while 0.0.0.0 exposed the coordinator to every interface
(enp5s0 cloud-LAB subnet, docker bridges). Lab clients set
`LG_COORDINATOR=192.168.13.221:20408` (in ~/.bashrc on the workstation).
Future machines reach the coordinator over the lab LAN; the exporter on
each machine publishes its local hardware to it. Verified: localhost
refused, cloud-LAB interface refused, lab interface serves.

## Cross-pollination ledger

| Repo | Contributes | Should adopt |
|---|---|---|
| microfips | The bench inventory + security checklist; wire-format lessons | This playbook's graduation discipline |
| fips-lab | Drivers, pytest suite, results publishing (tests.tollgate.me) | microfips bench targets + WiFi-path scenarios |
| tollgate-lab | Shared flash/lock/serial library | The no-reset tap + binary-verification patterns (via fips-lab #3) |
| PRta | Venues, locks, `results/<run_id>/`, plan-doc culture | The graduation principle for its backend-matrix tests |
| hackathon-tooling | CI templates | Bench-nightly job once scenarios exist |
