#!/usr/bin/env python3
"""
CRIU Wrapper for Inner Kernel Service

This wrapper:
1. Starts the kernel as a subprocess
2. Proxies all requests to the kernel
3. Provides /criu/checkpoint and /criu/restore endpoints
4. Uses CRIU to checkpoint/restore the kernel subprocess

Run with: docker run --privileged inner-kernel-criu
"""

import asyncio
import os
import sys
import json
import time
import signal
import subprocess
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from typing import Any, Dict, Optional
import http.client


# =============================================================================
# Configuration
# =============================================================================

KERNEL_PORT = int(os.environ.get("KERNEL_PORT", "8080"))
WRAPPER_PORT = KERNEL_PORT  # Wrapper takes the main port
INTERNAL_KERNEL_PORT = KERNEL_PORT + 1  # Kernel runs on internal port

CHECKPOINT_DIR = os.environ.get("CRIU_CHECKPOINT_DIR", "/data/criu_checkpoints")
CHECKPOINT_NAME = "kernel_ckpt"
STATE_FILE = os.path.join(CHECKPOINT_DIR, "kernel_state.pkl")

# Global state
kernel_process: Optional[subprocess.Popen] = None
kernel_pid: Optional[int] = None
is_checkpointed = False
evolve_in_progress = False


# =============================================================================
# Kernel Process Management
# =============================================================================

