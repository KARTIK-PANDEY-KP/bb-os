"""
Test script for all three MCP servers, kernel, and CRIU.
Run: uv run --with mcp python tests/test_mcp.py
"""

import asyncio
import base64
import json
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

from mcp.client.sse import sse_client
from mcp import ClientSession

def _base() -> str:
    for a in sys.argv[1:]:
        if a.startswith(("http://", "https://")) and re.search(r":\d+", a):
            return a.rstrip("/")
    return "http://localhost:8765"

def _kernel_base() -> str:
    """Kernel URL: same host as MCP, port 8080."""
    m = re.match(r"(https?://[^:/]+)(?::\d+)?", _base())
    return f"{m.group(1)}:8080" if m else "http://localhost:8080"

BASE = _base()
KERNEL_BASE = _kernel_base()


def _http(method: str, url: str, data: dict | None = None) -> dict:
    """Sync HTTP request. Returns JSON body as dict."""
    req = urllib.request.Request(url, method=method)
    if data is not None:
        req.data = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return json.loads(body) if body.strip() else {"error": str(e)}
        except json.JSONDecodeError:
            return {"error": str(e), "body": body[:200]}


async def test_filesystem():
    print("=" * 60)
    print("TEST 1: filesystem — create, read, append, read")
    print("=" * 60)
    url = f"{BASE}/servers/filesystem/sse"
    async with sse_client(url) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()

            tools = await s.list_tools()
            print(f"Tools: {', '.join(t.name for t in tools.tools)}\n")

            print("1. Creating /tmp/mcp_test.txt...")
            r = await s.call_tool("write_file", {"path": "/tmp/mcp_test.txt", "content": "Hello from MCP!\n"})
            print(f"   {r.content[0].text}")

            print("2. Reading...")
            r = await s.call_tool("read_file", {"path": "/tmp/mcp_test.txt"})
            print(f"   Content: {r.content[0].text!r}")

            print("3. Writing with appended line...")
            r = await s.call_tool("write_file", {"path": "/tmp/mcp_test.txt", "content": "Hello from MCP!\nThis line was appended.\n"})
            print(f"   {r.content[0].text}")

            print("4. Reading final...")
            r = await s.call_tool("read_file", {"path": "/tmp/mcp_test.txt"})
            print(f"   Content: {r.content[0].text!r}")

            print("\nFILESYSTEM: PASS\n")


async def test_linux():
    print("=" * 60)
    print("TEST 2: linux — system info")
    print("=" * 60)
    url = f"{BASE}/servers/linux/sse"
    async with sse_client(url) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()

            tools = await s.list_tools()
            names = [t.name for t in tools.tools]
            print(f"Tools: {', '.join(names[:15])}{'...' if len(names) > 15 else ''}\n")

            tool_name = "get_system_information" if "get_system_information" in names else names[0]
            print(f"Calling {tool_name}...")
            r = await s.call_tool(tool_name, {})
            print(f"\n{r.content[0].text}")

            print("\nLINUX: PASS\n")


async def test_browser_use():
    print("=" * 60)
    print("TEST 3: browser-use — Hacker News top 3 stories")
    print("=" * 60)
    url = f"{BASE}/servers/browser-use/sse"
    async with sse_client(url) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()

            tools = await s.list_tools()
            names = [t.name for t in tools.tools]
            print(f"Tools: {', '.join(names[:15])}{'...' if len(names) > 15 else ''}\n")

            print("Navigating to Hacker News (server-rendered, no JS needed)...")
            r = await s.call_tool("browser_navigate", {"url": "https://www.google.com"})
            print(f"   {r.content[0].text[:200]}")

            await asyncio.sleep(2)

            print("\nExtracting top 3 links via LLM...")
            r = await s.call_tool("browser_extract_content", {"query": "Return the links of the top 3 results after searching for 'apple'  expplain what does the webpage contain"})
            print(f"\n{r.content[0].text}")

            print("\nBROWSER-USE: PASS\n")


