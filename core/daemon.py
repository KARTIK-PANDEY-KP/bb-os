#!/usr/bin/env python3
"""
Autonomous agent daemon — the heartbeat.

Biologically-inspired sleep-wake cycle:

  AWAKE — continuous stretch of heartbeats (/chat calls). A guaranteed
          minimum window of wakefulness, then exponential sleep pressure
          builds until it wins a stochastic coin flip.

  SLEEP — continuous digestion (/digest call). Processes all new data
          in chunks plus random replay of old memories. Does not wake
          until everything is digested.

The awake/sleep ratio evolves with maturity:
  - Newborn: mostly sleeping, short awake bursts, heavy replay.
  - Adult:   mostly awake, long stretches, efficient sleep.

Growth follows a power curve with tunable shape. All parameters are
sampled from distributions — no two cycles are identical.
"""

import json
import math
import os
import random
import sys
import time
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

KERNEL_URL = "http://localhost:8080"
CHAT_URL = f"{KERNEL_URL}/chat"
DIGEST_URL = f"{KERNEL_URL}/digest"
PING_URL = f"{KERNEL_URL}/ping"

# ---------------------------------------------------------------------------
# Growth curve — tunable constants
# ---------------------------------------------------------------------------

MATURITY_CYCLES = int(os.environ.get("MATURITY_CYCLES", "500"))
GROWTH_CURVE = float(os.environ.get("GROWTH_CURVE", "0.5"))
JITTER = float(os.environ.get("MATURITY_JITTER", "0.05"))

# Awake window bounds (guaranteed heartbeats before sleep pressure starts)
MIN_GUARANTEED = 1
MAX_GUARANTEED = 8

# Awake capacity bounds (exponential scale for pressure after the window)
MIN_CAPACITY = 1.0
MAX_CAPACITY = 6.0

# Cooldown bounds (seconds between awake heartbeats)
MIN_COOLDOWN = 5.0
MAX_COOLDOWN = 30.0

# Replay ratio bounds
MIN_REPLAY = 0.05
MAX_REPLAY = 0.60

# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

STATE_PATH = "/repo/.memory/daemon_state.json"

BOOT_MESSAGE = (
    "You just booted. Read /repo/LAYOUT.md to orient yourself, "
    "then decide what to do."
)
HEARTBEAT_MESSAGE = "Continue. What's next?"


def _load_state() -> dict:
    try:
        if os.path.isfile(STATE_PATH):
            with open(STATE_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return {"total_cycles": 0}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"[daemon] Warning: could not save state: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Stochastic parameter samplers
# ---------------------------------------------------------------------------

def _sample_maturity(total_cycles: int) -> float:
    t = min(1.0, total_cycles / max(MATURITY_CYCLES, 1))
    base = t ** GROWTH_CURVE
    noisy = base + random.uniform(-JITTER, JITTER)
    return max(0.0, min(1.0, noisy))


def _sample_min_awake(maturity: float) -> int:
    center = MIN_GUARANTEED + (MAX_GUARANTEED - MIN_GUARANTEED) * maturity
    sampled = center * random.uniform(0.7, 1.3)
    return max(1, round(sampled))


def _sample_awake_capacity(maturity: float) -> float:
    center = MIN_CAPACITY + (MAX_CAPACITY - MIN_CAPACITY) * maturity
    return max(0.5, center * random.uniform(0.7, 1.3))


def _sample_cooldown(maturity: float) -> float:
    center = MIN_COOLDOWN + (MAX_COOLDOWN - MIN_COOLDOWN) * maturity
    return max(2.0, center * random.uniform(0.6, 1.4))


def _sample_replay_ratio(maturity: float) -> float:
    center = 0.5 - 0.4 * maturity
    noisy = center + random.uniform(-0.08, 0.08)
    return max(MIN_REPLAY, min(MAX_REPLAY, noisy))


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _post_json(url: str, payload: dict | None = None, timeout: int = 600) -> dict:
    data = json.dumps(payload or {}).encode() if payload else b"{}"
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _ping() -> bool:
    try:
        req = urllib.request.Request(PING_URL)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Phase executors
# ---------------------------------------------------------------------------

def _awake(message: str) -> None:
    try:
        print(f"[daemon] AWAKE: {message[:60]}...", file=sys.stderr)
        result = _post_json(CHAT_URL, {"message": message})
        response = result.get("response", "")
        print(f"[daemon] AWAKE response: {response[:200]}...", file=sys.stderr)
    except Exception as e:
        print(f"[daemon] AWAKE error: {e}", file=sys.stderr)
        time.sleep(30)


def _sleep(replay_ratio: float) -> None:
    try:
        print(
            f"[daemon] SLEEP: digesting (replay_ratio={replay_ratio:.2f})...",
            file=sys.stderr,
        )
        result = _post_json(
            DIGEST_URL,
            {"replay_ratio": replay_ratio},
            timeout=600,
        )
        status = result.get("status", "?")
        chunks = result.get("chunks_processed", "?")
        replays = result.get("replays", "?")
        print(
            f"[daemon] SLEEP complete: {status} "
            f"(chunks={chunks}, replays={replays})",
            file=sys.stderr,
        )
    except Exception as e:
        print(f"[daemon] SLEEP error: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    print("[daemon] Waiting for kernel...", file=sys.stderr)
    while not _ping():
        time.sleep(2)
    print("[daemon] Kernel ready. Starting heartbeat.", file=sys.stderr)

    state = _load_state()
    total_cycles = state.get("total_cycles", 0)
    first = True

    while True:
        maturity = _sample_maturity(total_cycles)
        min_awake = _sample_min_awake(maturity)
        capacity = _sample_awake_capacity(maturity)

        print(
            f"[daemon] Cycle {total_cycles} | maturity={maturity:.3f} "
            f"min_awake={min_awake} capacity={capacity:.2f}",
            file=sys.stderr,
        )

        # --- AWAKE PHASE — continuous, variable duration ---
        awake_count = 0
        while True:
            message = BOOT_MESSAGE if first else HEARTBEAT_MESSAGE
            first = False
            _awake(message)
            awake_count += 1

            if awake_count < min_awake:
                cd = _sample_cooldown(maturity)
                print(
                    f"[daemon]   heartbeat {awake_count}/{min_awake} "
                    f"(guaranteed window) cooldown={cd:.1f}s",
                    file=sys.stderr,
                )
                time.sleep(cd)
                continue

            overtime = awake_count - min_awake
            p = 1 - math.exp(-overtime / capacity)
            roll = random.random()

            if roll < p:
                print(
                    f"[daemon]   heartbeat {awake_count} | "
                    f"overtime={overtime} p_sleep={p:.3f} roll={roll:.3f} → SLEEPING",
                    file=sys.stderr,
                )
                break

            cd = _sample_cooldown(maturity)
            print(
                f"[daemon]   heartbeat {awake_count} | "
                f"overtime={overtime} p_sleep={p:.3f} roll={roll:.3f} → "
                f"staying awake, cooldown={cd:.1f}s",
                file=sys.stderr,
            )
            time.sleep(cd)

        # --- SLEEP PHASE — continuous, process everything ---
        replay_ratio = _sample_replay_ratio(maturity)
        _sleep(replay_ratio)

        total_cycles += 1
        _save_state({"total_cycles": total_cycles})


if __name__ == "__main__":
    main()
