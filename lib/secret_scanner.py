#!/usr/bin/env python3
"""
Secret Scanner -- 3-layer detection and redaction for public publishing.

Ensures that private keys, API tokens, cloud credentials, router passwords, and
other secrets never appear in any artifact uploaded to Blossom or published to
Nostr.

Three layers:
  1. Blocklist  -- files that must NEVER be published (config, .env, keys, etc.)
  2. Regex      -- known secret formats (nsec1, GCP keys, Cashu, GitHub, Hetzner)
  3. Context-aware hex detection -- 64-char hex strings near key keywords + entropy

Additional patterns for infrastructure automation contexts:
  - GCP service account JSON (private_key fields)
  - Hetzner cloud tokens (HCLOUD_TOKEN / HETZNER_TOKEN)
  - SSH passwords (sshpass -p, SSH_PASSWORD=)
  - Hardcoded router/network device passwords
  - ADB serial numbers (warning level, not block)
  - IP addresses with credentials (root@192.168.x.x)

Project-agnostic -- no imports from other lib modules. Designed to be lifted
into hackathon-tooling or any CI pipeline.

Usage:
    from secret_scanner import scan_file, scan_directory, verify_clean

    sanitized, findings = scan_file("results/report.txt")
    if sanitized is None:
        print("BLOCKED: file must not be published")
    elif findings:
        print(f"WARN: {len(findings)} secrets redacted")
"""

import json
import math
import os
import re
import sys
from typing import Tuple

# ===========================================================================
# Layer 1: Files that must NEVER be published
# ===========================================================================

NEVER_PUBLISH_EXACT = {
    # Config / env
    "opencode.json",
    "config.json",
    "config.local.json",
    ".env",
    ".env.local",
    ".env.production",
    # Secret stores
    "secrets",
    "secrets.json",
    "secrets.yaml",
    "bot_nsec",
    "bot_nsec.txt",
    "nsec.txt",
    "nsec",
    # SSH keys
    "id_rsa",
    "id_ed25519",
    # Package manager creds
    ".npmrc",
    ".pypirc",
    # Infra
    "cloud-init.yaml",
    "blossomfs.toml",
    # GCP / cloud service account keys (full JSON)
    "service_account.json",
    "gcp-key.json",
    "credentials.json",
}

NEVER_PUBLISH_SUFFIXES = (
    ".key",
    ".pem",
    ".p12",
    ".pfx",
    ".keystore",
    ".kube",
)

NEVER_PUBLISH_SUBSTRINGS = (
    "nsec",
    "secret",
    "credential",
    "api-key",
    "apikey",
    "token",
)

# Files that are allowed despite having "secret" in the name (like this module)
_ALLOWLIST_SECRET_NAMES = {"secret_scanner.py"}

# ===========================================================================
# Layer 2: Regex patterns for known secret formats
# ===========================================================================