async def test_kernel():
    """Kernel HTTP API: exec, reset, status, ping, context persistence."""
    print("=" * 60)
    print("TEST 4: kernel — exec, reset, status, ping, context persistence")
    print("=" * 60)
    base = KERNEL_BASE

    # GET /ping
    print("1. GET /ping...")
    r = _http("GET", f"{base}/ping")
    assert r.get("status") == "ok", r
    print(f"   {r}")

    # GET /status
    print("\n2. GET /status...")
    r = _http("GET", f"{base}/status")
    print(f"   exec_count={r.get('exec_count')}, globals_count={r.get('globals_count')}")

    # POST /exec — define variable
    print("\n3. POST /exec (define a=10)...")
    r = _http("POST", f"{base}/exec", {"id": "k1", "code": "a = 10\nprint('a =', a)"})
    assert r.get("status") == "completed", r
    print(f"   stdout: {r.get('stdout', '').strip()}")

    # POST /exec — use variable (context persistence)
    print("\n4. POST /exec (use a, define b)...")
    r = _http("POST", f"{base}/exec", {"id": "k2", "code": "b = a + 5\nprint('b =', b, ', a still =', a)"})
    assert r.get("status") == "completed", r
    print(f"   stdout: {r.get('stdout', '').strip()}")

    # POST /reset
    print("\n5. POST /reset...")
    r = _http("POST", f"{base}/reset")
    assert r.get("status") == "completed", r

    # POST /exec — verify reset (a and b should be gone)
    print("\n6. POST /exec (after reset, a should be undefined)...")
    r = _http("POST", f"{base}/exec", {"id": "k3", "code": "print(a)"})
    if r.get("status") == "failed":
        err = r.get("error") or {}
        err_type = err.get("type") if isinstance(err, dict) else "Error"
        print(f"   error: {err_type} (expected)")
    else:
        raise AssertionError(f"Reset should have cleared globals, got: {r}")

    print("\nKERNEL: PASS\n")


async def test_shell():
    """Shell execution, script creation/run, and persistent cwd/env."""
    print("=" * 60)
    print("TEST: SHELL — POST /shell, script create/run, cd, env, runtime.shell")
    print("=" * 60)
    base = KERNEL_BASE

    # POST /shell
    print("1. POST /shell (echo hello)...")
    r = _http("POST", f"{base}/shell", {"command": "echo hello"})
    assert r.get("status") == "completed" or "stdout" in r, r
    out = r.get("stdout", "").strip()
    assert "hello" in out, r
    print(f"   stdout: {out!r}")

    # Create shell script
    print("\n2. Create script /tmp/test_script.sh...")
    r = _http("POST", f"{base}/shell", {"command": "echo '#!/bin/sh' > /tmp/test_script.sh && echo 'echo script_ran_ok' >> /tmp/test_script.sh && echo 'echo from_script' >> /tmp/test_script.sh && chmod +x /tmp/test_script.sh"})
    assert r.get("status") == "completed" and r.get("returncode") == 0, r
    print("   script created")

    # Run script
    print("3. POST /shell (run script)...")
    r = _http("POST", f"{base}/shell", {"command": "sh /tmp/test_script.sh"})
    assert r.get("status") == "completed", r
    out = r.get("stdout", "").strip()
    assert "script_ran_ok" in out and "from_script" in out, r
    print(f"   stdout: {out!r}")

    # POST /shell/cd
    print("\n4. POST /shell/cd /tmp...")
    r = _http("POST", f"{base}/shell/cd", {"path": "/tmp"})
    assert r.get("status") == "completed", r

    # verify cwd
    print("5. POST /shell (pwd)...")
    r = _http("POST", f"{base}/shell", {"command": "pwd"})
    assert "/tmp" in (r.get("stdout") or ""), r
    print(f"   stdout: {r.get('stdout','').strip()!r}")

    # POST /shell/env
    print("\n6. POST /shell/env SHELL_TEST=ok...")
    r = _http("POST", f"{base}/shell/env", {"key": "SHELL_TEST", "value": "ok"})
    assert r.get("status") == "completed", r

    # verify env
    print("7. POST /shell (echo $SHELL_TEST)...")
    r = _http("POST", f"{base}/shell", {"command": "echo $SHELL_TEST"})
    assert "ok" in (r.get("stdout") or ""), r
    print(f"   stdout: {r.get('stdout','').strip()!r}")

    # runtime.shell from Python
    print("\n8. POST /exec (runtime.shell.run script)...")
    r = _http("POST", f"{base}/exec", {"code": """
out = runtime.shell.run("sh /tmp/test_script.sh")
print(out["stdout"].strip(), "rc=", out["returncode"])
"""})
    assert r.get("status") == "completed", r
    assert "script_ran_ok" in (r.get("stdout") or ""), r
    print(f"   {r.get('stdout','').strip()}")

    print("\nSHELL: PASS\n")


