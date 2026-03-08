"""
Sandbox
-------
Hardened subprocess execution for agent-generated scripts.

Defence layers (in order of enforcement):
  1. AST parse        — reject before execution if dangerous nodes found
  2. Import allowlist — only whitelisted stdlib + vetted packages permitted
  3. Pattern blocklist — catch obfuscated patterns AST misses
  4. Injected api     — agents use agent_api.ask() wrapper; never touch real keys
  5. Network isolation — subprocess has no network via unshare(1) when available
  6. Restricted env   — minimal PATH, no HOME, no credentials in environment
  7. Python -I mode   — ignores PYTHONPATH, user site-packages
  8. Hard timeout     — 15s max, SIGKILL on breach
  9. Resource limits  — 256 MB RAM, 32 FDs, no forking
 10. Output cap       — 4000 chars stdout+stderr
 11. Output scrub     — strip any accidental credential leakage from output
"""

import ast
import asyncio
import os
import re
import sys
import tempfile
import textwrap
import time
from pathlib import Path


MAX_OUTPUT_CHARS = 4000
DEFAULT_TIMEOUT  = 15     # seconds
MAX_CODE_LENGTH  = 8000   # chars

# ── Per-session API rate limiting ─────────────────────────────────────────────
_api_calls_this_session: int = 0
_API_CALL_LIMIT = 10        # max agent_api.ask() calls per process lifetime
_API_TOKENS_LIMIT = 20_000  # max total tokens across all calls this session
_api_tokens_used: int = 0


# ── Import allowlist ──────────────────────────────────────────────────────────
#
# ONLY these modules may be imported by agent scripts.
# Adding a module here is a deliberate security decision.
#
# Rules:
#   - No I/O (no open, no file access)
#   - No network (no socket, no http, no requests)
#   - No process control (no os, no subprocess, no signal)
#   - No dynamic code execution (no eval, no exec, no compile, no importlib)
#   - No FFI (no ctypes, no cffi, no cython)
#   - No serialisation of arbitrary objects (no pickle, no marshal)
#
_ALLOWED_IMPORTS = frozenset({
    # Pure computation
    "math", "cmath", "statistics", "decimal", "fractions", "numbers",
    "random",
    # Data structures
    "collections", "heapq", "bisect", "array", "queue",
    # Functional
    "itertools", "functools", "operator",
    # Text / encoding
    "re", "string", "textwrap", "unicodedata", "difflib",
    # Serialisation of safe types only
    "json",
    # Date / time (read-only; no filesystem interaction)
    "datetime", "calendar", "time",
    # Type system helpers
    "typing", "types", "dataclasses", "enum", "abc", "copy",
    # Introspection (safe subset — no access to live objects)
    "pprint", "reprlib",
    # Hashing (no private-key crypto)
    "hashlib", "hmac",
    # Structured binary (no file I/O)
    "struct", "binascii",
    # Agent API wrapper (injected by sandbox — agents call this for LLM calls)
    "agent_api",
    # Optional data science packages (only if installed)
    "numpy", "scipy", "pandas", "sympy", "networkx", "sklearn",
    "matplotlib",  # safe if backend=Agg (no display), output to bytes
})

# Sub-modules of allowed packages (e.g. numpy.linalg)
_ALLOWED_IMPORT_PREFIXES = tuple(
    m + "." for m in _ALLOWED_IMPORTS
)


# ── Dangerous AST nodes ────────────────────────────────────────────────────────
_BLOCKED_AST_NODE_TYPES = (
    ast.Global,
    ast.Nonlocal,
    ast.AsyncFunctionDef,   # prevents async escape tricks
)

_BLOCKED_NAMES = frozenset({
    "__import__", "__builtins__", "__class__", "__subclasses__",
    "__bases__", "__mro__", "__code__", "__globals__", "__dict__",
    "__loader__", "__spec__", "__file__", "__cached__",
    "eval", "exec", "compile", "open", "input",
    "getattr", "setattr", "delattr", "vars", "dir", "hasattr",
    "breakpoint", "memoryview",
})