# Each entry: (compiled_regex, name)
# Matches are replaced with [REDACTED:<name>] in the sanitized output.
REGEX_PATTERNS = [
    # Nostr private key (bech32)
    (
        re.compile(r"nsec1[023456789acdefghjklmnpqrstuvwxyz]{58}"),
        "nostr-nsec-bech32",
    ),
    # Nostr private key (hex) assigned to a variable
    (
        re.compile(
            r"(?:NOSTR_SECRET_KEY|BOT_NSEC|nsec_hex)\s*[=:]\s*['\"]?"
            r"([a-f0-9]{64})['\"]?"
        ),
        "nostr-nsec-hex",
    ),
    # z.ai / GLM API keys
    (
        re.compile(r"[a-f0-9]{32}\.[A-Za-z0-9]{16}"),
        "zai-api-key",
    ),
    # Cashu tokens
    (
        re.compile(r"cashu[AB][a-zA-Z0-9+/=]{20,}"),
        "cashu-token",
    ),
    # GitHub tokens
    (
        re.compile(r"\b(gho_|ghp_|github_pat_)[A-Za-z0-9_]{36,}\b"),
        "github-token",
    ),
    # OpenAI / Anthropic keys
    (
        re.compile(r"\bsk-[a-zA-Z0-9]{40,}\b"),
        "openai-key",
    ),
    # PEM private keys (GCP service account, TLS certs, SSH keys)
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        "pem-private-key",
    ),
    # Hetzner cloud tokens (64-char alphanumeric)
    (
        re.compile(r"\b(HCLOUD_TOKEN|HETZNER_TOKEN|HETZNER_API_TOKEN)\s*[=:]\s*['\"]?([A-Za-z0-9]{64})['\"]?"),
        "hetzner-token",
    ),
    # Generic API key assignments (ZAI, OPENCODE, etc.)
    (
        re.compile(
            r"\b(ZAI_API_KEY|Z_AI_API_KEY|OPENCODE_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY)"
            r"\s*[=:]\s*['\"]?([A-Za-z0-9._\-]{20,})['\"]?"
        ),
        "api-key-assignment",
    ),
    # SSH password via sshpass
    (
        re.compile(r"sshpass\s+-p\s+['\"]([^'\"]{3,})['\"]"),
        "ssh-password-sshpass",
    ),
    # SSH_PASSWORD / SSH_PASS variable assignment
    (
        re.compile(r"\b(SSH_PASSWORD|SSH_PASS|SSH_USER_PASSWORD)\s*[=:]\s*['\"]([^'\"]{3,})['\"]"),
        "ssh-password-var",
    ),
    # GCP service account private key block (full PEM)
    (
        re.compile(
            r"(\"private_key\"\s*:\s*\")"
            r"(-----BEGIN (?:RSA )?PRIVATE KEY-----[A-Za-z0-9+/=\s]+-----END (?:RSA )?PRIVATE KEY-----)"
            r"(\")"
        ),
        "gcp-private-key-pem",
    ),
    # Hardcoded tollgate/router passwords (standalone literal, e.g. tollgate123)
    (
        re.compile(r"\btollgate\d+\b"),
        "hardcoded-router-password",
    ),
    # Router / device password assignments: "router_password: secret" or "password = 'xyz'"
    # (quotes optional to catch unquoted shell/config values)
    (
        re.compile(
            r"\b(?:router|device|switch|gateway)[._-]?(?:password|passwd|pwd)\s*[=:]\s*"
            r"['\"]?([^'\"\s]{4,})['\"]?",
            re.IGNORECASE,
        ),
        "router-password",
    ),
    # Generic password assignment with a hardcoded value (quoted or unquoted)
    (
        re.compile(
            r"\b(?:password|passwd|pwd)\s*[=:]\s*['\"]([^'\"]{4,})['\"]",
            re.IGNORECASE,
        ),
        "hardcoded-password",
    ),
    # Router config CLI: "set system login password <value>"
    (
        re.compile(r"(?:password|passwd)\s+(tollgate\d+|[A-Za-z0-9!@#$%^&*]{8,})", re.IGNORECASE),
        "device-password-config",
    ),
]

# ===========================================================================
# Layer 3: Context-aware hex private key detection + entropy
# ===========================================================================

# Hex value near a key-related keyword
HEX_KEY_CONTEXT = re.compile(
    r"(?:nsec|private[_\s-]?key|secret[_\s-]?key|NOSTR_SECRET_KEY|BOT_NSEC|"
    r"signing[_\s-]?key|auth[_\s-]?key)\s*[:=]\s*"
    r"([a-f0-9]{64})",
    re.IGNORECASE,
)

# Bare 64-char hex that's NOT adjacent to a hex char
BARE_HEX_64 = re.compile(r"(?<![0-9a-fA-F])([a-f0-9]{64})(?![0-9a-fA-F])")

# Contexts where a 64-char hex is expected (sha256, commit hashes, etc.)
SAFE_HEX_CONTEXTS = re.compile(
    r"(?:sha[_\s-]?256|hash|checksum|digest|etag|blob[_\s-]?id|content[_\s-]?address|"
    r"commit|tree|git|x-ref|refer|event[_\s-]?id|note[_\s-]?id)",
    re.IGNORECASE,
)