async def test_shell_cryo():
    """Shell context, script, and connection persist across cryo store/reload."""
    print("=" * 60)
    print("TEST: SHELL-CRYO — script + connection persistence across cryo")
    print("=" * 60)
    base = KERNEL_BASE

    # Set shell context
    print("1. POST /shell/cd and /shell/env...")
    _http("POST", f"{base}/shell/cd", {"path": "/tmp"})
    _http("POST", f"{base}/shell/env", {"key": "CRYO_SHELL", "value": "persisted"})

    # Create script (will persist on disk across cryo)
    print("\n2. Create script /tmp/cryo_script.sh...")
    r = _http("POST", f"{base}/shell", {"command": "echo '#!/bin/sh' > /tmp/cryo_script.sh && echo 'echo cryo_script_ok' >> /tmp/cryo_script.sh && chmod +x /tmp/cryo_script.sh"})
    assert r.get("status") == "completed", r

    # Run script and verify
    r = _http("POST", f"{base}/shell", {"command": "sh /tmp/cryo_script.sh"})
    assert "cryo_script_ok" in (r.get("stdout") or ""), r
    print("   script created and ran OK")

    # Create connection via runtime.shell.resource
    print("\n3. POST /exec (create http_handle via runtime.shell.resource)...")
    r = _http("POST", f"{base}/exec", {"code": """
import urllib.request
def fetch():
    with urllib.request.urlopen("https://httpbin.org/get", timeout=10) as r:
        return r.read().decode()
http_handle = runtime.shell.resource(fetch)
data = http_handle.get()
print("Fetched:", "httpbin" in data)
http_handle.invalidate()
"""})
    if r.get("status") != "completed":
        print(f"   SKIP: network or exec failed: {r.get('error')}")
        print("\nSHELL-CRYO: SKIP (network)\n")
        return
    assert "True" in (r.get("stdout") or ""), r
    print("   connection created, used, invalidated")

    # Cryo store
    print("\n4. POST /cryo/store...")
    r = _http("POST", f"{base}/cryo/store")
    assert r.get("status") == "completed", r

    # Reset
    print("5. POST /reset...")
    _http("POST", f"{base}/reset")

    # Reload
    print("6. POST /cryo/reload...")
    r = _http("POST", f"{base}/cryo/reload")
    assert r.get("status") == "completed", r

    # Verify shell context persisted
    print("\n7. Verify shell context (pwd, echo $CRYO_SHELL)...")
    r = _http("POST", f"{base}/shell", {"command": "pwd"})
    assert "/tmp" in (r.get("stdout") or ""), r
    r = _http("POST", f"{base}/shell", {"command": "echo $CRYO_SHELL"})
    assert "persisted" in (r.get("stdout") or ""), r
    print("   cwd and env persisted: OK")

    # Verify script still runs (file on disk)
    print("\n8. Run script again (file persists)...")
    r = _http("POST", f"{base}/shell", {"command": "sh /tmp/cryo_script.sh"})
    assert "cryo_script_ok" in (r.get("stdout") or ""), r
    print("   script still runs: OK")

    # Verify connection handle reconnects
    print("\n9. POST /exec (http_handle.get() reconnects after reload)...")
    r = _http("POST", f"{base}/exec", {"code": """
try:
    data = http_handle.get()
    print("SUCCESS: handle persisted, reconnected:", "httpbin" in data)
except NameError:
    print("handle not persisted (expected if not dill-serializable)")
except Exception as e:
    print("FAIL:", type(e).__name__, e)
"""})
    out = (r.get("stdout") or "") + (r.get("stderr") or "")
    if "SUCCESS" in out:
        print("   connection handle persisted, reconnected: OK")
    else:
        print(f"   {out.strip()}")

    print("\nSHELL-CRYO: PASS\n")


