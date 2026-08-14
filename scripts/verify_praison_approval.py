#!/usr/bin/env python3
"""Verify the backported per-invocation approval fix before ignoring its advisory.

PYSEC-2026-2946 currently maps the praisonaiagents 1.x package to a PraisonAI
4.x fixed version that is not available for this distribution. The installed
1.6.166 code contains the fix, so the release gate proves the behavior instead
of trusting a version-only exception.
"""
import inspect
import sys

from importlib.metadata import version
from praisonaiagents import approval
from praisonaiagents.approval.registry import ApprovalRegistry


ADVISORY = "PYSEC-2026-2946"


def verify() -> None:
    first = ApprovalRegistry._approval_cache_key(
        "execute_command", {"command": "ls -la"}, "worker-a"
    )
    changed_arguments = ApprovalRegistry._approval_cache_key(
        "execute_command", {"command": "env"}, "worker-a"
    )
    changed_agent = ApprovalRegistry._approval_cache_key(
        "execute_command", {"command": "ls -la"}, "worker-b"
    )
    decorator_source = inspect.getsource(approval.require_approval)

    failures = []
    if first == changed_arguments:
        failures.append("approval cache does not include invocation arguments")
    if first == changed_agent:
        failures.append("approval cache does not include agent identity")
    if "is_already_approved(tool_name, approval_args)" not in decorator_source:
        failures.append("approval wrapper does not check normalized call arguments")
    if "mark_approved(tool_name, approval_args)" not in decorator_source:
        failures.append("approval wrapper does not store normalized call arguments")
    if failures:
        raise RuntimeError("; ".join(failures))


def main() -> int:
    try:
        verify()
    except Exception as exc:
        print(f"FAIL {ADVISORY}: {exc}", file=sys.stderr)
        return 1
    print(
        f"PASS {ADVISORY}: praisonaiagents {version('praisonaiagents')} "
        "uses per-agent, per-arguments approval keys"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
