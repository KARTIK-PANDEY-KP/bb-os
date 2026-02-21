# Inner Kernel API

HTTP API for incremental Python execution and shell access, with cryo/CRIU persistence.

- **Python:** `POST /exec` — execute code in a persistent GLOBAL namespace (notebook semantics).
- **Shell:** `POST /shell`, `runtime.shell.run()` — run commands with persistent cwd/env.
- **Resources:** `runtime.resource()`, `runtime.shell.resource()` — reconnectable connections (HTTP, SSH, WebSocket, DB).

---

# Runtime Resource API

The `runtime.resource()` API provides a unified way to manage external connections that can survive checkpoint/restore operations. **Always use this API instead of creating connections directly.**

## Why Use `runtime.resource()`?

When the kernel is checkpointed and restored:
- ❌ **Direct connections break** - TCP sockets, WebSockets, database connections all become invalid
- ✅ **Resource handles survive** - They remember how to reconnect and do so automatically

```python
# ❌ BAD: Direct connection - will break after restore
ws = websocket.create_connection("wss://example.com")

# ✅ GOOD: Resource handle - auto-reconnects after restore
ws_handle = runtime.resource(lambda: websocket.create_connection("wss://example.com"))
ws = ws_handle.get()
```

## Basic Usage

### Step 1: Define a Factory Function

Create a function that establishes your connection:

```python
def connect_to_server():
    return websocket.create_connection("wss://my-server.com")
```

### Step 2: Register with `runtime.resource()`

```python
ws_handle = runtime.resource(connect_to_server)
```

### Step 3: Use `.get()` to Access the Connection

```python
ws = ws_handle.get()
ws.send("hello")
response = ws.recv()
```

The first call creates the connection. Subsequent calls return the cached connection.

## Complete Examples

### WebSocket Connection

```python
import websocket
import json

def make_ws():
    print("Creating WebSocket connection...")
    ws = websocket.create_connection("wss://my-server.com", timeout=10)
    return ws

ws_handle = runtime.resource(make_ws) \
    .teardown(lambda ws: ws.close()) \
    .retry(max_attempts=5, backoff_base=1.0)

# Use it
ws = ws_handle.get()
ws.send(json.dumps({"type": "subscribe", "channel": "updates"}))
print(ws.recv())
```

### Database Connection (PostgreSQL)

```python
import psycopg2

def connect_db():
    print("Connecting to database...")
    return psycopg2.connect(
        host="db.example.com",
        database="myapp",
        user="app_user",
        password="secret"
    )

db_handle = runtime.resource(connect_db) \
    .teardown(lambda conn: conn.close()) \
    .retry(max_attempts=3)

# Use it
conn = db_handle.get()
cursor = conn.cursor()
cursor.execute("SELECT * FROM users")
```

### Redis Connection

```python
import redis

def connect_redis():
    return redis.Redis(host='redis.example.com', port=6379, db=0)

redis_handle = runtime.resource(connect_redis) \
    .teardown(lambda r: r.close()) \
    .retry(max_attempts=5)

# Use it
r = redis_handle.get()
r.set('key', 'value')
```

### HTTP Session (with cookies/auth)

```python
import requests

def create_session():
    session = requests.Session()
    session.headers.update({'Authorization': 'Bearer my-token'})
    return session

session_handle = runtime.resource(create_session) \
    .teardown(lambda s: s.close())

# Use it
session = session_handle.get()
response = session.get("https://api.example.com/data")
```

### gRPC Channel

```python
import grpc

def create_channel():
    return grpc.insecure_channel('grpc.example.com:50051')

channel_handle = runtime.resource(create_channel) \
    .teardown(lambda ch: ch.close())

# Use it
channel = channel_handle.get()
stub = MyServiceStub(channel)
```

## API Reference

### `runtime.resource(factory, *args, **kwargs)`

Creates a new resource handle.

| Parameter | Type | Description |
|-----------|------|-------------|
| `factory` | `Callable` | Function that creates the connection |
| `*args` | `Any` | Arguments passed to factory |
| `**kwargs` | `Any` | Keyword arguments passed to factory |

