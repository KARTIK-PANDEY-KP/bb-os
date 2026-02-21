#!/usr/bin/env python3
"""
Autonomous agent daemon. Runs as a background process, continuously calling
/chat to keep the agent thinking. Human messages arrive in the same conversation
stream — the agent sees everything as one continuous experience.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

KERNEL_URL = "http://localhost:8080"
CHAT_URL = f"{KERNEL_URL}/chat"
PING_URL = f"{KERNEL_URL}/ping"

INTERVAL = int(os.environ.get("DAEMON_INTERVAL", "30"))

BOOT_MESSAGE = (
    "You just booted. Read /repo/LAYOUT.md to orient yourself, "
    "then decide what to do."
)
HEARTBEAT_MESSAGE = "Continue. What's next?"


def _post_chat(message: str) -> dict:
    req = urllib.request.Request(
        CHAT_URL,
        data=json.dumps({"message": message}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read().decode())


def _ping() -> bool:
    try:
        req = urllib.request.Request(PING_URL)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def main():
    print("[daemon] Waiting for kernel...", file=sys.stderr)
    while not _ping():
        time.sleep(2)
    print("[daemon] Kernel ready. Starting autonomous loop.", file=sys.stderr)

    first = True
    while True:
        message = BOOT_MESSAGE if first else HEARTBEAT_MESSAGE
        first = False

        try:
            print(f"[daemon] Sending: {message[:60]}...", file=sys.stderr)
            result = _post_chat(message)
            response = result.get("response", "")
            print(f"[daemon] Response: {response[:200]}...", file=sys.stderr)
        except Exception as e:
            print(f"[daemon] Error: {e}. Retrying in 60s...", file=sys.stderr)
            time.sleep(60)
            continue

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
