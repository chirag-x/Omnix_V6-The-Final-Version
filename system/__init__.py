"""
Omnix V6 — System Subsystem Package (Phase 2).

This package contains the *concrete* implementations of the
``core.execution.interfaces`` contracts.  Every module here targets a
single Windows capability and follows these rules:

    * Returns structured :class:`core.results.ActionResult` for every
      state-changing call.  Never raises on recoverable failures.
    * Honors ``core.utils.timers.run_with_timeout`` and
      :class:`CancellationToken` on long-running operations.
    * Implements :class:`core.lifecycle.LifecycleMixin` so the engine
      can register it with the :class:`core.service_registry.ServiceRegistry`.
    * Surfaces a :meth:`statistics` dict for the health monitor.
    * Logs with :mod:`loguru`.

Public re-exports below are *intentionally narrow* — callers should
import from the submodules, not the package root, to make
dependencies explicit.
"""

# Re-exports for convenience (callers may import the package and
# reference by class name without diving into submodules).
from .application.app_service import WindowsApplicationService
from .clipboard.clipboard_service import WindowsClipboardService
from .filesystem.filesystem_service import WindowsFilesystemService
from .input.input_service import WindowsInputService
from .processes.process_service import (
    WindowsProcessService,
    NON_OVERRIDABLE_PROTECTED,
    DEFAULT_PROTECTED,
)
from .windows.window_service import WindowsWindowService, WindowInfo

__all__ = [
    "WindowsApplicationService",
    "WindowsWindowService",
    "WindowInfo",
    "WindowsProcessService",
    "NON_OVERRIDABLE_PROTECTED",
    "DEFAULT_PROTECTED",
    "WindowsInputService",
    "WindowsClipboardService",
    "WindowsFilesystemService",
]