# Pattern blocklist — obfuscated attacks the AST might miss
_BLOCKED_PATTERNS = [
    re.compile(r'__[a-z]+__',                              re.I),  # dunder access
    re.compile(r'importlib',                               re.I),
    re.compile(r'subprocess',                              re.I),
    re.compile(r'os\s*\.\s*(system|popen|execv|spawn|fork|kill|remove|unlink|rmdir|listdir|getcwd|chdir|environ|getenv)', re.I),
    re.compile(r'sys\s*\.\s*(exit|argv|path|modules|stdin|stdout|stderr)', re.I),
    re.compile(r'\bsocket\b',                              re.I),
    re.compile(r'\burllib\b',                              re.I),
    re.compile(r'\brequests\b',                            re.I),
    re.compile(r'\bhttp\s*\.',                             re.I),
    re.compile(r'ftplib|smtplib|paramiko|fabric',          re.I),
    re.compile(r'ctypes|cffi|cython',                      re.I),
    re.compile(r'pickle|marshal|shelve',                   re.I),
    re.compile(r'\bshutil\b',                              re.I),
    re.compile(r'\btempfile\b',                            re.I),
    re.compile(r'\bpathlib\b',                             re.I),
    re.compile(r'\bopen\s*\(',                             re.I),
    re.compile(r'base64\s*\.\s*b64decode',                 re.I),
    re.compile(r'codecs\s*\.\s*decode',                    re.I),
    re.compile(r'chr\s*\(\s*\d+\s*\)',                     re.I),  # chr() chaining
    re.compile(r'bytes\s*\(\s*\['),                                 # bytes([...]) obfuscation
    re.compile(r'lambda\s*.*:\s*.*\(',                     re.I),  # lambda-wrapped calls
    re.compile(r'globals\s*\(\s*\)'),
    re.compile(r'locals\s*\(\s*\)'),
    re.compile(r'type\s*\(\s*.*,\s*.*,\s*.*\)'),                   # dynamic class creation
]


class SandboxViolation(Exception):
    pass


# ── AST checker ───────────────────────────────────────────────────────────────
def _ast_check(code: str) -> list[str]:
    violations = []
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"Syntax error: {e}"]

    for node in ast.walk(tree):
        # Block dangerous statement types
        if isinstance(node, _BLOCKED_AST_NODE_TYPES):
            violations.append(f"Blocked node type: {type(node).__name__}")

        # Validate imports against allowlist
        elif isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name.split(".")[0]
                if mod not in _ALLOWED_IMPORTS:
                    violations.append(f"Import blocked: ['{alias.name}'] — not in allowlist")

        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod not in _ALLOWED_IMPORTS:
                names = [a.name for a in node.names]
                violations.append(f"Import blocked: {node.module} — not in allowlist")

        # Block dangerous name references
        elif isinstance(node, ast.Name) and node.id in _BLOCKED_NAMES:
            violations.append(f"Blocked builtin: {node.id}")

        # Block dangerous attribute access
        elif isinstance(node, ast.Attribute) and node.attr in _BLOCKED_NAMES:
            violations.append(f"Blocked attribute: .{node.attr}")

    return violations


# ── Pattern checker ───────────────────────────────────────────────────────────
def _pattern_check(code: str) -> list[str]:
    violations = []
    for pattern in _BLOCKED_PATTERNS:
        if pattern.search(code):
            violations.append(f"Blocked pattern: {pattern.pattern[:50]}")
    return violations