def start_kernel() -> subprocess.Popen:
    """Start the kernel subprocess."""
    global kernel_process, kernel_pid
    
    env = os.environ.copy()
    env["KERNEL_PORT"] = str(INTERNAL_KERNEL_PORT)
    env["KERNEL_HOST"] = "127.0.0.1"
    
    kernel_process = subprocess.Popen(
        [sys.executable, "kernel.py"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    kernel_pid = kernel_process.pid
    
    print(f"[wrapper] Started kernel with PID {kernel_pid} on port {INTERNAL_KERNEL_PORT}", file=sys.stderr)
    
    # Wait for kernel to be ready
    for _ in range(100):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", INTERNAL_KERNEL_PORT, timeout=1)
            conn.request("GET", "/ping")
            resp = conn.getresponse()
            if resp.status == 200:
                print(f"[wrapper] Kernel is ready!", file=sys.stderr)
                conn.close()
                return kernel_process
            conn.close()
        except Exception:
            pass
        time.sleep(0.1)
    
    raise RuntimeError("Kernel failed to start")


def stop_kernel():
    """Stop the kernel subprocess."""
    global kernel_process, kernel_pid
    
    if kernel_process:
        kernel_process.terminate()
        try:
            kernel_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            kernel_process.kill()
        kernel_process = None
        kernel_pid = None
        print(f"[wrapper] Kernel stopped", file=sys.stderr)


def proxy_to_kernel(method: str, path: str, body: bytes = b"", headers: dict = None) -> tuple:
    """Proxy a request to the kernel subprocess."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", INTERNAL_KERNEL_PORT, timeout=30)
        conn.request(method, path, body, headers or {})
        resp = conn.getresponse()
        data = resp.read()
        status = resp.status
        resp_headers = dict(resp.getheaders())
        conn.close()
        return status, data, resp_headers
    except Exception as e:
        return 503, json.dumps({"error": f"Kernel unavailable: {e}"}).encode(), {}


# =============================================================================
# CRIU Operations
# =============================================================================

def criu_check() -> Dict[str, Any]:
    """Check if CRIU is available and working."""
    try:
        result = subprocess.run(
            ["criu", "check"],
            capture_output=True,
            text=True,
            timeout=10
        )
        return {
            "available": result.returncode == 0,
            "version": subprocess.run(["criu", "--version"], capture_output=True, text=True).stdout.strip(),
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except FileNotFoundError:
        return {"available": False, "error": "CRIU not installed"}
    except Exception as e:
        return {"available": False, "error": str(e)}


def save_kernel_state() -> bool:
    """Save kernel state (globals) to disk before checkpoint."""
    try:
        # Execute code in the kernel to save its state using dill
        save_code = f'''
import dill
import os
state = {{k: v for k, v in globals().items() if not k.startswith("__") and k != "runtime"}}
serializable = {{}}
for k, v in state.items():
    try:
        dill.dumps(v)
        serializable[k] = v
    except:
        pass
os.makedirs("{CHECKPOINT_DIR}", exist_ok=True)
with open("{STATE_FILE}", "wb") as f:
    dill.dump(serializable, f)
print(f"Saved {{len(serializable)}} variables")
'''
        conn = http.client.HTTPConnection("127.0.0.1", INTERNAL_KERNEL_PORT, timeout=30)
        body = json.dumps({"code": save_code}).encode()
        conn.request("POST", "/exec", body, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        
        if data.get("status") == "completed":
            print(f"[wrapper] Saved kernel state: {data.get('stdout', '').strip()}", file=sys.stderr)
            return True
        else:
            print(f"[wrapper] Failed to save state: {data.get('error')}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"[wrapper] Error saving state: {e}", file=sys.stderr)
        return False


def load_kernel_state() -> bool:
    """Load kernel state from disk after restore."""
    if not os.path.exists(STATE_FILE):
        print(f"[wrapper] No state file found at {STATE_FILE}", file=sys.stderr)
        return False
    
    try:
        load_code = f'''
import dill
with open("{STATE_FILE}", "rb") as f:
    state = dill.load(f)
globals().update(state)
print(f"Restored {{len(state)}} variables: {{list(state.keys())}}")
'''
        conn = http.client.HTTPConnection("127.0.0.1", INTERNAL_KERNEL_PORT, timeout=30)
        body = json.dumps({"code": load_code}).encode()
        conn.request("POST", "/exec", body, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        
        if data.get("status") == "completed":
            print(f"[wrapper] Loaded kernel state: {data.get('stdout', '').strip()}", file=sys.stderr)
            return True
        else:
            print(f"[wrapper] Failed to load state: {data.get('error')}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"[wrapper] Error loading state: {e}", file=sys.stderr)
        return False


def criu_checkpoint() -> Dict[str, Any]:
    """Checkpoint the kernel process using CRIU."""
    global kernel_process, kernel_pid, is_checkpointed
    
    if kernel_pid is None:
        return {"status": "failed", "error": "No kernel process running"}
    
    # Save state before checkpoint (in case CRIU socket restore fails)
    print(f"[wrapper] Saving kernel state before checkpoint", file=sys.stderr)
    state_saved = save_kernel_state()
    
    # Create checkpoint directory
    ckpt_path = os.path.join(CHECKPOINT_DIR, CHECKPOINT_NAME)
    os.makedirs(ckpt_path, exist_ok=True)
    
    # Clear old checkpoint files (but keep state file)
    for f in os.listdir(ckpt_path):
        if not f.endswith('.pkl'):
            os.remove(os.path.join(ckpt_path, f))
    
    print(f"[wrapper] Checkpointing PID {kernel_pid} to {ckpt_path}", file=sys.stderr)
    
    start = time.time()
    
    try:
        result = subprocess.run(
            [
                "criu", "dump",
                "-t", str(kernel_pid),
                "-D", ckpt_path,
                "--shell-job",
                "--tcp-established",
                "-v4",
                "-o", os.path.join(ckpt_path, "dump.log"),
            ],
            capture_output=True,
            text=True,
            timeout=60
        )
        
        elapsed = int((time.time() - start) * 1000)
        
        if result.returncode != 0:
            # Read the log file for more details
            log_content = ""
            log_path = os.path.join(ckpt_path, "dump.log")
            if os.path.exists(log_path):
                with open(log_path) as f:
                    log_content = f.read()[-2000:]  # Last 2000 chars
            
            return {
                "status": "failed",
                "error": result.stderr or "CRIU dump failed",
                "log": log_content,
                "returncode": result.returncode,
                "timing_ms": elapsed,
            }
        
        # Process is now stopped by CRIU
        kernel_process = None
        kernel_pid = None
        is_checkpointed = True
        
        # Get checkpoint size
        ckpt_size = sum(
            os.path.getsize(os.path.join(ckpt_path, f))
            for f in os.listdir(ckpt_path)
        )
        
        return {
            "status": "completed",
            "checkpoint_path": ckpt_path,
            "checkpoint_size_bytes": ckpt_size,
            "timing_ms": elapsed,
            "message": "Kernel checkpointed. Process stopped. Call /criu/restore to resume.",
        }
        
    except subprocess.TimeoutExpired:
        return {"status": "failed", "error": "CRIU dump timed out"}
    except Exception as e:
        return {"status": "failed", "error": str(e)}


def criu_restore() -> Dict[str, Any]:
    """Restore the kernel process from CRIU checkpoint."""
    global kernel_process, kernel_pid, is_checkpointed
    
    ckpt_path = os.path.join(CHECKPOINT_DIR, CHECKPOINT_NAME)
    
    if not os.path.exists(ckpt_path) or not os.listdir(ckpt_path):
        return {"status": "failed", "error": f"No checkpoint found at {ckpt_path}"}
    
    # Check if kernel process already exists (from previous restore)
    try:
        pgrep = subprocess.run(["pgrep", "-f", "kernel.py"], capture_output=True, text=True)
        if pgrep.stdout.strip():
            existing_pid = int(pgrep.stdout.strip().split()[0])
            print(f"[wrapper] Killing existing kernel PID {existing_pid}", file=sys.stderr)
            subprocess.run(["kill", "-9", str(existing_pid)], capture_output=True)
            time.sleep(0.5)
    except Exception:
        pass
    
    print(f"[wrapper] Restoring from {ckpt_path}", file=sys.stderr)
    
    start = time.time()
    
    try:
        # Run CRIU restore in background
        result = subprocess.run(
            [
                "criu", "restore",
                "-D", ckpt_path,
                "--shell-job",
                "--tcp-established",
                "-v4",
                "-o", os.path.join(ckpt_path, "restore.log"),
                "-d",  # Detach (daemonize)
            ],
            capture_output=True,
            text=True,
            timeout=60
        )
        
        elapsed = int((time.time() - start) * 1000)
        
        if result.returncode != 0:
            log_content = ""
            log_path = os.path.join(ckpt_path, "restore.log")
            if os.path.exists(log_path):
                with open(log_path) as f:
                    log_content = f.read()[-2000:]
            
            return {
                "status": "failed",
                "error": result.stderr or "CRIU restore failed",
                "log": log_content,
                "returncode": result.returncode,
                "timing_ms": elapsed,
            }
        
        # CRIU restore succeeded - find the restored process
        time.sleep(0.5)
        
        # Find the restored process by looking for kernel.py
        try:
            pgrep = subprocess.run(["pgrep", "-f", "kernel.py"], capture_output=True, text=True)
            if pgrep.stdout.strip():
                kernel_pid = int(pgrep.stdout.strip().split()[0])
                print(f"[wrapper] Restored kernel PID: {kernel_pid}", file=sys.stderr)
                
                # Send SIGUSR1 to tell kernel to restart its HTTP server
                print(f"[wrapper] Sending SIGUSR1 to kernel to restart HTTP server", file=sys.stderr)
                subprocess.run(["kill", "-USR1", str(kernel_pid)], capture_output=True)
                time.sleep(1)  # Wait for server to restart
        except Exception as e:
            print(f"[wrapper] Error finding/signaling kernel: {e}", file=sys.stderr)
        
        # Mark as not checkpointed since CRIU succeeded
        is_checkpointed = False
        
        # Verify kernel is responding
        for i in range(50):
            try:
                conn = http.client.HTTPConnection("127.0.0.1", INTERNAL_KERNEL_PORT, timeout=1)
                conn.request("GET", "/ping")
                resp = conn.getresponse()
                if resp.status == 200:
                    conn.close()
                    print(f"[wrapper] Kernel responding after {i+1} attempts", file=sys.stderr)
                    return {
                        "status": "completed",
                        "kernel_pid": kernel_pid,
                        "timing_ms": int((time.time() - start) * 1000),
                        "message": "Kernel restored and running!",
                    }
                conn.close()
            except Exception:
                pass
            time.sleep(0.1)
        
        # CRIU restored the process but socket is broken (known limitation)
        # Kill the zombie kernel and start fresh, then restore state from saved file
        print(f"[wrapper] Kernel socket broken after CRIU restore, starting fresh kernel", file=sys.stderr)
        
        if kernel_pid:
            try:
                subprocess.run(["kill", "-9", str(kernel_pid)], capture_output=True)
                time.sleep(0.5)
            except Exception:
                pass
        
        # Start fresh kernel and restore state
        try:
            start_kernel()
            
            # Load saved state into the fresh kernel
            state_loaded = load_kernel_state()
            
            return {
                "status": "completed",
                "kernel_pid": kernel_pid,
                "timing_ms": int((time.time() - start) * 1000),
                "message": "Kernel restored with state!" if state_loaded else "Fresh kernel started (no saved state).",
                "state_restored": state_loaded,
            }
        except Exception as e:
            return {
                "status": "failed",
                "error": f"Could not start fresh kernel: {e}",
                "timing_ms": int((time.time() - start) * 1000),
            }
        
    except subprocess.TimeoutExpired:
        return {"status": "failed", "error": "CRIU restore timed out"}
    except Exception as e:
        return {"status": "failed", "error": str(e)}


# =============================================================================
# Evolve Operations
# =============================================================================

def run_evolve() -> Dict[str, Any]:
    """Run the evolve script in a subprocess. Returns output and status."""
    global evolve_in_progress

    if evolve_in_progress:
        return {"status": "already_running", "error": "An evolve is already in progress"}

    evolve_script = "/repo/scripts/evolve.sh"
    if not os.path.isfile(evolve_script):
        return {"status": "failed", "error": f"{evolve_script} not found. Is /repo mounted?"}

    evolve_in_progress = True
    try:
        result = subprocess.run(
            ["sh", evolve_script],
            capture_output=True,
            text=True,
            timeout=600,
            env=os.environ.copy(),
        )
        return {
            "status": "completed" if result.returncode == 0 else "failed",
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"status": "failed", "error": "Evolve timed out after 600s"}
    except Exception as e:
        return {"status": "failed", "error": str(e)}
    finally:
        evolve_in_progress = False


def get_evolve_status() -> Dict[str, Any]:
    """Check evolve state and latest run info."""
    runs_dir = "/repo/.memory/runs"
    latest_run = None
    latest_status = None

    if os.path.isdir(runs_dir):
        runs = sorted(os.listdir(runs_dir), reverse=True)
        for run_name in runs:
            run_path = os.path.join(runs_dir, run_name)
            if os.path.isdir(run_path):
                latest_run = run_name
                status_file = os.path.join(run_path, "status")
                if os.path.isfile(status_file):
                    with open(status_file) as f:
                        latest_status = f.read().strip()
                break

    return {
        "evolve_in_progress": evolve_in_progress,
        "restart_pending": os.path.isfile("/repo/.memory/restart_requested"),
        "latest_run": latest_run,
        "latest_status": latest_status,
    }


# =============================================================================
# HTTP Handler
# =============================================================================

class WrapperRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler that proxies to kernel and handles CRIU endpoints."""
    
    protocol_version = "HTTP/1.1"
    
    def log_message(self, format: str, *args) -> None:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {args[0]}", file=sys.stderr)
    
    def _send_json(self, data: Dict[str, Any], status_code: int = 200) -> None:
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
    
    def _read_body(self) -> bytes:
        content_length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(content_length) if content_length else b""
    
    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()
    
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        
        # CRIU-specific endpoints
        if path == "/criu/status":
            self._send_json({
                "criu": criu_check(),
                "kernel_pid": kernel_pid,
                "is_checkpointed": is_checkpointed,
                "checkpoint_dir": CHECKPOINT_DIR,
            })
            return

        if path == "/evolve/status":
            self._send_json(get_evolve_status())
            return

        if path == "/chat/history":
            history_path = "/repo/.memory/chat_history.json"
            try:
                if os.path.isfile(history_path):
                    with open(history_path) as f:
                        self._send_json({"messages": json.load(f)})
                else:
                    self._send_json({"messages": []})
            except Exception as e:
                self._send_json({"messages": [], "error": str(e)})
            return

        if path == "/chat/log":
            log_path = "/repo/.memory/tool_log.jsonl"
            try:
                if os.path.isfile(log_path):
                    with open(log_path) as f:
                        lines = f.readlines()
                    entries = []
                    for line in lines:
                        line = line.strip()
                        if line:
                            entries.append(json.loads(line))
                    self._send_json({"entries": entries, "count": len(entries)})
                else:
                    self._send_json({"entries": [], "count": 0})
            except Exception as e:
                self._send_json({"entries": [], "count": 0, "error": str(e)})
            return

        if path == "/digest/learnings":
            learnings_path = "/repo/.memory/learnings.md"
            try:
                if os.path.isfile(learnings_path):
                    with open(learnings_path) as f:
                        content = f.read()
                    self._send_json({"learnings": content, "exists": True})
                else:
                    self._send_json({"learnings": "", "exists": False})
            except Exception as e:
                self._send_json({"learnings": "", "exists": False, "error": str(e)})
            return

        if path == "/":
            self._send_json({
                "service": "inner-kernel-criu-wrapper",
                "kernel_pid": kernel_pid,
                "is_checkpointed": is_checkpointed,
                "endpoints": {
                    "POST /chat": "Chat with LLM agent (has access to all MCP tools + kernel)",
                    "POST /exec": "Execute Python code",
                    "POST /shell": "Run shell command",
                    "POST /shell/cd": "Set shell cwd",
                    "POST /shell/env": "Set shell env var",
                    "POST /reset": "Clear namespace",
                    "POST /cryo/store": "Store context (dill)",
                    "POST /cryo/reload": "Reload context (dill)",
                    "POST /criu/checkpoint": "CRIU checkpoint (freeze process)",
                    "POST /criu/restore": "CRIU restore (resume process)",
                    "POST /evolve": "Trigger self-evolve (build + restart)",
                    "POST /digest": "Trigger sleep digest — process experiences, extract learnings",
                    "GET /chat/history": "Get conversation history",
                    "GET /chat/log": "Get tool call log (every tool execution with timestamp, args, result)",
                    "GET /digest/learnings": "Get accumulated learnings from sleep digestion",
                    "GET /criu/status": "CRIU status",
                    "GET /evolve/status": "Evolve status and latest run info",
                    "GET /ping": "Health check",
                    "GET /status": "Kernel status",
                }
            })
            return
        
        # Proxy to kernel
        if is_checkpointed:
            self._send_json({
                "error": "Kernel is checkpointed. Call POST /criu/restore first.",
                "is_checkpointed": True,
            }, 503)
            return
        
        status, data, headers = proxy_to_kernel("GET", path)
        self.send_response(status)
        for k, v in headers.items():
            if k.lower() not in ("transfer-encoding", "connection"):
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    
    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        body = self._read_body()
        
        # Cryo (dill state) and CRIU endpoints
        if path == "/cryo/store":
            ok = save_kernel_state()
            self._send_json({"status": "completed" if ok else "failed", "message": "State saved" if ok else "Save failed"})
            return

        if path == "/cryo/reload":
            global is_checkpointed
            # If kernel checkpointed or dead, start fresh first
            if is_checkpointed or not kernel_pid:
                try:
                    start_kernel()
                    is_checkpointed = False
                except Exception as e:
                    self._send_json({"status": "failed", "error": str(e)}, 500)
                    return
            ok = load_kernel_state()
            self._send_json({"status": "completed" if ok else "failed", "message": "State restored" if ok else "No saved state or load failed"})
            return

        if path == "/criu/checkpoint":
            result = criu_checkpoint()
            status = 200 if result.get("status") == "completed" else 500
            self._send_json(result, status)
            return

        if path == "/criu/restore":
            result = criu_restore()
            status = 200 if result.get("status") == "completed" else 500
            self._send_json(result, status)
            return

        if path == "/evolve":
            self._send_json({"status": "started", "message": "Evolve triggered. Connection will drop when container restarts."})
            threading.Thread(target=run_evolve, daemon=True).start()
            return

        if path == "/chat":
            try:
                req = json.loads(body.decode("utf-8")) if body else {}
            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            user_message = req.get("message", "")
            if not user_message:
                self._send_json({"error": "Missing 'message' field"}, 400)
                return
            provider = req.get("provider")
            reset = req.get("reset", False)
            try:
                import importlib, sys
                if 'agent' in sys.modules:
                    del sys.modules['agent']
                import agent as _agent_mod
                handle_chat = _agent_mod.handle_chat
                loop = asyncio.new_event_loop()
                result = loop.run_until_complete(handle_chat(user_message, provider=provider, reset=reset))
                loop.close()
                self._send_json(result)
            except Exception as e:
                import traceback
                self._send_json({"error": str(e), "traceback": traceback.format_exc()}, 500)
            return

        if path == "/digest":
            provider = None
            replay_ratio = None
            try:
                if body:
                    req = json.loads(body.decode("utf-8"))
                    provider = req.get("provider")
                    replay_ratio = req.get("replay_ratio")
            except json.JSONDecodeError:
                pass
            try:
                import importlib, sys
                if 'agent' in sys.modules:
                    del sys.modules['agent']
                import agent as _agent_mod
                handle_digest = _agent_mod.handle_digest
                loop = asyncio.new_event_loop()
                result = loop.run_until_complete(
                    handle_digest(provider=provider, replay_ratio=replay_ratio)
                )
                loop.close()
                self._send_json(result)
            except Exception as e:
                import traceback
                self._send_json({"error": str(e), "traceback": traceback.format_exc()}, 500)
            return

        # Proxy to kernel
        if is_checkpointed:
            self._send_json({
                "error": "Kernel is checkpointed. Call POST /criu/restore first.",
                "is_checkpointed": True,
            }, 503)
            return
        
        headers = {"Content-Type": self.headers.get("Content-Type", "application/json")}
        status, data, resp_headers = proxy_to_kernel("POST", path, body, headers)
        self.send_response(status)
        for k, v in resp_headers.items():
            if k.lower() not in ("transfer-encoding", "connection"):
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ThreadedHTTPServer(HTTPServer):
    """HTTP server with threading."""
    
    def process_request(self, request, client_address):
        thread = threading.Thread(target=self._handle, args=(request, client_address))
        thread.daemon = True
        thread.start()
    
    def _handle(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


# =============================================================================
# Main
# =============================================================================

def main():
    # Check CRIU availability
    criu_info = criu_check()
    print(f"[wrapper] CRIU available: {criu_info.get('available')}", file=sys.stderr)
    if criu_info.get("available"):
        print(f"[wrapper] CRIU version: {criu_info.get('version')}", file=sys.stderr)
    else:
        print(f"[wrapper] CRIU error: {criu_info.get('error', 'unknown')}", file=sys.stderr)
        print(f"[wrapper] CRIU endpoints will not work, but kernel will still run", file=sys.stderr)
    
    # Start kernel
    start_kernel()
    
    # Start wrapper server
    server = ThreadedHTTPServer(("0.0.0.0", WRAPPER_PORT), WrapperRequestHandler)
    print(f"[wrapper] CRIU Wrapper running on http://0.0.0.0:{WRAPPER_PORT}", file=sys.stderr)
    print(f"[wrapper] Endpoints:", file=sys.stderr)
    print(f"[wrapper]   POST /criu/checkpoint - Checkpoint kernel", file=sys.stderr)
    print(f"[wrapper]   POST /criu/restore    - Restore kernel", file=sys.stderr)
    print(f"[wrapper]   GET  /criu/status     - CRIU status", file=sys.stderr)
    print(f"[wrapper]   + all kernel endpoints proxied", file=sys.stderr)
    
    def shutdown(sig, frame):
        print("\n[wrapper] Shutting down...", file=sys.stderr)
        stop_kernel()
        server.shutdown()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        shutdown(None, None)


if __name__ == "__main__":
    main()
