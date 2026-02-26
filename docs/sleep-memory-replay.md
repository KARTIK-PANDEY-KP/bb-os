# Sleep-Memory Replay Math (Awake, New Memory, Old Memory)

This document defines the exact equations used by BB's daemon and digest pipeline for awake duration, sleep triggering, and replay effort.

Implementation sources:
- `core/daemon.py` (maturity, awake window, sleep pressure, cooldown, replay ratio sampling)
- `core/agent.py` (digest chunking, replay chunk count and sampling, cursor updates)

## Variables

- `cycles`: completed wake+sleep cycles (persisted in `.memory/daemon_state.json`)
- `m`: maturity in `[0, 1]`
- `awakeCount`: number of heartbeats in current awake phase
- `minAwake`: guaranteed awake heartbeats before sleep can happen
- `overtime`: heartbeats beyond the guaranteed awake window
- `capacity`: sleep-pressure scale
- `p_sleep`: probability of sleeping on current heartbeat
- `roll`: uniform random draw in `[0, 1]`
- `cooldown_sec`: time between two awake heartbeats
- `replay_ratio` (rho): fraction of digest effort allocated to replay
- `N`: number of new-data chunks in current sleep digest
- `R`: number of replay chunks

`U(a, b)` means a random sample from uniform distribution `[a, b]`.

## 1) Maturity Function

Normalize life progress:

`t = min(1, cycles / MATURITY_CYCLES)`

Power growth:

`base = t ^ GROWTH_CURVE`

Add jitter and clamp:

`m = clamp(base + U(-JITTER, JITTER), 0, 1)`

Default constants:
- `MATURITY_CYCLES = 500`
- `GROWTH_CURVE = 0.5`
- `JITTER = 0.05`

With defaults:

`m ~= clamp((min(1, cycles/500))^0.5 + U(-0.05, 0.05), 0, 1)`

Interpretation:
- Early cycles change behavior quickly (`^0.5`).
- Later cycles saturate near `m = 1`.

## 2) Guaranteed Awake Window

Center increases with maturity:

`center_awake = MIN_GUARANTEED + (MAX_GUARANTEED - MIN_GUARANTEED) * m`

Randomized sample:

`minAwake = max(1, round(center_awake * U(0.7, 1.3)))`

Defaults:
- `MIN_GUARANTEED = 1`
- `MAX_GUARANTEED = 8`

Equivalent default center:

`center_awake = 1 + 7m`

Rule:
- If `awakeCount < minAwake`, sleep is impossible for that beat.

## 3) Sleep Pressure (Sleep Trigger Probability)

After guaranteed awake window:

`overtime = awakeCount - minAwake`

Capacity increases with maturity:

`center_capacity = MIN_CAPACITY + (MAX_CAPACITY - MIN_CAPACITY) * m`

`capacity = max(0.5, center_capacity * U(0.7, 1.3))`

Defaults:
- `MIN_CAPACITY = 1.0`
- `MAX_CAPACITY = 6.0`
- default center form: `center_capacity = 1 + 5m`

Sleep probability per heartbeat:

`p_sleep = 1 - exp(-overtime / capacity)`

Decision:

`roll = U(0, 1)`

`sleep if roll < p_sleep else stay awake`

Interpretation:
- At `overtime = 0`, `p_sleep = 0`.
- As `overtime` grows, `p_sleep` approaches `1`.
- Larger `capacity` means slower pressure growth.

## 4) Heartbeat Cooldown (Awake Cadence)

Center:

`center_cooldown = MIN_COOLDOWN + (MAX_COOLDOWN - MIN_COOLDOWN) * m`

Sample:

`cooldown_sec = max(2, center_cooldown * U(0.6, 1.4))`

Defaults:
- `MIN_COOLDOWN = 5`
- `MAX_COOLDOWN = 30`
- default center form: `center_cooldown = 5 + 25m`

Interpretation:
- Mature agents usually have longer cooldowns between heartbeats.

## 5) Replay Ratio (How Much of Sleep Is Replay)

Center decreases with maturity:

`center_replay = 0.5 - 0.4m`

Sample and clamp:

`replay_ratio = clamp(center_replay + U(-0.08, 0.08), MIN_REPLAY, MAX_REPLAY)`

Defaults:
- `MIN_REPLAY = 0.05`
- `MAX_REPLAY = 0.60`

Interpretation:
- Young: high replay fraction.
- Mature: lower replay fraction.

## 6) Digest Math: New Chunks + Replay Chunks

State cursors from `.memory/digest_state.json`:
- `h` (history cursor)
- `t` (tool-log cursor)

Given full logs `H` and `T`:

`H_new = H[h:]`

`T_new = T[t:]`

`H_old = H[:h]`

`T_old = T[:t]`

Chunk sizes:
- `HISTORY_CHUNK_SIZE = 10`
- `TOOL_LOG_CHUNK_SIZE = 20`

New chunk count:

`N_H = ceil(len(H_new) / 10)`

`N_T = ceil(len(T_new) / 20)`

`N = N_H + N_T`

Replay chunk count (always at least 1):

`R = max(1, ceil(max(N, 1) * replay_ratio))`

Total digest workload:

`W = N + R`

Replay sampling from old chunk pool:

`selected = random_sample(pool, min(R, len(pool)))`

Important behavior:
- Learnings are updated incrementally after each chunk.
- Digest cursors are persisted once at end of digest:
  - `h_next = len(H)`
  - `t_next = len(T)`
- Next sleep only processes newly accumulated logs.

## 7) Full Cycle Summary

Each daemon cycle:

1. Sample `m`, `minAwake`, `capacity`.
2. Run heartbeats until stochastic sleep trigger.
3. Sample `replay_ratio`.
4. Run one uninterrupted digest (`N` new chunks + `R` replay chunks).
5. Increment cycle count:
   - `cycles_next = cycles + 1`

This is the full sleep-energy model in BB: maturity controls wake tolerance, sleep pressure speed, heartbeat cadence, and replay fraction.