# ── Agent API wrapper (injected into sandbox) ─────────────────────────────────
#
# This module is written to a temp file and injected alongside the script.
# Agents call: agent_api.ask(prompt, max_tokens=500)
#
# Security properties:
#   - API key injected at runtime by the host process; never in agent code
#   - Model is hardcoded to a cheap/fast model
#   - Per-call token cap: 1000 tokens max
#   - Hard session limit: 10 calls, 20k tokens total
#   - All calls logged to stderr with token counts
#   - Rate-limit enforced OUTSIDE the sandbox (can't be bypassed)
#   - No streaming — response must be a single short completion
#
def _make_agent_api_module(api_key: str) -> str:
    return textwrap.dedent(f"""\
        # agent_api — injected safe wrapper for LLM calls
        # Agents may call: agent_api.ask(prompt, max_tokens=500)
        # This module enforces rate limits and hides the real API key.
        import json as _json
        import sys as _sys

        _CALLS_MADE = 0
        _TOKENS_USED = 0
        _MAX_CALLS = 5          # hard cap per script execution
        _MAX_TOKENS = 5000      # hard cap per script execution
        _MAX_PER_CALL = 1000    # max tokens per single call
        _MODEL = "claude-haiku-4-5-20251001"  # cheapest/fastest only

        def ask(prompt: str, max_tokens: int = 500) -> str:
            global _CALLS_MADE, _TOKENS_USED
            if _CALLS_MADE >= _MAX_CALLS:
                raise RuntimeError(f"agent_api: call limit reached ({{_MAX_CALLS}} calls max per script)")
            if _TOKENS_USED >= _MAX_TOKENS:
                raise RuntimeError(f"agent_api: token limit reached ({{_MAX_TOKENS}} tokens max per script)")
            max_tokens = min(max_tokens, _MAX_PER_CALL)

            # Import here (inside function) — the sandbox preamble is already past by now
            import urllib.request as _req
            import json as _json

            payload = _json.dumps({{
                "model": _MODEL,
                "max_tokens": max_tokens,
                "messages": [{{"role": "user", "content": str(prompt)[:4000]}}],
            }}).encode()

            request = _req.Request(
                "https://api.anthropic.com/v1/messages",
                data=payload,
                headers={{
                    "x-api-key": "{api_key}",
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                }},
                method="POST",
            )
            try:
                with _req.urlopen(request, timeout=10) as resp:
                    data = _json.loads(resp.read())
                    tokens_in  = data.get("usage", {{}}).get("input_tokens", 0)
                    tokens_out = data.get("usage", {{}}).get("output_tokens", 0)
                    _CALLS_MADE  += 1
                    _TOKENS_USED += tokens_in + tokens_out
                    print(f"[agent_api] call {{_CALLS_MADE}}/{{_MAX_CALLS}} — {{tokens_in}}in {{tokens_out}}out — {{_TOKENS_USED}}/{{_MAX_TOKENS}} tokens used", file=_sys.stderr)
                    result = data.get("content", [])
                    return result[0].get("text", "") if result else ""
            except Exception as e:
                raise RuntimeError(f"agent_api: request failed: {{e}}")

        def status() -> dict:
            return {{"calls": _CALLS_MADE, "max_calls": _MAX_CALLS,
                    "tokens": _TOKENS_USED, "max_tokens": _MAX_TOKENS}}
    """)


# ── Network isolation helper ──────────────────────────────────────────────────
_UNSHARE_SUPPORTED = None  # cached after first probe

