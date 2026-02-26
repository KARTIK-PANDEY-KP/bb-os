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
U(a,b) \sim \mathrm{Uniform}[a,b]
$$

Constant shorthand used in equations:
- `M_c = MATURITY_CYCLES`
- `G = GROWTH_CURVE`
- `J = JITTER`
- `G_min, G_max = MIN_GUARANTEED, MAX_GUARANTEED`
- `C_min, C_max = MIN_CAPACITY, MAX_CAPACITY`
- `D_min, D_max = MIN_COOLDOWN, MAX_COOLDOWN`
- `R_min, R_max = MIN_REPLAY, MAX_REPLAY`

## 1) Maturity Function

Normalize life progress:

$$
t = \min\left(1,\frac{cycles}{M_c}\right)
$$

Power growth:

$$
b = t^{G}
$$

Add jitter and clamp:

$$
m = \min\!\left(1,\max\!\left(0, b + U(-J,J)\right)\right)
$$

Default constants:
- `MATURITY_CYCLES = 500`
- `GROWTH_CURVE = 0.5`
- `JITTER = 0.05`

With defaults:

$$
m \approx \min\!\left(1,\max\!\left(0,\left(\min\left(1,\frac{cycles}{500}\right)\right)^{0.5}+U(-0.05,0.05)\right)\right)
$$

Interpretation:
- Early cycles change behavior quickly (`0.5` power).
- Later cycles saturate near `m = 1`.

## 2) Guaranteed Awake Window

Center increases with maturity:

$$
c_{awake}=G_{min}+(G_{max}-G_{min})m
$$

Randomized sample:

$$
minAwake=\max\left(1,\mathrm{round}\!\left(c_{awake}\,U(0.7,1.3)\right)\right)
$$

Defaults:
- `MIN_GUARANTEED = 1`
- `MAX_GUARANTEED = 8`

Equivalent default center:

$$
c_{awake}=1+7m
$$

Rule:
- If `awakeCount < minAwake`, sleep is impossible for that beat.

## 3) Sleep Pressure (Sleep Trigger Probability)

After guaranteed awake window:

$$
overtime=awakeCount-minAwake
$$

Capacity increases with maturity:

$$
c_{cap}=C_{min}+(C_{max}-C_{min})m
$$

$$
capacity=\max\left(0.5,c_{cap}\,U(0.7,1.3)\right)
$$

Defaults:
- `MIN_CAPACITY = 1.0`
- `MAX_CAPACITY = 6.0`
- default center form: `center_capacity = 1 + 5m`

Sleep probability per heartbeat:

$$
p_{sleep}=1-\exp\!\left(-\frac{overtime}{capacity}\right)
$$

Decision:

$$
roll=U(0,1)
$$

Sleep rule: sleep if `roll < p_sleep`, else stay awake.

Interpretation:
- At `overtime = 0`, `p_sleep = 0`.
- As `overtime` grows, `p_sleep` approaches `1`.
- Larger `capacity` means slower pressure growth.

## 4) Heartbeat Cooldown (Awake Cadence)

Center:

$$
c_{cool}=D_{min}+(D_{max}-D_{min})m
$$

Sample:

$$
cooldown_{sec}=\max\left(2,c_{cool}\,U(0.6,1.4)\right)
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
c_{replay}=0.5-0.4m
$$

Sample and clamp:

$$
replay\_ratio=\min\!\left(R_{max},\max\!\left(R_{min},c_{replay}+U(-0.08,0.08)\right)\right)
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
R=\max\left(1,\left\lceil \max(N,1)\,replay\_ratio \right\rceil\right)
$$

Total digest workload:

$$
W=N+R
$$

Replay sampling from old chunk pool:

$$
selected=sample\!\left(pool,\min(R,|pool|)\right)
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
cycles_{next}=cycles+1
$$

This is the full sleep-energy model in BB: maturity controls wake tolerance, sleep pressure speed, heartbeat cadence, and replay fraction.