Returns: `ResourceHandle`

### `ResourceHandle.get()`

Returns the connection, creating it if necessary.

```python
ws = ws_handle.get()
```

### `ResourceHandle.invalidate()`

Closes the current connection and marks it for recreation.

```python
ws_handle.invalidate()  # Connection closed
ws = ws_handle.get()    # New connection created
```

**Important:** Call `invalidate()` before checkpoint to release sockets!

### `ResourceHandle.teardown(fn)`

Registers a cleanup function called when the resource is invalidated.

```python
ws_handle = runtime.resource(make_ws).teardown(lambda ws: ws.close())
```

### `ResourceHandle.retry(max_attempts=3, backoff_base=1.0)`

Configures automatic retry with exponential backoff.

```python
ws_handle = runtime.resource(make_ws).retry(max_attempts=5, backoff_base=2.0)
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_attempts` | 3 | Maximum connection attempts |
| `backoff_base` | 1.0 | Base seconds for exponential backoff |

Backoff delays: 1s, 2s, 4s, 8s, ... (capped at 30s)

## Checkpoint/Restore Pattern

### Before Checkpoint

Always invalidate active connections before checkpointing:

```python
# Close all connections
ws_handle.invalidate()
db_handle.invalidate()
redis_handle.invalidate()

# Now checkpoint is safe
# POST /criu/checkpoint
```

### After Restore

Just call `.get()` - connections auto-reconnect:

```python
# After restore, just use them normally
ws = ws_handle.get()  # Reconnects automatically
ws.send("I'm back!")
```

## Best Practices

### 1. Always Use Resource Handles

Never create connections directly. Always wrap them:

```python
# ❌ Never do this
conn = psycopg2.connect(...)

# ✅ Always do this
conn_handle = runtime.resource(lambda: psycopg2.connect(...))
conn = conn_handle.get()
```

### 2. Always Define Teardown

Prevent resource leaks by defining cleanup:

```python
handle = runtime.resource(create_conn).teardown(lambda c: c.close())
```

### 3. Store Handles, Not Connections

Keep the handle in a variable, get fresh connections as needed:

```python
# ✅ Store the handle
db_handle = runtime.resource(connect_db)

def query_users():
    conn = db_handle.get()  # Gets cached or fresh connection
    return conn.execute("SELECT * FROM users")
```

### 4. Invalidate Before Long Operations

If a connection might timeout, invalidate and reconnect:

```python
# After a long computation
ws_handle.invalidate()
ws = ws_handle.get()  # Fresh connection
```

### 5. Use Retry for Unreliable Connections

```python
handle = runtime.resource(connect) \
    .retry(max_attempts=10, backoff_base=0.5)
```

## Error Handling

The resource API handles connection errors gracefully:

```python
try:
    ws = ws_handle.get()
    ws.send("hello")
except Exception as e:
    # Connection failed after all retries
    print(f"Connection failed: {e}")
    
    # Optionally invalidate and try again later
    ws_handle.invalidate()
```

## Complete Working Example

```python
import websocket
import json

# Define factory
def make_connection():
    print("[factory] Connecting to server...")
    ws = websocket.create_connection("wss://echo.example.com", timeout=10)
    # Read welcome message
    welcome = ws.recv()
    print(f"[factory] Connected: {welcome}")
    return ws

# Create handle with teardown and retry
ws_handle = runtime.resource(make_connection) \
    .teardown(lambda ws: ws.close()) \
    .retry(max_attempts=5, backoff_base=1.0)

# Use the connection
def send_message(msg):
    ws = ws_handle.get()
    ws.send(json.dumps(msg))
    return json.loads(ws.recv())

# Store some state
messages_sent = 0

# Send messages
response = send_message({"type": "hello"})
messages_sent += 1
print(f"Sent {messages_sent} messages")

# === BEFORE CHECKPOINT ===
ws_handle.invalidate()
# Now safe to checkpoint

# === AFTER RESTORE ===
# State is preserved: messages_sent = 1
# Connection will auto-reconnect on next .get()

response = send_message({"type": "still here", "count": messages_sent})
messages_sent += 1
print(f"Sent {messages_sent} messages total")
```

