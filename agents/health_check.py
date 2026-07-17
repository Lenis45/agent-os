#!/usr/bin/env python3
"""
Compatibility wrapper for the old health_check entrypoint.

The real monitor is infra_monitor.py. Keeping this file prevents stale external
jobs from sending the old "Health Check" alert format with incomplete checks.
"""
import sys

import infra_monitor


def run() -> int:
    return infra_monitor.run_check()


if __name__ == "__main__":
    sys.exit(run())
