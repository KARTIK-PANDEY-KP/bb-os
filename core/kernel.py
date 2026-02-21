#!/usr/bin/env python3
"""
Inner Service (Kernel) — HTTP API server for incremental Python execution.

- Runs an HTTP server on configurable port (default: 8080)
- Executes code in a persistent GLOBAL namespace (notebook semantics)
- Captures stdout/stderr + error tracebacks
- Exposes `runtime.resource(...)` to user code for generic reconnectable external resources
- Marks resources "stale" on boot so after CRIU restore, next use reconnects lazily

Endpoints:
  POST /exec       - Execute Python code
  POST /shell      - Run shell command (persisted cwd/env)
  POST /shell/cd   - Set shell working directory
  POST /shell/env  - Set shell env var
  POST /reset      - Clear namespace
  GET  /ping       - Health check
  GET  /status     - Get kernel status

Request body (JSON):
  {"id": "cell_001", "code": "x=1\\nprint(x)"}

Response (JSON):
  {"id": "cell_001", "status": "completed", "stdout": "1\\n", "stderr": "", "result": null, "error": null, "timing_ms": 3}
"""

import sys
import json
import io
import os
import time
import random
import signal
import subprocess
import threading
import traceback
import contextlib
from dataclasses import dataclass
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

# Global server reference for restart
_server: Optional["ThreadedHTTPServer"] = None
_restart_flag = False


# -----------------------------
# Generic resource handle API
# -----------------------------

@dataclass
class RetryPolicy:
    """
    Configuration for retry behavior with exponential backoff and jitter.
    """
    max_attempts: int = 10
    base_delay_s: float = 0.2
    max_delay_s: float = 5.0
    backoff: float = 2.0
    jitter: float = 0.2

    def sleep_time(self, attempt: int) -> float:
        """Calculate sleep time for a given attempt (1-indexed)."""
        delay = min(self.max_delay_s, self.base_delay_s * (self.backoff ** max(0, attempt - 1)))
        return max(0.0, delay * (1.0 + random.uniform(-self.jitter, self.jitter)))