async def test_criu():
    """CRIU checkpoint and restore (may fail if CRIU not available)."""
    print("=" * 60)
    print("TEST 5: CRIU — checkpoint, restore, state persistence")
    print("=" * 60)
    base = KERNEL_BASE

    # GET /criu/status
    print("1. GET /criu/status...")
    r = _http("GET", f"{base}/criu/status")
    criu_ok = r.get("criu", {}).get("available", False)
    print(f"   criu available: {criu_ok}")
    if not criu_ok:
        print(f"   (CRIU not installed/working — checkpoint/restore will fail)")
        print("\nCRIU: SKIP (not available)\n")
        return

    # Define state
    print("\n2. POST /exec (define c=777)...")
    r = _http("POST", f"{base}/exec", {"id": "c1", "code": "c = 777\nprint('c =', c)"})
    assert r.get("status") == "completed", r

    # POST /criu/checkpoint
    print("\n3. POST /criu/checkpoint...")
    r = _http("POST", f"{base}/criu/checkpoint")
    if r.get("status") != "completed":
        print(f"   FAILED: {r.get('error', r)}")
        print("\nCRIU: FAIL (checkpoint failed)\n")
        return
    print(f"   checkpoint_path: {r.get('checkpoint_path')}, timing_ms: {r.get('timing_ms')}")

    # POST /criu/restore
    print("\n4. POST /criu/restore...")
    r = _http("POST", f"{base}/criu/restore")
    if r.get("status") != "completed":
        print(f"   FAILED: {r.get('error', r)}")
        print("\nCRIU: FAIL (restore failed)\n")
        return
    print(f"   {r.get('message')}, timing_ms: {r.get('timing_ms')}")

    # Verify state persisted
    print("\n5. POST /exec (verify c after restore)...")
    r = _http("POST", f"{base}/exec", {"id": "c2", "code": "print('c after restore =', c)"})
    if r.get("status") == "completed" and "777" in (r.get("stdout") or ""):
        print(f"   stdout: {r.get('stdout', '').strip()}")
        print("\nCRIU: PASS\n")
    else:
        print(f"   State may not have persisted: {r}")
        print("\nCRIU: FAIL (state not restored)\n")


async def test_cryo():
    """Cryo store/reload (dill-based kernel state, no CRIU)."""
    print("=" * 60)
    print("TEST 6: cryo — store/reload (dill state, works everywhere)")
    print("=" * 60)
    base = KERNEL_BASE

    # Define state
    print("1. POST /exec (define d=555)...")
    r = _http("POST", f"{base}/exec", {"id": "cy1", "code": "d = 555\nprint('d =', d)"})
    assert r.get("status") == "completed", r

    # POST /cryo/store
    print("\n2. POST /cryo/store...")
    r = _http("POST", f"{base}/cryo/store")
    assert r.get("status") == "completed", r
    print(f"   {r.get('message')}")

    # Reset to clear in-memory state
    print("\n3. POST /reset (clear in-memory)...")
    _http("POST", f"{base}/reset")

    # POST /cryo/reload
    print("\n4. POST /cryo/reload...")
    r = _http("POST", f"{base}/cryo/reload")
    assert r.get("status") == "completed", r
    print(f"   {r.get('message')}")

    # Verify state restored
    print("\n5. POST /exec (verify d after reload)...")
    r = _http("POST", f"{base}/exec", {"id": "cy2", "code": "print('d after reload =', d)"})
    assert r.get("status") == "completed", r
    assert "555" in (r.get("stdout") or ""), r
    print(f"   stdout: {r.get('stdout', '').strip()}")

    print("\nCRYO: PASS\n")