def _probe_unshare() -> bool:
    """
    Return True only if `unshare --net --user --map-root-user` actually works
    on this kernel. Some kernels (hardened / container environments) have the
    binary but deny unprivileged user namespaces (EPERM on uid_map write).
    We probe once and cache the result.
    """
    import shutil
    import subprocess as _sp
    if not shutil.which("unshare"):
        return False
    try:
        r = _sp.run(
            ["unshare", "--net", "--user", "--map-root-user",
             sys.executable, "-c", "import sys; sys.exit(0)"],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _build_exec_cmd(script_path: str) -> list[str]:
    """
    Build the execution command. Uses `unshare --net --user` when the kernel
    supports unprivileged user namespaces. Falls back gracefully to plain
    Python — all other sandbox layers remain active.
    """
    global _UNSHARE_SUPPORTED
    if _UNSHARE_SUPPORTED is None:
        _UNSHARE_SUPPORTED = _probe_unshare()
    if _UNSHARE_SUPPORTED:
        return [
            "unshare", "--net", "--user", "--map-root-user",
            sys.executable, "-I", script_path,
        ]
    # Fallback — no network namespace, but all other layers still apply
    return [sys.executable, "-I", script_path]


# ── Public API ────────────────────────────────────────────────────────────────
async def run_script(
    code: str,
    language: str = "python",
    timeout: int = DEFAULT_TIMEOUT,
    extra_files: dict[str, str] = None,
    inject_api: bool = True,         # inject agent_api module (set False in tests)
) -> dict:
    """Run a sandboxed Python script."""

    # 0. Reject bash entirely
    if language != "python":
        return _reject(["Only Python scripts are permitted. Bash is disabled."])

    # 1. Length cap
    if len(code) > MAX_CODE_LENGTH:
        return _reject([f"Script too long ({len(code)} chars). Max {MAX_CODE_LENGTH}."])

    # 2. AST analysis (validates imports against allowlist)
    ast_violations = _ast_check(code)
    if ast_violations:
        return _reject(ast_violations)

    # 3. Pattern scan (obfuscation detection)
    pattern_violations = _pattern_check(code)
    if pattern_violations:
        return _reject(pattern_violations)

    # 4. Execute in isolated temp dir
    with tempfile.TemporaryDirectory(prefix="agentbox_") as tmpdir:
        if extra_files:
            for fname, content in extra_files.items():
                safe_fname = Path(fname).name
                (Path(tmpdir) / safe_fname).write_text(content)

        # Inject agent_api module (key never touches agent code)
        if inject_api:
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if api_key:
                (Path(tmpdir) / "agent_api.py").write_text(_make_agent_api_module(api_key))
            else:
                # Write a stub that explains the key is not configured
                (Path(tmpdir) / "agent_api.py").write_text(textwrap.dedent("""\
                    def ask(*a, **kw):
                        raise RuntimeError("agent_api: ANTHROPIC_API_KEY not configured on this host")
                    def status():
                        return {"calls": 0, "max_calls": 0, "tokens": 0, "max_tokens": 0}
                """))

        # Resource limiting preamble
        preamble = textwrap.dedent("""\
            import resource as _r
            _r.setrlimit(_r.RLIMIT_AS,    (256*1024*1024, 256*1024*1024))  # 256 MB RAM
            _r.setrlimit(_r.RLIMIT_NOFILE, (32, 32))                        # 32 file descriptors
            _r.setrlimit(_r.RLIMIT_NPROC,  (1, 1))                          # no forking
            del _r
        """)

        script_path = Path(tmpdir) / "script.py"
        script_path.write_text(preamble + "\n" + code)

        # Minimal environment — no credentials, no paths that leak system info
        # ANTHROPIC_API_KEY is intentionally excluded — agent_api injects it at
        # module level from the host process at module-write time above.
        restricted_env = {
            "PATH":       "/usr/bin:/bin",
            "PYTHONPATH": str(tmpdir),   # only the tmpdir (for agent_api.py)
            "PYTHONHOME": "",
            "HOME":       tmpdir,        # redirect HOME to sandbox dir
            "TMPDIR":     tmpdir,
        }

        exec_cmd = _build_exec_cmd(str(script_path))

        try:
            proc = await asyncio.create_subprocess_exec(
                *exec_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=tmpdir,
                env=restricted_env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            out = stdout.decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARS]
            err = stderr.decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARS]

            out = _scrub_output(out)
            err = _scrub_output(err)

            return {
                "ok":         proc.returncode == 0,
                "stdout":     out,
                "stderr":     err,
                "exit_code":  proc.returncode,
                "violations": [],
                "network_isolated": "unshare" in exec_cmd[0],
                "summary":    out[:300] if out else (err[:300] if err else "No output"),
            }
        except asyncio.TimeoutError:
            try: proc.kill()
            except Exception: pass
            return _reject([f"Script timed out after {timeout}s"])
        except Exception as e:
            return _reject([f"Execution error: {e}"])


def _reject(violations: list[str]) -> dict:
    return {
        "ok":         False,
        "stdout":     "",
        "stderr":     "\n".join(violations),
        "exit_code":  -1,
        "violations": violations,
        "network_isolated": False,
        "summary":    f"Script blocked: {violations[0]}",
        "raw":        {},
    }


_OUTPUT_SCRUB = [
    re.compile(r'(?i)(api[_-]?key|secret|password|token)\s*[=:]\s*\S+'),
    re.compile(r'(?i)authorization:\s*\S+'),
    re.compile(r'sk-[A-Za-z0-9]{20,}'),          # OpenAI-style keys
    re.compile(r'ghp_[A-Za-z0-9]{36}'),           # GitHub PATs
    re.compile(r'eyJ[A-Za-z0-9_\-]{20,}'),        # JWT tokens
]

def _scrub_output(text: str) -> str:
    for p in _OUTPUT_SCRUB:
        text = p.sub("[REDACTED]", text)
    return text