class ResourceHandle:
    """
    Generic lifecycle wrapper for ANY external resource (WebSocket, TCP, DB, SDK client, etc.).

    Usage:
        ws = runtime.resource(create_connection, url) \\
                   .on_connect(lambda c: c.send("subscribe")) \\
                   .validate(lambda c: c.connected) \\
                   .retry(max_attempts=20)

        msg = ws.get().recv()  # Auto-reconnects if stale/dead

    Features:
    - factory(*args, **kwargs) creates a live connection object
    - on_connect(conn) runs after each (re)connect (auth/subscribe/handshake)
    - validate(conn) returns True if conn is healthy
    - teardown(conn) cleans up before reconnect
    - invalidate() forces reconnect on next get()
    - restored() marks handle stale after CRIU restore
    """

    def __init__(self, factory: Callable[..., Any], args: tuple, kwargs: dict):
        self._factory = factory
        self._args = args
        self._kwargs = kwargs

        self._lock = threading.Lock()
        self._conn: Any = None
        self._stale: bool = True  # Start stale so after restore the first use reconnects

        self._on_connect_fn: Optional[Callable[[Any], None]] = None
        self._validate_fn: Optional[Callable[[Any], bool]] = None
        self._teardown_fn: Optional[Callable[[Any], None]] = None
        self._retry = RetryPolicy()

    def on_connect(self, fn: Callable[[Any], None]) -> "ResourceHandle":
        """
        Register a callback to run after each successful (re)connection.
        Use this for authentication, subscription, or handshake logic.
        """
        self._on_connect_fn = fn
        return self

    def validate(self, fn: Callable[[Any], bool]) -> "ResourceHandle":
        """
        Register a function to check if the connection is still healthy.
        Called on get() to determine if reconnection is needed.
        """
        self._validate_fn = fn
        return self

    def teardown(self, fn: Callable[[Any], None]) -> "ResourceHandle":
        """
        Register a cleanup function called before reconnection.
        Use this to properly close the old connection.
        """
        self._teardown_fn = fn
        return self

    def retry(self, **kw) -> "ResourceHandle":
        """
        Configure retry policy. Options:
        - max_attempts: Maximum reconnection attempts (default: 10)
        - base_delay_s: Initial delay between attempts (default: 0.2)
        - max_delay_s: Maximum delay between attempts (default: 5.0)
        - backoff: Exponential backoff multiplier (default: 2.0)
        - jitter: Random jitter factor (default: 0.2)
        """
        self._retry = RetryPolicy(**kw)
        return self

    def invalidate(self) -> None:
        """
        Force the next get() to reconnect.
        Call this when you detect the connection is broken.
        """
        with self._lock:
            if self._conn is not None and self._teardown_fn is not None:
                try:
                    self._teardown_fn(self._conn)
                except Exception:
                    pass
            self._conn = None
            self._stale = True

    def restored(self) -> None:
        """
        Called at process boot (and implicitly after CRIU restore).
        Marks the handle stale so next get() reconnects lazily.
        Do NOT try to keep old sockets; force lazy reconnect.
        """
        self._stale = True

    def _healthy(self, conn: Any) -> bool:
        """Check if connection is healthy using the validate function."""
        if self._validate_fn is None:
            return True
        try:
            return bool(self._validate_fn(conn))
        except Exception:
            return False

    def get(self) -> Any:
        """
        Get the connection, reconnecting if necessary.
        Thread-safe with retry logic and exponential backoff.
        """
        with self._lock:
            # Return existing connection if healthy
            if self._conn is not None and not self._stale and self._healthy(self._conn):
                return self._conn

            # Teardown old connection if exists
            if self._conn is not None and self._teardown_fn is not None:
                try:
                    self._teardown_fn(self._conn)
                except Exception:
                    pass
                self._conn = None

            # Reconnect with retry
            last_err: Optional[BaseException] = None
            for attempt in range(1, self._retry.max_attempts + 1):
                try:
                    conn = self._factory(*self._args, **self._kwargs)
                    if self._on_connect_fn is not None:
                        self._on_connect_fn(conn)
                    if not self._healthy(conn):
                        raise RuntimeError("resource validation failed")
                    self._conn = conn
                    self._stale = False
                    return conn
                except BaseException as e:
                    last_err = e
                    if attempt < self._retry.max_attempts:
                        time.sleep(self._retry.sleep_time(attempt))

            # All attempts failed
            raise last_err if last_err is not None else RuntimeError("resource connection failed")


class ResourceRegistry:
    """
    Registry of all ResourceHandle instances.
    Allows bulk operations like marking all handles stale after restore.
    """

    def __init__(self) -> None:
        self._handles: List[ResourceHandle] = []
        self._lock = threading.Lock()

    def register(self, h: ResourceHandle) -> ResourceHandle:
        """Register a new handle and return it."""
        with self._lock:
            self._handles.append(h)
        return h

    def restored(self) -> None:
        """Mark all handles as stale (call after CRIU restore or at boot)."""
        with self._lock:
            for h in self._handles:
                try:
                    h.restored()
                except Exception:
                    pass

    def invalidate_all(self) -> None:
        """Force all handles to reconnect on next use."""
        with self._lock:
            for h in self._handles:
                try:
                    h.invalidate()
                except Exception:
                    pass

    def count(self) -> int:
        """Return number of registered handles."""
        with self._lock:
            return len(self._handles)


class RuntimeAPI:
    """
    Runtime API exposed to user code as `runtime`.

    Provides:
    - runtime.resource(factory, *args, **kwargs) -> ResourceHandle
      Create a reconnectable handle for any external resource.
    """

    def __init__(self) -> None:
        self._resources = ResourceRegistry()

    def resource(self, factory: Callable[..., Any], *args: Any, **kwargs: Any) -> ResourceHandle:
        """
        Create a generic reconnectable resource handle.

        Example:
            ws = runtime.resource(create_connection, "wss://example.com/ws") \\
                       .on_connect(lambda c: c.send("subscribe")) \\
                       .retry(max_attempts=20)

            msg = ws.get().recv()  # Auto-reconnects if needed
        """
        return self._resources.register(ResourceHandle(factory, args, kwargs))


