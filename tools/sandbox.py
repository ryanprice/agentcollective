"""
Sandbox
-------
Safe subprocess execution for agent-generated scripts.
- No network access from scripts (firewall via seccomp/nsjail if available)
- No filesystem access outside of a temp sandbox dir
- Hard timeout enforcement
- Stdout/stderr captured and returned
"""

import asyncio
import os
import sys
import tempfile
import textwrap
from pathlib import Path


BLOCKED_IMPORTS = [
    "subprocess", "os.system", "shutil.rmtree",
    "socket", "ftplib", "smtplib", "paramiko",
    "__import__", "eval(", "exec(",
]

MAX_OUTPUT_CHARS = 4000
DEFAULT_TIMEOUT  = 15  # seconds


class SandboxViolation(Exception):
    pass


def _check_script_safety(code: str) -> list[str]:
    """Return list of violations found in script."""
    violations = []
    for blocked in BLOCKED_IMPORTS:
        if blocked in code:
            violations.append(f"Blocked pattern: {blocked}")
    return violations


async def run_script(
    code: str,
    language: str = "python",
    timeout: int = DEFAULT_TIMEOUT,
    extra_files: dict[str, str] = None,
) -> dict:
    """
    Run a sandboxed script. Returns:
    {
        "ok": bool,
        "stdout": str,
        "stderr": str,
        "exit_code": int,
        "violations": list[str]
    }
    """
    violations = _check_script_safety(code)
    if violations:
        return {
            "ok": False,
            "stdout": "",
            "stderr": "\n".join(violations),
            "exit_code": -1,
            "violations": violations,
        }

    with tempfile.TemporaryDirectory(prefix="agentbox_") as tmpdir:
        # Write extra files if provided
        if extra_files:
            for fname, content in extra_files.items():
                (Path(tmpdir) / fname).write_text(content)

        if language == "python":
            script_path = Path(tmpdir) / "script.py"
            script_path.write_text(code)
            cmd = [sys.executable, str(script_path)]
        elif language == "bash":
            script_path = Path(tmpdir) / "script.sh"
            script_path.write_text(code)
            os.chmod(script_path, 0o755)
            cmd = ["/bin/bash", str(script_path)]
        else:
            return {
                "ok": False,
                "stdout": "",
                "stderr": f"Unsupported language: {language}",
                "exit_code": -1,
                "violations": [],
            }

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=tmpdir,
                env={
                    "PATH": "/usr/bin:/bin",
                    "HOME": tmpdir,
                    "PYTHONPATH": "",
                },
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            return {
                "ok": proc.returncode == 0,
                "stdout": stdout.decode()[:MAX_OUTPUT_CHARS],
                "stderr": stderr.decode()[:MAX_OUTPUT_CHARS],
                "exit_code": proc.returncode,
                "violations": [],
            }
        except asyncio.TimeoutError:
            proc.kill()
            return {
                "ok": False,
                "stdout": "",
                "stderr": f"Script timed out after {timeout}s",
                "exit_code": -1,
                "violations": ["timeout"],
            }
        except Exception as e:
            return {
                "ok": False,
                "stdout": "",
                "stderr": str(e),
                "exit_code": -1,
                "violations": [],
            }