# --- ADB serial numbers (warning level, NOT redacted) ---

ADB_SERIAL = re.compile(r"\b([0-9a-fA-F]{16})\b")

# IP address with username (root@192.168.x.x)
IP_WITH_CRED = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9._-]*)@(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b"
)


# --- Entropy analysis ---


def shannon_entropy(data: str) -> float:
    """Shannon entropy in bits per character."""
    if not data:
        return 0.0
    freq = {}
    for c in data:
        freq[c] = freq.get(c, 0) + 1
    n = len(data)
    return -sum((f / n) * math.log2(f / n) for f in freq.values())


def is_high_entropy_hex(s: str, threshold: float = 3.2) -> bool:
    """True if a hex string is long enough and has high entropy (likely a key)."""
    if len(s) < 32:
        return False
    return shannon_entropy(s) >= threshold


# ===========================================================================
# Core scanning functions
# ===========================================================================


def is_blocked_file(file_path: str) -> bool:
    """Check if a file should NEVER be published based on its name.

    Blocks: exact names (config.json, .env), suffixes (.pem, .key), and
    suspicious substrings (nsec, token) -- with an allowlist for this module.
    """
    basename = os.path.basename(file_path)
    if basename in NEVER_PUBLISH_EXACT:
        return True
    if basename.endswith(NEVER_PUBLISH_SUFFIXES):
        return True
    lower = basename.lower()
    for substr in NEVER_PUBLISH_SUBSTRINGS:
        if substr in lower and not basename.endswith((".py", ".js", ".html", ".css", ".md")):
            if basename not in _ALLOWLIST_SECRET_NAMES:
                return True
    return False


def scan_content(content: str) -> tuple[str, list]:
    """Scan content for secrets. Returns (sanitized_content, findings_list).

    findings is a list of dicts: {"type": str, "preview": str, "pos": int}
    Redacted secrets are replaced with [REDACTED:<type>] in the output.
    """
    sanitized = content
    findings = []

    # --- Layer 2: Regex patterns ---
    for pattern, name in REGEX_PATTERNS:
        for match in pattern.finditer(sanitized):
            preview_match = match.group(2) if match.lastindex and match.lastindex >= 2 else match.group()
            findings.append({
                "type": name,
                "preview": str(preview_match)[:12] + "...",
                "pos": match.start(),
            })
            sanitized = (
                sanitized[: match.start()]
                + f"[REDACTED:{name}]"
                + sanitized[match.end():]
            )

    # --- Layer 3a: Hex private key in key-context ---
    for match in HEX_KEY_CONTEXT.finditer(sanitized):
        hex_val = match.group(1)
        findings.append({
            "type": "hex-privkey-in-context",
            "preview": hex_val[:12] + "...",
            "pos": match.start(1),
        })
        sanitized = (
            sanitized[: match.start(1)]
            + "[REDACTED:hex-key]"
            + sanitized[match.end(1):]
        )

    # --- Layer 3b: Bare 64-char hex with entropy + context check ---
    start = 0
    while True:
        match = BARE_HEX_64.search(sanitized, start)
        if not match:
            break
        hex_val = match.group(1)
        context_before = sanitized[max(0, match.start() - 80): match.start()]
        if SAFE_HEX_CONTEXTS.search(context_before):
            start = match.end()
            continue
        if is_high_entropy_hex(hex_val):
            findings.append({
                "type": "suspicious-bare-hex",
                "preview": hex_val[:12] + "...",
                "pos": match.start(),
            })
            sanitized = (
                sanitized[: match.start()]
                + "[REDACTED:hex]"
                + sanitized[match.end():]
            )
        start = match.end()

    # --- Warning-level: ADB serial numbers (not redacted, just flagged) ---
    for match in ADB_SERIAL.finditer(sanitized):
        context_after = sanitized[match.end(): match.end() + 30].lower()
        if "adb" in context_after or "serial" in context_after or "device" in context_after:
            findings.append({
                "type": "adb-serial-warning",
                "preview": match.group(1),
                "pos": match.start(),
                "level": "warning",
            })

    # --- Warning-level: IP addresses with credentials ---
    for match in IP_WITH_CRED.finditer(sanitized):
        findings.append({
            "type": "ip-with-credential",
            "preview": f"{match.group(1)}@{match.group(2)}",
            "pos": match.start(),
            "level": "warning",
        })

    return sanitized, findings


