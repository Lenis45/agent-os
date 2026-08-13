#!/usr/bin/env python3
"""Non-destructive security posture checks for the local Amori stack."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Check:
    level: str
    name: str
    detail: str


def command(args: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        result = subprocess.run(
            args,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return result.returncode, result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def permission_checks() -> list[Check]:
    paths = [ROOT / ".env", ROOT / "agents" / ".env", ROOT / "agents" / "token.json", ROOT / "agents" / "credentials.json"]
    checks = []
    for path in paths:
        label = str(path.relative_to(ROOT))
        if not path.exists():
            checks.append(Check("WARN", f"Permissions {label}", "file is absent"))
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        checks.append(Check("PASS" if mode & 0o077 == 0 else "FAIL", f"Permissions {label}", oct(mode)))
    return checks


def core_port_check() -> Check:
    names = {"ai_postgres", "ai_redis", "ai_qdrant", "ai_langfuse", "ai_n8n"}
    code, output = command(["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"])
    if code != 0:
        return Check("FAIL", "Core Docker ports", output or "docker ps failed")
    exposed = []
    for line in output.splitlines():
        name, _, ports = line.partition("\t")
        if name in names and ("0.0.0.0:" in ports or "[::]:" in ports):
            exposed.append(name)
    if exposed:
        return Check("FAIL", "Core Docker ports", f"public bind: {', '.join(sorted(exposed))}")
    return Check("PASS", "Core Docker ports", "loopback-only")


def dev_port_check() -> Check:
    code, output = command(["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"])
    if code != 0:
        return Check("WARN", "Development stacks", "cannot inspect Docker ports")
    exposed = []
    for line in output.splitlines():
        name, _, ports = line.partition("\t")
        if name.startswith("amori-local-") and ("0.0.0.0:" in ports or "[::]:" in ports):
            exposed.append(name)
    if exposed:
        return Check("WARN", "Development stacks", f"LAN-visible services: {len(exposed)}; keep only while testing mobile app")
    return Check("PASS", "Development stacks", "no LAN-visible development services")


def tracked_secret_check() -> Check:
    code, files_output = command(["git", "ls-files"])
    if code != 0:
        return Check("FAIL", "Tracked secrets", files_output or "git ls-files failed")
    patterns = [
        re.compile(rb"gh[opsu]_[A-Za-z0-9]{20,}"),
        re.compile(rb"gsk_[A-Za-z0-9]{20,}"),
        re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        re.compile(rb"[0-9]{8,12}:[A-Za-z0-9_-]{30,}"),
    ]
    hits = []
    for relative in files_output.splitlines():
        path = ROOT / relative
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if any(pattern.search(data) for pattern in patterns):
            hits.append(relative)
    if hits:
        return Check("FAIL", "Tracked secrets", f"secret-like data in: {', '.join(hits[:8])}")
    return Check("PASS", "Tracked secrets", "no high-confidence secret patterns")


def launchd_secret_check() -> Check:
    directory = Path.home() / "Library" / "LaunchAgents"
    suspicious = []
    for path in directory.glob("*.plist"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if re.search(r"(TOKEN|PASSWORD|API_KEY|SECRET)</(?:string|key)>", text, re.I):
            suspicious.append(path.name)
    if suspicious:
        return Check("FAIL", "LaunchAgent secrets", f"move env values out of: {', '.join(suspicious[:8])}")
    return Check("PASS", "LaunchAgent secrets", "no credential variables embedded")


def firewall_check() -> Check:
    tool = "/usr/libexec/ApplicationFirewall/socketfilterfw"
    code, output = command([tool, "--getglobalstate"])
    if code != 0:
        return Check("WARN", "macOS firewall", output or "cannot read state")
    enabled = "enabled" in output.lower() and "disabled" not in output.lower()
    return Check("PASS" if enabled else "WARN", "macOS firewall", output)


def runtime_check() -> Check:
    python = ROOT / ".venv" / "bin" / "python"
    if not python.exists():
        return Check("WARN", "Python runtime", "isolated .venv is absent; run make bootstrap-runtime")
    code, output = command([str(python), "-m", "pip", "check"], timeout=30)
    return Check(
        "PASS" if code == 0 else "FAIL",
        "Python runtime",
        "isolated dependencies consistent" if code == 0 else output[:160],
    )


def main() -> int:
    checks = permission_checks()
    checks.extend([
        core_port_check(), dev_port_check(), tracked_secret_check(), launchd_secret_check(),
        runtime_check(), firewall_check(),
    ])
    print("Amori security check")
    print("=" * 72)
    for item in checks:
        print(f"{item.level:4}  {item.name:28} {item.detail}")
    totals = {level: sum(item.level == level for item in checks) for level in ("PASS", "WARN", "FAIL")}
    print("-" * 72)
    print(f"PASS={totals['PASS']} WARN={totals['WARN']} FAIL={totals['FAIL']}")
    return 1 if totals["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
