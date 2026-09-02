"""
Omnix V6 - Process Capabilities.

Provides implementations for executing shell commands and managing processes.

SECURITY NOTE: RunCommandCapability uses asyncio.create_subprocess_shell and
therefore represents arbitrary code execution. It is classified as a DANGEROUS
capability (spec.dangerous=True) and must be routed through the canonical
CapabilityRouter/safety mechanism. Higher-level planning components must not
bypass the safety gate to invoke this capability.

Phase 12 (REAL AUTOMATION EXECUTION LAYER) extends this module with
:class:`ProcessIsRunningCapability`, a read-only observation used to verify
that a previously launched process is still alive (and to map a process name
back to a PID when possible).  The capability never kills, never starts,
never inspects command lines.
"""

import asyncio
import os
import re
from typing import Any, Mapping

from core.capability import CapabilitySpec, CapabilityParameter, ParamType
from core.results import CapabilityResult, CapabilityStatus, ActionResult, ActionStatus
from .base import BaseCapability
from core.errors import OmnixError

# Basic blocklist of dangerous characters / shell metachars that
# indicate the command is doing something risky (e.g. piping, redirects, subshells).
# This is a defensive layer, not a comprehensive safety boundary.
_DANGEROUS_SHELL_CHARS = re.compile(r'[<>|&;$\(\)\[\]\*\?~\n\r]')

class RunCommandCapability(BaseCapability):
    """Capability to execute a shell command.
    
    SECURITY: This capability is classified as DANGEROUS. It is the
    responsibility of the safety layer to enforce constraints on its usage.
    """
    
    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="process.run",
            version="1.0.0",
            description="Executes a shell command and returns its output.",
            parameters={
                "command": CapabilityParameter(
                    name="command",
                    type=ParamType.STRING,
                    description="The shell command to execute.",
                    required=True
                ),
                "timeout": CapabilityParameter(
                    name="timeout",
                    type=ParamType.INTEGER,
                    description="Timeout in seconds for the command execution.",
                    required=False,
                    default=30
                )
            },
            tags={"process", "shell", "execute", "high-risk"},
            dangerous=True,  # Mark as dangerous so the router enforces safety
            requires_capabilities=(),
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        command = params.get("command")
        timeout = params.get("timeout", 30)
        
        if not command:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("Command parameter is required.")
            )

        # Validate timeout
        try:
            timeout = int(timeout)
        except (ValueError, TypeError):
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Timeout must be an integer: {timeout}")
            )
            
        if timeout <= 0:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Timeout must be a positive integer: {timeout}")
            )

        # Defensive validation: block obvious shell injection vectors
        if _DANGEROUS_SHELL_CHARS.search(str(command)):
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(
                    "Command contains blocked shell metacharacters "
                    "(<, >, |, &, ;, backtick, $, (), [], *, ?, etc.). "
                    "Run a simpler command or use a more specific capability."
                )
            )

        try:
            # Create subprocess with explicit shell
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            try:
                # Wait for command completion with timeout
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
                
                return_code = process.returncode
                stdout_str = stdout.decode('utf-8', errors='replace') if stdout else ""
                stderr_str = stderr.decode('utf-8', errors='replace') if stderr else ""
                
                return CapabilityResult(
                    capability_name=self.spec.name,
                    status=CapabilityStatus.VERIFIED if return_code == 0 else CapabilityStatus.EXECUTED,
                    attempted=True,
                    executed=True,
                    verified=(return_code == 0),
                    failed=(return_code != 0),
                    action=ActionResult(status=ActionStatus.EXECUTED, action_name=self.spec.name),
                    details={
                        "return_code": return_code,
                        "stdout": stdout_str,
                        "stderr": stderr_str
                    }
                )
                
            except asyncio.TimeoutError:
                # Kill process on timeout
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except Exception:
                    process.kill()
                    
                return CapabilityResult(
                    capability_name=self.spec.name,
                    status=CapabilityStatus.TIMED_OUT,
                    attempted=True,
                    executed=False,
                    failed=True,
                    error=OmnixError(f"Command execution timed out after {timeout} seconds.")
                )
                
        except Exception as e:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Failed to execute command: {str(e)}")
            )


