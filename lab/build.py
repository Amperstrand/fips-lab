"""Build manager — checkout and build FIPS binaries on test devices."""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .device import Device

log = logging.getLogger(__name__)

_BUILD_TIMEOUT_SECS = 600  # 10 minutes per device


@dataclass
class BuildResult:
    """Result of a build attempt on a single device."""

    success: bool
    commit: str
    duration_secs: float
    error: str | None = None


class BuildManager:
    """Manages FIPS source checkout and release builds on test devices.

    For each FIPS device in the inventory, derives the repo path, checks out
    the requested commit, and runs ``cargo build --release``.
    """

    def __init__(
        self,
        devices: dict[str, Device],
        configs: dict[str, dict[str, Any]],
        results_dir: Path,
        timeout_secs: int = _BUILD_TIMEOUT_SECS,
    ):
        self._devices = devices
        self._configs = configs
        self._results_dir = results_dir
        self._timeout_secs = timeout_secs

    def build_all(
        self,
        commit: str,
        features: dict[str, str] | None = None,
    ) -> dict[str, BuildResult]:
        """Checkout and build FIPS on all FIPS devices.

        Args:
            commit: git commit/branch/tag to checkout.
            features: optional per-device feature overrides (maps alias to
                ``--features`` value).

        Returns:
            dict mapping device alias to BuildResult.
        """
        targets = self._fips_devices()
        if not targets:
            log.warning("No FIPS devices to build")
            return {}

        results: dict[str, BuildResult] = {}
        for alias, (_device, cfg) in targets.items():
            device_features = (features or {}).get(alias) or cfg.get("build_features", "")
            results[alias] = self.build_device(alias, cfg, commit, device_features)

        return results

    def build_device(
        self,
        alias: str,
        cfg: dict[str, Any],
        commit: str,
        features: str = "",
    ) -> BuildResult:
        """Build on a single device.

        Args:
            alias: device alias (e.g. ``macbook-local``).
            cfg: resolved device config dict.
            commit: git ref to checkout.
            features: cargo ``--features`` value (empty = default features).

        Returns:
            BuildResult with success status and resolved commit.
        """
        transport = cfg.get("transport", "local")
        repo_path = self._resolve_repo_path(cfg)
        if not repo_path:
            return BuildResult(
                success=False,
                commit="",
                duration_secs=0,
                error=f"No repo_path for {alias} and cannot derive from fips_binary",
            )

        start = time.time()
        try:
            if transport == "local":
                resolved_commit = self._build_local(alias, repo_path, commit, features)
            elif transport == "ssh":
                resolved_commit = self._build_ssh(alias, cfg, repo_path, commit, features)
            else:
                return BuildResult(
                    success=False,
                    commit="",
                    duration_secs=0,
                    error=f"Unsupported transport {transport} for {alias}",
                )
            elapsed = time.time() - start
            log.info(
                "Build succeeded on %s: commit=%s duration=%.1fs",
                alias, resolved_commit, elapsed,
            )
            return BuildResult(success=True, commit=resolved_commit, duration_secs=elapsed)

        except BuildError as exc:
            elapsed = time.time() - start
            log.error("Build failed on %s: %s", alias, exc)
            return BuildResult(
                success=False,
                commit="",
                duration_secs=elapsed,
                error=str(exc),
            )

    def _fips_devices(self) -> dict[str, tuple[Device, dict[str, Any]]]:
        out: dict[str, tuple[Device, dict[str, Any]]] = {}
        for alias, device in self._devices.items():
            cfg = self._configs.get(alias, {})
            if cfg.get("fips_binary") and cfg.get("type") == "fips":
                out[alias] = (device, cfg)
        return out

    def _build_local(
        self,
        alias: str,
        repo_path: str,
        commit: str,
        features: str,
    ) -> str:
        """Checkout and build on a local device. Returns resolved commit hash."""
        self._git_checkout_local(alias, repo_path, commit)
        resolved = self._git_resolve_commit_local(alias, repo_path)
        self._cargo_build_local(alias, repo_path, features)
        return resolved

    def _build_ssh(
        self,
        alias: str,
        cfg: dict[str, Any],
        repo_path: str,
        commit: str,
        features: str,
    ) -> str:
        """Checkout and build on an SSH device. Returns resolved commit hash."""
        host = cfg.get("host") or cfg.get("ssh_host")
        user = cfg.get("user") or cfg.get("ssh_user")
        target = f"{user}@{host}" if user else str(host)

        self._git_checkout_ssh(alias, target, repo_path, commit)
        resolved = self._git_resolve_commit_ssh(alias, target, repo_path)
        self._cargo_build_ssh(alias, target, repo_path, features, cfg)
        return resolved

    # ── Local operations ──────────────────────────────────────────────────

    def _git_checkout_local(self, alias: str, repo_path: str, commit: str) -> None:
        log.info("Checking out %s on %s (local): git checkout %s", commit, alias, commit)
        result = subprocess.run(
            ["git", "checkout", commit],
            cwd=repo_path,
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode != 0:
            raise BuildError(
                f"git checkout {commit} on {alias} failed (exit {result.returncode}): "
                f"{result.stderr.strip()}"
            )

    def _git_resolve_commit_local(self, alias: str, repo_path: str) -> str:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode != 0:
            raise BuildError(f"Cannot resolve commit on {alias}: {result.stderr.strip()}")
        return result.stdout.strip()

    def _cargo_build_local(self, alias: str, repo_path: str, features: str) -> None:
        cmd = ["cargo", "build", "--release"]
        if features:
            cmd.extend(["--features", features])
        log.info("Building on %s (local): %s", alias, " ".join(cmd))
        result = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True, text=True, timeout=self._timeout_secs, check=False,
        )
        if result.returncode != 0:
            stderr_tail = result.stderr.strip().splitlines()[-5:]
            raise BuildError(
                f"cargo build exited with code {result.returncode} on {alias}:\n"
                + "\n".join(stderr_tail)
            )

    # ── SSH operations ────────────────────────────────────────────────────

    def _git_checkout_ssh(
        self, alias: str, target: str, repo_path: str, commit: str,
    ) -> None:
        log.info("Checking out %s on %s (ssh %s)", commit, alias, target)
        # Fetch first so remote branches/tags are available
        fetch_cmd = f"cd {repo_path} && git fetch origin"
        result = subprocess.run(
            ["ssh", target, fetch_cmd],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if result.returncode != 0:
            log.warning(
                "git fetch on %s failed (exit %d): %s — continuing with checkout",
                alias, result.returncode, result.stderr.strip(),
            )

        checkout_cmd = f"cd {repo_path} && git checkout {commit}"
        result = subprocess.run(
            ["ssh", target, checkout_cmd],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode != 0:
            raise BuildError(
                f"git checkout {commit} on {alias} failed (exit {result.returncode}): "
                f"{result.stderr.strip()}"
            )

    def _git_resolve_commit_ssh(
        self, alias: str, target: str, repo_path: str,
    ) -> str:
        cmd = f"cd {repo_path} && git rev-parse HEAD"
        result = subprocess.run(
            ["ssh", target, cmd],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode != 0:
            raise BuildError(f"Cannot resolve commit on {alias}: {result.stderr.strip()}")
        return result.stdout.strip()

    def _cargo_build_ssh(
        self,
        alias: str,
        target: str,
        repo_path: str,
        features: str,
        cfg: dict[str, Any],
    ) -> None:
        build_env = cfg.get("build_env", "")
        cargo_cmd = "cargo build --release"
        if features:
            cargo_cmd += f" --features {features}"
        if build_env:
            remote_cmd = f"cd {repo_path} && {build_env} && {cargo_cmd}"
        else:
            remote_cmd = f"cd {repo_path} && {cargo_cmd}"
        log.info("Building on %s (ssh %s): %s", alias, target, cargo_cmd)
        result = subprocess.run(
            ["ssh", target, remote_cmd],
            capture_output=True, text=True, timeout=self._timeout_secs, check=False,
        )
        if result.returncode != 0:
            stderr_tail = result.stderr.strip().splitlines()[-5:]
            raise BuildError(
                f"cargo build exited with code {result.returncode} on {alias}:\n"
                + "\n".join(stderr_tail)
            )

    # ── Repo path derivation ──────────────────────────────────────────────

    @staticmethod
    def _resolve_repo_path(cfg: dict[str, Any]) -> str | None:
        """Derive the FIPS source repo path from device config.

        Priority:
        1. Explicit ``repo_path`` field.
        2. Derived from ``fips_binary`` by stripping ``/target/release/fips``.
        """
        explicit = cfg.get("repo_path")
        if explicit:
            return str(explicit)
        fips_binary = cfg.get("fips_binary")
        if fips_binary:
            path = Path(str(fips_binary))
            # fips_binary = /path/to/repo/target/release/fips
            # repo = path.parent.parent.parent
            return str(path.parent.parent.parent)
        return None


class BuildError(Exception):
    """Raised when a build step fails on a device."""