def scan_file(file_path: str) -> tuple[str | None, list]:
    """Scan a single file for secrets.

    Returns:
        (sanitized_content, findings) -- content is None if file is blocked.
        findings is a list of dicts with type/preview/pos.
    """
    if is_blocked_file(file_path):
        return None, [{"type": "blocked-file", "filename": os.path.basename(file_path)}]

    try:
        with open(file_path, errors="replace") as f:
            content = f.read()
    except Exception as e:
        return None, [{"type": "read-error", "filename": file_path, "error": str(e)}]

    if file_path.endswith((".json", ".xml")):
        return content, []

    sanitized, findings = scan_content(content)
    return sanitized, findings


def scan_directory(dir_path: str, skip_dirs: set = None) -> dict:
    """Scan all files in a directory tree.

    Args:
        dir_path: Root directory to scan.
        skip_dirs: Directory names to skip (default: .git, __pycache__, etc.)

    Returns:
        {
            "scanned": int,
            "blocked": [file_paths],
            "clean": [file_paths],
            "redacted": [{filename, count, findings}],
            "errors": [{filename, error}],
        }
    """
    if skip_dirs is None:
        skip_dirs = {".git", "__pycache__", ".omo", "node_modules", ".cache", ".venv", "venv"}

    result = {
        "scanned": 0,
        "blocked": [],
        "clean": [],
        "redacted": [],
        "errors": [],
    }

    for root, dirs, files in os.walk(dir_path):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in files:
            fpath = os.path.join(root, fname)
            result["scanned"] += 1

            sanitized, findings = scan_file(fpath)

            if sanitized is None:
                if findings and findings[0]["type"] == "blocked-file":
                    result["blocked"].append(fpath)
                else:
                    result["errors"].append({
                        "filename": fpath,
                        "error": findings[0].get("error", "unknown") if findings else "unknown",
                    })
            elif findings:
                result["redacted"].append({
                    "filename": fpath,
                    "count": len(findings),
                    "findings": [{"type": f["type"], "preview": f.get("preview", "?")} for f in findings],
                })
            else:
                result["clean"].append(fpath)

    return result


def verify_clean(file_path: str) -> bool:
    """Verify a file is safe to publish. Returns True if no secrets detected."""
    sanitized, findings = scan_file(file_path)
    return sanitized is not None and len(findings) == 0


def verify_content_clean(content: str) -> bool:
    """Verify content string is safe to publish. Returns True if clean."""
    _, findings = scan_content(content)
    return len(findings) == 0


# --- CLI entry point ---

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python secret_scanner.py <file_or_directory>")
        print("       python secret_scanner.py --verify <file>")
        sys.exit(1)

    cli_target = sys.argv[1]

    if cli_target == "--verify" and len(sys.argv) > 2:
        cli_target = sys.argv[2]
        if os.path.isdir(cli_target):
            res = scan_directory(cli_target)
            print(json.dumps(res, indent=2))
            sys.exit(0 if not res["blocked"] and not res["redacted"] else 1)
        else:
            ok = verify_clean(cli_target)
            print(f"{'CLEAN' if ok else 'SECRETS FOUND'}: {cli_target}")
            sys.exit(0 if ok else 1)

    if os.path.isdir(cli_target):
        res = scan_directory(cli_target)
        print(json.dumps(res, indent=2))
    else:
        sanitized, findings = scan_file(cli_target)
        if sanitized is None:
            print(f"BLOCKED: {cli_target}")
        elif findings:
            print(f"REDACTED {len(findings)} secrets in {cli_target}:")
            for f in findings:
                level = f.get("level", "block")
                print(f"  [{f['type']}:{level}] {f.get('preview', '?')}")
        else:
            print(f"CLEAN: {cli_target}")