## Cryo Store/Reload (dill)

The same pattern applies for **cryo** (dill-based persistence via `POST /cryo/store` and `POST /cryo/reload`):

1. **Before store:** Call `invalidate()` on all handles to release sockets.
2. **After reload:** Call `handle.get()` — it reconnects automatically.

```python
import urllib.request

def fetch_url(url="https://httpbin.org/get"):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode()

# Wrap in resource handle (same pattern as WebSocket/DB)
http_handle = runtime.resource(fetch_url)

# Use it
data = http_handle.get()
print("Fetched:", data[:80])

# Before cryo store
http_handle.invalidate()

# POST /cryo/store
# POST /reset
# POST /cryo/reload

# After reload — same pattern
data = http_handle.get()  # Reconnects (re-fetches)
print("After reload:", data[:80])
```

**Note:** `ResourceHandle` may not serialize with dill (threading locks). If handles don't persist through cryo, store the fetched data or a reconnect factory instead. CRIU preserves handles in memory; cryo reloads into a fresh process.

## Shell API

The kernel exposes shell execution with **persistent cwd and env** — survives cryo and reset.

### HTTP Endpoints

| Endpoint | Description |
|----------|-------------|
| `POST /shell` | Run shell command — `{"command": "ls -la"}` |
| `POST /shell/cd` | Set working dir — `{"path": "/tmp"}` |
| `POST /shell/env` | Set env var — `{"key": "VAR", "value": "value"}` |

### Python API (`runtime.shell`)

```python
runtime.shell.cd("/tmp")
runtime.shell.env("MY_VAR", "hello")
out = runtime.shell.run("echo $MY_VAR")
print(out["stdout"])  # hello
print(out["returncode"])  # 0
```

### Persistence

`_shell_context` (cwd, env) is stored in the kernel globals and persists across:
- `POST /cryo/store` / `POST /cryo/reload`
- `POST /reset` (shell context is kept)
- CRIU checkpoint/restore

### Shell Resources (SSH, Remote Exec)

For **network shell access** (SSH, remote execution), use `runtime.resource()` or `runtime.shell.resource()` — same reconnect pattern:

```python
# Example: SSH connection (requires paramiko)
import paramiko

def connect_ssh():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect("host", username="user", key_filename="/path/to/key")
    return c

ssh_handle = runtime.shell.resource(connect_ssh) \
    .teardown(lambda c: c.close()) \
    .retry(max_attempts=3)

# Use it
conn = ssh_handle.get()
stdin, stdout, stderr = conn.exec_command("ls -la")
print(stdout.read().decode())

# Before cryo store
ssh_handle.invalidate()
```

### Shell + Cryo Pattern

```python
runtime.shell.cd("/tmp")
runtime.shell.env("TEST", "persisted")
r = runtime.shell.run("pwd")
print(r["stdout"])  # /tmp

# POST /cryo/store
# POST /reset
# POST /cryo/reload

# After reload — cwd and env persist
r = runtime.shell.run("echo $TEST")
print(r["stdout"])  # persisted
```

## Summary

| What | How |
|------|-----|
| Run shell command | `runtime.shell.run("ls")` or `POST /shell` |
| Shell cwd/env | `runtime.shell.cd(path)`, `runtime.shell.env(k,v)` |
| Shell resource (SSH) | `runtime.shell.resource(factory)` — same as `runtime.resource` |
| Create connection | `handle = runtime.resource(factory)` |
| Get connection | `conn = handle.get()` |
| Close connection | `handle.invalidate()` |
| Add cleanup | `.teardown(lambda c: c.close())` |
| Add retry | `.retry(max_attempts=5)` |
| Before checkpoint / cryo store | Call `invalidate()` on all handles |
| After restore / cryo reload | Just call `get()` - auto-reconnects |

**Remember:** The handle persists across checkpoint/restore. The connection doesn't. That's the whole point.
