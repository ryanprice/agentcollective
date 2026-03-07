"""
Sandbox
-------
Hardened subprocess execution for agent-generated scripts.

Defence layers:
  1. AST parse — reject before any execution if dangerous nodes found
  2. Pattern blocklist — catch obfuscated patterns AST misses
  3. Bash disabled — shell scripts rejected entirely
  4. Restricted env — minimal PATH, no HOME leakage, no API keys
  5. Hard timeout — 15s max
  6. Output cap — 4000 chars stdout+stderr
  7. Resource limits — set via ulimit in wrapper
"""

import ast
import asyncio
import os
import re
import sys
import tempfile
import textwrap
from pathlib import Path


MAX_OUTPUT_CHARS = 4000
DEFAULT_TIMEOUT  = 15   # seconds
MAX_CODE_LENGTH  = 8000  # chars


# ── Dangerous AST node types ──────────────────────────────────────────────────
_BLOCKED_AST_NODES = (
    ast.Import,
    ast.ImportFrom,
    ast.Global,
    ast.Nonlocal,
    ast.AsyncFunctionDef,   # prevents async escape tricks
)

# Function/attribute names that are dangerous even without import
_BLOCKED_NAMES = frozenset({
    "__import__", "__builtins__", "__class__", "__subclasses__",
    "__bases__", "__mro__", "__code__", "__globals__", "__dict__",
    "eval", "exec", "compile", "open", "input",
    "getattr", "setattr", "delattr", "vars", "dir", "hasattr",
    "breakpoint", "memoryview",
})

# String patterns for obfuscated attacks (base64, hex encoding etc.)
_BLOCKED_PATTERNS = [
    re.compile(r'__[a-z]+__'),                    # dunder access of any kind
    re.compile(r'importlib', re.I),
    re.compile(r'subprocess', re.I),
    re.compile(r'os\s*\.\s*(system|popen|execv|spawn|fork|kill|remove|unlink|rmdir|listdir|getcwd|chdir|environ|getenv)', re.I),
    re.compile(r'sys\s*\.\s*(exit|argv|path|modules|stdin|stdout|stderr)', re.I),
    re.compile(r'socket', re.I),
    re.compile(r'urllib', re.I),
    re.compile(r'requests', re.I),
    re.compile(r'http\s*\.', re.I),
    re.compile(r'ftplib|smtplib|paramiko|fabric', re.I),
    re.compile(r'ctypes|cffi|cython', re.I),
    re.compile(r'pickle|marshal|shelve', re.I),
    re.compile(r'shutil', re.I),
    re.compile(r'tempfile', re.I),
    re.compile(r'pathlib', re.I),
    re.compile(r'\bopen\s*\(', re.I),
    re.compile(r'base64\s*\.\s*b64decode', re.I),
    re.compile(r'codecs\s*\.\s*decode', re.I),
    re.compile(r'chr\s*\(\s*\d+\s*\)', re.I),   # chr() chaining (obfuscation)
    re.compile(r'bytes\s*\(\s*\['),              # bytes([...]) (obfuscation)
    re.compile(r'lambda\s*.*:\s*.*\('),          # lambda-wrapped calls
    re.compile(r'globals\s*\(\s*\)'),
    re.compile(r'locals\s*\(\s*\)'),
    re.compile(r'type\s*\(\s*.*,\s*.*,\s*.*\)'), # dynamic class creation
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
        if isinstance(node, _BLOCKED_AST_NODES):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in getattr(node, 'names', [])]
                mod   = getattr(node, 'module', '') or ''
                violations.append(f"Import blocked: {mod or names}")
            else:
                violations.append(f"Blocked node type: {type(node).__name__}")

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
            violations.append(f"Blocked pattern: {pattern.pattern[:40]}")
    return violations


# ── Public API ────────────────────────────────────────────────────────────────
async def run_script(
    code: str,
    language: str = "python",
    timeout: int = DEFAULT_TIMEOUT,
    extra_files: dict[str, str] = None,
) -> dict:
    """Run a sandboxed Python script. Bash is not supported."""

    # 0. Reject bash entirely
    if language != "python":
        return _reject(["Only Python scripts are permitted. Bash is disabled."])

    # 1. Length cap
    if len(code) > MAX_CODE_LENGTH:
        return _reject([f"Script too long ({len(code)} chars). Max {MAX_CODE_LENGTH}."])

    # 2. AST analysis
    ast_violations = _ast_check(code)
    if ast_violations:
        return _reject(ast_violations)

    # 3. Pattern scan (catches obfuscated imports AST might miss)
    pattern_violations = _pattern_check(code)
    if pattern_violations:
        return _reject(pattern_violations)

    # 4. Execute in isolated temp dir with minimal env
    with tempfile.TemporaryDirectory(prefix="agentbox_") as tmpdir:
        if extra_files:
            for fname, content in extra_files.items():
                safe_fname = Path(fname).name  # strip any path traversal
                (Path(tmpdir) / safe_fname).write_text(content)

        # Prepend hard resource limiter
        preamble = textwrap.dedent("""\
            import resource as _r, sys as _sys
            _r.setrlimit(_r.RLIMIT_AS,   (256*1024*1024, 256*1024*1024))  # 256 MB RAM
            _r.setrlimit(_r.RLIMIT_NOFILE, (16, 16))                       # 16 file descriptors
            _r.setrlimit(_r.RLIMIT_NPROC,  (1, 1))                         # no forking
            del _r
        """)

        script_path = Path(tmpdir) / "script.py"
        script_path.write_text(preamble + "\n" + code)

        restricted_env = {
            "PATH":       "/usr/bin:/bin",
            "PYTHONPATH": "",
            "PYTHONHOME": "",
            # Explicitly exclude all env vars that might leak credentials
        }

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-I",  # -I = isolated mode (ignores env, site packages)
                str(script_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=tmpdir,
                env=restricted_env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            out = stdout.decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARS]
            err = stderr.decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARS]

            # Scrub any accidental credential leakage from output
            out = _scrub_output(out)
            err = _scrub_output(err)

            return {
                "ok":         proc.returncode == 0,
                "stdout":     out,
                "stderr":     err,
                "exit_code":  proc.returncode,
                "violations": [],
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
        "summary":    f"Script blocked: {violations[0]}",
        "raw":        {},
    }


# Patterns that should never appear in script output (belt-and-suspenders)
_OUTPUT_SCRUB = [
    re.compile(r'(?i)(api[_-]?key|secret|password|token)\s*[=:]\s*\S+'),
    re.compile(r'(?i)authorization:\s*\S+'),
]

def _scrub_output(text: str) -> str:
    for p in _OUTPUT_SCRUB:
        text = p.sub("[REDACTED]", text)
    return text
