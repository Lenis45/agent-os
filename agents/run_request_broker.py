#!/usr/bin/env python3
"""Launch the local request broker."""

import os

import uvicorn


if __name__ == "__main__":
    uvicorn.run(
        "request_broker:app",
        host=os.getenv("AMORI_BROKER_HOST", "127.0.0.1"),
        port=int(os.getenv("AMORI_BROKER_PORT", "8110")),
        log_level=os.getenv("AMORI_BROKER_LOG_LEVEL", "info"),
    )