# ---------------------------------------------------------------------------
# Phase 12: process observation capability (read-only verification)
# ---------------------------------------------------------------------------

def _is_running_on_windows(name: str) -> dict:
    """Return ``{"running": bool, "pids": [...]}`` for ``name`` on Windows.

    Implemented on top of :mod:`psutil` (already used by the
    application subsystem), with a fallback to ``tasklist`` via the
    standard library if ``psutil`` is not importable.  The function
    matches by exact process name (case-insensitive on Windows), by
    a substring of the image name (``"notepad"`` matches
    ``"notepad.exe"``), and by the bare file name of the ``exe`` path
    (so passing the full ``sys.executable`` works too).
    """
    result = {"running": False, "pids": []}
    needle = (name or "").strip().lower()
    if not needle:
        return result
    if not needle.endswith(".exe"):
        needle_with_ext = needle + ".exe"
    else:
        needle_with_ext = needle
    # Bare file name of the needle (e.g. ``python.exe`` from
    # ``E:\\Python\\python.exe``), so callers can pass either a name
    # or a full path.
    needle_basename = os.path.basename(needle).lower()

    try:
        import psutil  # type: ignore
    except Exception:  # noqa: BLE001
        psutil = None  # type: ignore

    if psutil is not None:
        try:
            for proc in psutil.process_iter(["pid", "name", "exe"]):
                try:
                    pname = (proc.info.get("name") or "").lower()
                    pexe = (proc.info.get("exe") or "").lower()
                except Exception:  # noqa: BLE001
                    continue
                pexe_basename = os.path.basename(pexe)
                if (
                    pname == needle
                    or pname == needle_with_ext
                    or needle in pname
                    or pexe_basename == needle_basename
                    or pexe_basename == needle_with_ext
                ):
                    pid = proc.info.get("pid")
                    if pid is not None:
                        result["pids"].append(int(pid))
        except Exception:  # noqa: BLE001
            pass
        result["running"] = bool(result["pids"])
        return result

    # Fallback: shell out to ``tasklist`` (Windows-only).
    try:
        import subprocess
        completed = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {needle_with_ext}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = (completed.stdout or "").strip()
        if out and "INFO:" not in out:
            result["running"] = True
            for line in out.splitlines():
                parts = [p.strip().strip('"') for p in line.split(",")]
                if len(parts) >= 2 and parts[1].isdigit():
                    result["pids"].append(int(parts[1]))
    except Exception:  # noqa: BLE001
        pass
    return result


class ProcessIsRunningCapability(BaseCapability):
    """Read-only observation: is a process with the given image name alive?

    This is the canonical "post-execution verification" capability for
    the launch / close / kill family of automation.  It is observation
    only: it never starts, never stops, never modifies state.

    Returns VERIFIED when the lookup itself succeeded (the process
    listing was reachable) and reports ``running`` and ``pids`` in the
    details.  A not-running process is therefore *not* a failure of
    this capability -- it is just a structured negative result.
    """

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="process.is_running",
            version="1.0.0",
            description="Read-only: is a process with the given name running?",
            parameters={
                "name": CapabilityParameter(
                    name="name",
                    type=ParamType.STRING,
                    description=(
                        "Process image name (e.g. 'notepad', 'notepad.exe', "
                        "'chrome').  Matched case-insensitively."
                    ),
                    required=True,
                ),
            },
            tags={"process", "observation", "read"},
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        name = params.get("name")
        if not name or not str(name).strip():
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("Name parameter is required."),
            )

        loop = asyncio.get_running_loop()

        def _check() -> dict:
            return _is_running_on_windows(str(name))

        try:
            result = await loop.run_in_executor(None, _check)
        except Exception as exc:  # noqa: BLE001
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(f"Failed to inspect process table: {exc!r}"),
            )

        return CapabilityResult(
            capability_name=self.spec.name,
            status=CapabilityStatus.VERIFIED,
            attempted=True,
            executed=True,
            verified=True,
            action=ActionResult(
                status=ActionStatus.EXECUTED, action_name=self.spec.name
            ),
            details={
                "name": str(name),
                "running": bool(result.get("running")),
                "pids": list(result.get("pids") or []),
                "platform": os.name,
            },
        )
