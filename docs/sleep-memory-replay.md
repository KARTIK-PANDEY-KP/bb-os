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
- `replay_ratio`: fraction of digest effort allocated to replay
- `N`: number of new-data chunks in current sleep digest
- `R`: number of replay chunks

Uniform draw notation:

$$
U(a,b) \sim \text{Uniform}[a,b]
$$

## 1) Maturity Function

Normalize life progress:

$$
t = \min\left(1,\frac{\text{cycles}}{\text{MATURITY\_CYCLES}}\right)
$$

Power growth:

$$
\text{base} = t^{\text{GROWTH\_CURVE}}
$$

Add jitter and clamp:

$$
m = \operatorname{clamp}\!\left(\text{base} + U(-\text{JITTER},\text{JITTER}),\,0,\,1\right)
$$

Default constants:
- `MATURITY_CYCLES = 500`
- `GROWTH_CURVE = 0.5`
- `JITTER = 0.05`

With defaults:

$$
m \approx \operatorname{clamp}\!\left(\left(\min\left(1,\frac{\text{cycles}}{500}\right)\right)^{0.5}+U(-0.05,0.05),\,0,\,1\right)
$$

Interpretation:
- Early cycles change behavior quickly (`0.5` power).
- Later cycles saturate near `m = 1`.

## 2) Guaranteed Awake Window

Center increases with maturity:

$$
\text{center}_{awake}=\text{MIN\_GUARANTEED}+(\text{MAX\_GUARANTEED}-\text{MIN\_GUARANTEED})\,m
$$

Randomized sample:

$$
\text{minAwake}=\max\left(1,\operatorname{round}\!\left(\text{center}_{awake}\cdot U(0.7,1.3)\right)\right)
$$

Defaults:
- `MIN_GUARANTEED = 1`
- `MAX_GUARANTEED = 8`

Equivalent default center:

$$
\text{center}_{awake}=1+7m
$$

Rule:
- If `awakeCount < minAwake`, sleep is impossible for that beat.

## 3) Sleep Pressure (Sleep Trigger Probability)

After guaranteed awake window:

$$
\text{overtime}=\text{awakeCount}-\text{minAwake}
$$

Capacity increases with maturity:

$$
\text{center}_{capacity}=\text{MIN\_CAPACITY}+(\text{MAX\_CAPACITY}-\text{MIN\_CAPACITY})\,m
$$

$$
\text{capacity}=\max\left(0.5,\text{center}_{capacity}\cdot U(0.7,1.3)\right)
$$

Defaults:
- `MIN_CAPACITY = 1.0`
- `MAX_CAPACITY = 6.0`
- default center form: `center_capacity = 1 + 5m`

Sleep probability per heartbeat:

$$
p_{sleep}=1-\exp\!\left(-\frac{\text{overtime}}{\text{capacity}}\right)
$$

Decision:

$$
\text{roll}=U(0,1), \quad
\text{sleep if } \text{roll}<p_{sleep} \text{ else stay awake}
$$

Interpretation:
- At `overtime = 0`, `p_sleep = 0`.
- As `overtime` grows, `p_sleep` approaches `1`.
- Larger `capacity` means slower pressure growth.

## 4) Heartbeat Cooldown (Awake Cadence)

Center:

$$
\text{center}_{cooldown}=\text{MIN\_COOLDOWN}+(\text{MAX\_COOLDOWN}-\text{MIN\_COOLDOWN})\,m
$$

Sample:

$$
\text{cooldown}_{sec}=\max\left(2,\text{center}_{cooldown}\cdot U(0.6,1.4)\right)
$$

Defaults:
- `MIN_COOLDOWN = 5`
- `MAX_COOLDOWN = 30`
- default center form: `center_cooldown = 5 + 25m`

Interpretation:
- Mature agents usually have longer cooldowns between heartbeats.

## 5) Replay Ratio (How Much of Sleep Is Replay)

Center decreases with maturity:

$$
\text{center}_{replay}=0.5-0.4m
$$

Sample and clamp:

$$
\text{replay\_ratio}=\operatorname{clamp}\!\left(\text{center}_{replay}+U(-0.08,0.08),\text{MIN\_REPLAY},\text{MAX\_REPLAY}\right)
$$

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

$$
H_{new}=H[h:], \quad T_{new}=T[t:]
$$

$$
H_{old}=H[:h], \quad T_{old}=T[:t]
$$

Chunk sizes:
- `HISTORY_CHUNK_SIZE = 10`
- `TOOL_LOG_CHUNK_SIZE = 20`

New chunk count:

$$
N_H=\left\lceil \frac{|H_{new}|}{10} \right\rceil, \quad
N_T=\left\lceil \frac{|T_{new}|}{20} \right\rceil
$$

$$
N=N_H+N_T
$$

Replay chunk count (always at least 1):

$$
R=\max\left(1,\left\lceil \max(N,1)\cdot \text{replay\_ratio} \right\rceil\right)
$$

Total digest workload:

$$
W=N+R
$$

Replay sampling from old chunk pool:

$$
\text{selected}=\operatorname{random\_sample}\!\left(\text{pool},\min(R,|\text{pool}|)\right)
$$

Important behavior:
- Learnings are updated incrementally after each chunk.
- Digest cursors are persisted once at end of digest:
  - `h_next = len(H)`
  - `t_next = len(T)`
- Next sleep only processes newly accumulated logs.

## 7) Full Cycle Summary

Each daemon cycle:

1. Sample `m`, `minAwake`, and `capacity`.
2. Run heartbeats until stochastic sleep trigger.
3. Sample `replay_ratio`.
4. Run one uninterrupted digest (`N` new chunks + `R` replay chunks).
5. Increment cycle count.

$$
\text{cycles}_{next}=\text{cycles}+1
$$

This is the full sleep-energy model in BB: maturity controls wake tolerance, sleep pressure speed, heartbeat cadence, and replay fraction.