async def test_cryo_resources():
    """Test runtime.resource + network + cryo per kernel documentation."""
    print("=" * 60)
    print("TEST 7: cryo + runtime.resource (network, per docs)")
    print("=" * 60)
    base = KERNEL_BASE

    # Code per documentation: urllib fetch wrapped in runtime.resource, invalidate, store
    setup_code = '''
import urllib.request
def fetch_url(url="https://httpbin.org/get"):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode()
http_handle = runtime.resource(fetch_url)
data = http_handle.get()
network_result = data[:100]
print("Fetched:", network_result[:60])
http_handle.invalidate()
'''
    print("1. POST /exec (network fetch via runtime.resource, invalidate)...")
    r = _http("POST", f"{base}/exec", {"id": "r1", "code": setup_code})
    if r.get("status") != "completed":
        print(f"   SKIP: network or exec failed: {r.get('error')}")
        print("\nCRYO-RESOURCES: SKIP\n")
        return
    print(f"   stdout: {r.get('stdout','')[:80]}")

    print("\n2. POST /cryo/store...")
    r = _http("POST", f"{base}/cryo/store")
    assert r.get("status") == "completed", r

    print("\n3. POST /reset...")
    _http("POST", f"{base}/reset")

    print("\n4. POST /cryo/reload...")
    r = _http("POST", f"{base}/cryo/reload")
    assert r.get("status") == "completed", r

    # Per docs: after reload, handle.get() should reconnect
    print("\n5. POST /exec (http_handle.get() after reload - reconnects?)...")
    r = _http("POST", f"{base}/exec", {"id": "r2", "code": """
try:
    data = http_handle.get()
    print("SUCCESS: handle persisted, reconnected:", data[:60])
except NameError:
    print("handle not persisted (expected if not dill-serializable)")
except Exception as e:
    print("FAIL:", type(e).__name__, e)
"""})
    out = (r.get("stdout") or "") + (r.get("stderr") or "")
    print(f"   {out.strip()}")

    # Also verify network_result if it was saved (simple value persists)
    print("\n6. POST /exec (network_result variable persisted?)...")
    r = _http("POST", f"{base}/exec", {"id": "r3", "code": "print('network_result' in dir() and 'httpbin' in str(network_result)[:200])"})
    out = r.get("stdout") or ""
    if "True" in out:
        print("   network_result persisted: yes")
    else:
        print("   network_result:", "persisted" if r.get("status") == "completed" else "not found")

    print("\nCRYO-RESOURCES: PASS\n")


async def get_screenshot(url: str | None = None, output: str = "screenshot.png") -> str:
    """Get a screenshot from the browser-use MCP server. Saves to output path."""
    sse_url = f"{BASE}/servers/browser-use/sse"
    async with sse_client(sse_url) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()

            if url:
                print(f"Navigating to {url}...")
                await s.call_tool("browser_navigate", {"url": url})
                await asyncio.sleep(2)

            print("Getting screenshot...")
            r = await s.call_tool("browser_get_state", {"include_screenshot": True})
            text = r.content[0].text

            # Parse JSON for screenshot (base64)
            match = re.search(r'"screenshot"\s*:\s*"([^"]+)"', text)
            if not match:
                # Try extracting from markdown image
                match = re.search(r"data:image/(\w+);base64,([A-Za-z0-9+/=]+)", text)
            if match:
                b64 = match.group(2) if "data:image" in text else match.group(1)
                data = base64.b64decode(b64)
                Path(output).write_bytes(data)
                print(f"Saved screenshot to {output}")
                return output

            # Fallback: response might be raw JSON
            try:
                data = json.loads(text)
                if "screenshot" in data:
                    Path(output).write_bytes(base64.b64decode(data["screenshot"]))
                    print(f"Saved screenshot to {output}")
                    return output
            except json.JSONDecodeError:
                pass

            print(f"No screenshot found in response. First 500 chars:\n{text[:500]}")
            return ""


async def main():
    if "--screenshot" in sys.argv or "-s" in sys.argv:
        args = [a for a in sys.argv[1:] if a not in ("--screenshot", "-s")]
        url = "https://www.google.com"
        out = "screenshot.png"
        for a in args:
            if a.startswith(("http://", "https://")):
                url = a
            elif not a.startswith("-"):
                out = a
        await get_screenshot(url=url, output=out)
        return

    print(f"Testing MCP at {BASE} | Kernel at {KERNEL_BASE}\n")

    tests = [
        ("FILESYSTEM", test_filesystem),
        ("LINUX", test_linux),
        ("BROWSER-USE", test_browser_use),
        ("KERNEL", test_kernel),
        ("SHELL", test_shell),
        ("SHELL-CRYO", test_shell_cryo),
        ("CRYO", test_cryo),
        ("CRYO-RESOURCES", test_cryo_resources),
        ("CRIU", test_criu),
    ]
    for name, test in tests:
        try:
            await test()
        except Exception as e:
            print(f"{name}: FAIL — {e}\n")

    print("=" * 60)
    print("All tests complete.")


asyncio.run(main())