# -----------------------------
# Shell API ( persistent cwd/env, survives cryo )
# -----------------------------

class ShellAPI:
    """
    Shell execution API with persistent cwd and env, survives cryo/checkpoint.

    Usage:
        runtime.shell.cd("/tmp")
        runtime.shell.env("MY_VAR", "value")
        out = runtime.shell.run("ls -la")

    For remote shell (SSH, etc.) use runtime.resource() with a factory that
    returns an SSH client or similar — same reconnect pattern as Python.
    """

    def __init__(self, globals_dict: Dict[str, Any]):
        self._G = globals_dict

    def _ctx(self) -> Dict[str, Any]:
        if "_shell_context" not in self._G:
            self._G["_shell_context"] = {"cwd": "/root", "env": {}}
        return self._G["_shell_context"]

    def cd(self, path: str) -> None:
        """Set working directory for subsequent shell commands."""
        self._ctx()["cwd"] = path

    def env(self, key: str, value: str) -> None:
        """Set env var for subsequent shell commands."""
        self._ctx()["env"][key] = value

    def run(self, command: str, shell: bool = True) -> Dict[str, Any]:
        """
        Run a shell command. Returns dict with stdout, stderr, returncode.
        Uses persisted cwd and env from runtime.shell.cd() / .env().
        """
        ctx = self._ctx()
        cwd = ctx.get("cwd") or "/root"
        base_env = os.environ.copy()
        base_env.update(ctx.get("env", {}))
        try:
            r = subprocess.run(
                command if shell else command.split(),
                shell=shell,
                cwd=cwd,
                env=base_env,
                capture_output=True,
                text=True,
                timeout=300,
            )
            return {
                "stdout": r.stdout or "",
                "stderr": r.stderr or "",
                "returncode": r.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"stdout": "", "stderr": "command timed out", "returncode": -1}
        except Exception as e:
            return {"stdout": "", "stderr": str(e), "returncode": -1}

    def resource(self, factory: Callable[..., Any], *args: Any, **kwargs: Any) -> ResourceHandle:
        """
        Reconnectable handle for shell-like connections (SSH, remote exec).
        Same as runtime.resource() — use for network shell access.
        Invalidate before cryo/store; get() reconnects after reload.
        """
        return self._G["runtime"].resource(factory, *args, **kwargs)


# -----------------------------
# Kernel state
# -----------------------------

# Create the runtime API instance
runtime = RuntimeAPI()

# Notebook semantics: one shared globals dict across all cells.
GLOBAL: Dict[str, Any] = {
    "__name__": "__main__",
    "runtime": runtime,
    "_shell_context": {"cwd": "/root", "env": {}},  # persisted across cryo
}

# Attach shell API to runtime
runtime.shell = ShellAPI(GLOBAL)

# Important: call this at boot. After CRIU restore, the process "boots" back into a running state;
# sockets may be dead. This ensures any previously-created handles reconnect on next use.
runtime._resources.restored()

# Single-exec lock to prevent concurrent cell execution
EXEC_LOCK = threading.Lock()

# Execution counter for status
EXEC_COUNT = 0
EXEC_COUNT_LOCK = threading.Lock()


# -----------------------------
# Kernel exec implementation
# -----------------------------

def _run_exec(code: str) -> Dict[str, Any]:
    """
    Execute code in the shared GLOBAL namespace.
    Captures stdout/stderr and handles exceptions with full tracebacks.
    """
    global EXEC_COUNT
    
    stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
    start = time.time()

    try:
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            exec(code, GLOBAL, GLOBAL)
        
        with EXEC_COUNT_LOCK:
            EXEC_COUNT += 1
        
        return {
            "status": "completed",
            "stdout": stdout_buf.getvalue(),
            "stderr": stderr_buf.getvalue(),
            "result": None,
            "error": None,
            "timing_ms": int((time.time() - start) * 1000),
        }
    except Exception as e:
        with EXEC_COUNT_LOCK:
            EXEC_COUNT += 1
        
        return {
            "status": "failed",
            "stdout": stdout_buf.getvalue(),
            "stderr": stderr_buf.getvalue(),
            "result": None,
            "error": {
                "type": type(e).__name__,
                "message": str(e),
                "traceback": traceback.format_exc(),
            },
            "timing_ms": int((time.time() - start) * 1000),
        }


def handle_exec(req: Dict[str, Any]) -> Dict[str, Any]:
    """Execute code in the persistent namespace."""
    code = req.get("code", "")
    
    # Ensure single exec at a time
    if not EXEC_LOCK.acquire(blocking=False):
        return {
            "status": "busy",
            "stdout": "",
            "stderr": "",
            "result": None,
            "error": {"type": "Busy", "message": "Another execution is in progress", "traceback": ""},
        }

    try:
        result = _run_exec(code)
        result["id"] = req.get("id")
        return result
    finally:
        EXEC_LOCK.release()


def handle_reset() -> Dict[str, Any]:
    """Clear user globals but keep runtime and shell context available."""
    global EXEC_COUNT
    
    keep: Dict[str, Any] = {"__name__": "__main__", "runtime": runtime}
    if "_shell_context" in GLOBAL:
        keep["_shell_context"] = GLOBAL["_shell_context"]
    GLOBAL.clear()
    GLOBAL.update(keep)
    runtime._resources.restored()
    
    with EXEC_COUNT_LOCK:
        EXEC_COUNT = 0
    
    return {"status": "completed"}


def handle_shell(req: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a shell command with persisted cwd/env."""
    command = req.get("command", "")
    if not command:
        return {"status": "failed", "error": "Missing 'command'", "stdout": "", "stderr": "", "returncode": -1}
    ctx = GLOBAL.get("_shell_context", {"cwd": "/root", "env": {}})
    cwd = ctx.get("cwd") or "/root"
    base_env = os.environ.copy()
    base_env.update(ctx.get("env", {}))
    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            env=base_env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        return {
            "status": "completed",
            "stdout": r.stdout or "",
            "stderr": r.stderr or "",
            "returncode": r.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"status": "failed", "stdout": "", "stderr": "command timed out", "returncode": -1}
    except Exception as e:
        return {"status": "failed", "stdout": "", "stderr": str(e), "returncode": -1}


def handle_shell_cd(req: Dict[str, Any]) -> Dict[str, Any]:
    """Set shell working directory."""
    path = req.get("path", "")
    if not path:
        return {"status": "failed", "error": "Missing 'path'"}
    if "_shell_context" not in GLOBAL:
        GLOBAL["_shell_context"] = {"cwd": "/root", "env": {}}
    GLOBAL["_shell_context"]["cwd"] = path
    return {"status": "completed", "path": path}


def handle_shell_env(req: Dict[str, Any]) -> Dict[str, Any]:
    """Set shell env var."""
    key = req.get("key", "")
    value = req.get("value", "")
    if not key:
        return {"status": "failed", "error": "Missing 'key'"}
    if "_shell_context" not in GLOBAL:
        GLOBAL["_shell_context"] = {"cwd": "/root", "env": {}}
    GLOBAL["_shell_context"]["env"][key] = value
    return {"status": "completed", "key": key, "value": value}


def handle_status() -> Dict[str, Any]:
    """Return kernel status."""
    with EXEC_COUNT_LOCK:
        count = EXEC_COUNT
    
    return {
        "status": "ok",
        "exec_count": count,
        "globals_count": len(GLOBAL),
        "resources_count": runtime._resources.count(),
        "busy": EXEC_LOCK.locked(),
    }


# -----------------------------
# HTTP API Server
# -----------------------------

class KernelRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the kernel API."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:
        """Log HTTP requests."""
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {args[0]}", file=sys.stderr)

    def _send_json(self, data: Dict[str, Any], status_code: int = 200) -> None:
        """Send JSON response."""
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Optional[Dict[str, Any]]:
        """Read JSON from request body."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        
        try:
            body = self.rfile.read(content_length)
            return json.loads(body.decode("utf-8"))
        except Exception as e:
            return None

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        """Handle GET requests."""
        path = urlparse(self.path).path

        if path == "/ping":
            self._send_json({"status": "ok"})
        elif path == "/status":
            self._send_json(handle_status())
        elif path == "/":
            self._send_json({
                "service": "inner-kernel",
                "endpoints": {
                    "POST /exec": "Execute Python code",
                    "POST /shell": "Run shell command",
                    "POST /shell/cd": "Set shell cwd",
                    "POST /shell/env": "Set shell env var",
                    "POST /reset": "Clear namespace",
                    "GET /ping": "Health check",
                    "GET /status": "Kernel status",
                }
            })
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        """Handle POST requests."""
        path = urlparse(self.path).path

        if path == "/exec":
            req = self._read_json()
            if req is None:
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            
            result = handle_exec(req)
            status_code = 200 if result.get("status") == "completed" else 500
            if result.get("status") == "busy":
                status_code = 429
            self._send_json(result, status_code)

        elif path == "/reset":
            result = handle_reset()
            self._send_json(result)

        elif path == "/shell":
            req = self._read_json()
            if req is None:
                req = {}
            result = handle_shell(req)
            status_code = 200 if result.get("status") == "completed" else 500
            self._send_json(result, status_code)

        elif path == "/shell/cd":
            req = self._read_json() or {}
            result = handle_shell_cd(req)
            status_code = 200 if result.get("status") == "completed" else 400
            self._send_json(result, status_code)

        elif path == "/shell/env":
            req = self._read_json() or {}
            result = handle_shell_env(req)
            status_code = 200 if result.get("status") == "completed" else 400
            self._send_json(result, status_code)

        else:
            self._send_json({"error": "Not found"}, 404)


class ThreadedHTTPServer(HTTPServer):
    """HTTP server that handles each request in a new thread."""
    
    def process_request(self, request, client_address):
        """Start a new thread to process the request."""
        thread = threading.Thread(target=self._handle_request_thread, args=(request, client_address))
        thread.daemon = True
        thread.start()

    def _handle_request_thread(self, request, client_address):
        """Handle one request in a thread."""
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


def _handle_restart_signal(signum, frame):
    """Handle SIGUSR1 to restart the HTTP server after CRIU restore."""
    global _restart_flag
    print(f"[kernel] Received SIGUSR1 - will restart HTTP server", file=sys.stderr)
    _restart_flag = True
    if _server:
        _server.shutdown()


def run_server(host: str = "0.0.0.0", port: int = 8080) -> None:
    """Run the HTTP API server with restart support."""
    global _server, _restart_flag
    
    # Set up SIGUSR1 handler for post-CRIU restart
    signal.signal(signal.SIGUSR1, _handle_restart_signal)
    
    while True:
        _restart_flag = False
        
        # Create and start server
        _server = ThreadedHTTPServer((host, port), KernelRequestHandler)
        _server.allow_reuse_address = True
        
        print(f"Inner Kernel API server running on http://{host}:{port}", file=sys.stderr)
        print(f"Endpoints:", file=sys.stderr)
        print(f"  POST /exec   - Execute Python code", file=sys.stderr)
        print(f"  POST /reset  - Clear namespace", file=sys.stderr)
        print(f"  GET  /ping   - Health check", file=sys.stderr)
        print(f"  GET  /status - Kernel status", file=sys.stderr)
        
        try:
            _server.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down...", file=sys.stderr)
            _server.shutdown()
            break
        
        # Check if we should restart (after SIGUSR1)
        if _restart_flag:
            print(f"[kernel] Restarting HTTP server...", file=sys.stderr)
            try:
                _server.server_close()
            except Exception:
                pass
            time.sleep(0.5)  # Brief pause before restart
            continue
        else:
            break


def main() -> None:
    """Main entry point."""
    host = os.environ.get("KERNEL_HOST", "0.0.0.0")
    port = int(os.environ.get("KERNEL_PORT", "8080"))
    run_server(host, port)


if __name__ == "__main__":
    main()
