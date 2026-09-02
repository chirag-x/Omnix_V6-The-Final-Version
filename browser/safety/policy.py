"""
Browser safety policy (Phase 8).

The policy is a small, closed-set, *advisory* check that runs
*before* the browser service dispatches a :class:`BrowserRequest`.

Responsibilities
----------------

* Validate the URL scheme — ``http``/``https``/``file``/``about:blank``
  only.  No ``javascript:``, no ``data:`` with executable payloads.
* Validate the request against the closed action / parameter sets.
* Optionally enforce a host allowlist (None = no allowlist = open).
* Optionally enforce a per-session action count cap.

Non-responsibilities
--------------------

The policy is **not** a sandbox.  It does not isolate the browser
process; it does not run inside the browser.  It is a thin gate
that runs in the V6 process before a request is dispatched.

The policy **never**:

* imports ``subprocess``, ``os.system``, ``os.popen``;
* evaluates Python / JavaScript / shell;
* calls the LLM provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, Iterable, Optional, Tuple
from urllib.parse import urlparse

from browser.models.contracts import (
    ACTION_PARAM_KEYS,
    BrowserAction,
    BrowserRequest,
    BrowserResultStatus,
)


_ALLOWED_SCHEMES: FrozenSet[str] = frozenset({
    "http", "https", "file", "about", "chrome", "devtools",  # standard
    # NOTE: ``javascript:`` is intentionally NOT in this set.
    # ``data:text/html`` is allowed below for fixture usage only
    # and only when ``allow_data_urls=True``.
})

# File extensions the service refuses to download automatically
# (per the spec: "Do not download arbitrary executables automatically").
_DISALLOWED_DOWNLOAD_EXTS: FrozenSet[str] = frozenset({
    ".exe", ".msi", ".bat", ".cmd", ".ps1", ".sh",
    ".scr", ".com", ".vbs", ".js", ".jse", ".wsf",
    ".dll", ".so", ".app", ".dmg",
})


@dataclass(frozen=True)
class BrowserPolicyDecision:
    """The result of a :meth:`BrowserSafetyPolicy.check_request` call."""

    allowed: bool
    status: BrowserResultStatus = BrowserResultStatus.OK
    reason: str = ""


@dataclass(frozen=True)
class BrowserSafetyPolicy:
    """A small, closed-set safety policy for browser requests.

    The defaults are deliberately permissive (no host allowlist,
    generous action cap) so the system is usable in dev.  A
    deployment can build a stricter policy by passing in an
    allowlist or by lowering ``max_actions_per_session``.
    """

    # If set, only these hostnames (lower-cased) may be navigated to.
    # ``None`` = no allowlist.
    host_allowlist: Optional[FrozenSet[str]] = None
    # If True, allow ``data:text/html,...`` URLs (handy for local
    # fixtures).  Production deployments should set this False.
    allow_data_urls: bool = True
    # If True, allow ``file://`` URLs.
    allow_file_urls: bool = True
    # If True, allow ``about:blank``.
    allow_about_blank: bool = True
    # Soft cap on actions per session (None = no cap).
    max_actions_per_session: Optional[int] = 1000
    # Per-request URL length cap (None = no cap).
    max_url_length: Optional[int] = 4096
    # If False, refuse download of executables / scripts.
    allow_executable_downloads: bool = False

    def check_request(
        self,
        request: BrowserRequest,
        *,
        session_action_count: int = 0,
    ) -> BrowserPolicyDecision:
        """Return whether ``request`` is allowed by this policy."""

        if self.max_actions_per_session is not None:
            if session_action_count >= self.max_actions_per_session:
                return BrowserPolicyDecision(
                    allowed=False,
                    status=BrowserResultStatus.BLOCKED,
                    reason=(
                        f"session action cap reached "
                        f"({self.max_actions_per_session})"
                    ),
                )

        # Per-action URL / parameter checks.
        if request.action == BrowserAction.NAVIGATE:
            url = request.parameters.get("url")
            if not isinstance(url, str) or not url.strip():
                return BrowserPolicyDecision(
                    allowed=False,
                    status=BrowserResultStatus.INVALID_REQUEST,
                    reason="navigate: 'url' must be a non-empty string",
                )
            decision = self.check_url(url)
            if not decision.allowed:
                return decision

        if request.action == BrowserAction.OPEN:
            start = request.parameters.get("start_url")
            if isinstance(start, str) and start.strip():
                decision = self.check_url(start)
                if not decision.allowed:
                    return decision

        if request.action == BrowserAction.DOWNLOAD:
            save_to = request.parameters.get("save_to")
            if not isinstance(save_to, str) or not save_to.strip():
                return BrowserPolicyDecision(
                    allowed=False,
                    status=BrowserResultStatus.INVALID_REQUEST,
                    reason="download: 'save_to' must be a non-empty string",
                )
            if not self.allow_executable_downloads:
                lower = save_to.lower()
                for ext in _DISALLOWED_DOWNLOAD_EXTS:
                    if lower.endswith(ext):
                        return BrowserPolicyDecision(
                            allowed=False,
                            status=BrowserResultStatus.BLOCKED,
                            reason=(
                                f"download: extension {ext!r} is not "
                                f"allowed by policy"
                            ),
                        )

        # Parameter keys must be in the closed set for the action.
        allowed_keys = ACTION_PARAM_KEYS.get(request.action, ())
        for key in request.parameters.keys():
            if key not in allowed_keys:
                return BrowserPolicyDecision(
                    allowed=False,
                    status=BrowserResultStatus.INVALID_REQUEST,
                    reason=(
                        f"{request.action.value}: parameter key "
                        f"{key!r} is not in the closed parameter set"
                    ),
                )

        return BrowserPolicyDecision(allowed=True)

    # --------------------------------------------------------------- URL

    def check_url(self, url: str) -> BrowserPolicyDecision:
        """Return whether ``url`` is allowed by this policy."""

        if not isinstance(url, str):
            return BrowserPolicyDecision(
                allowed=False,
                status=BrowserResultStatus.INVALID_REQUEST,
                reason="url: must be a string",
            )
        if not url:
            return BrowserPolicyDecision(
                allowed=False,
                status=BrowserResultStatus.INVALID_REQUEST,
                reason="url: empty",
            )
        if self.max_url_length is not None and len(url) > self.max_url_length:
            return BrowserPolicyDecision(
                allowed=False,
                status=BrowserResultStatus.INVALID_REQUEST,
                reason=(
                    f"url: length {len(url)} exceeds policy cap "
                    f"{self.max_url_length}"
                ),
            )

        # ``about:blank`` is special: it has no scheme separator.
        if url == "about:blank":
            if not self.allow_about_blank:
                return BrowserPolicyDecision(
                    allowed=False,
                    status=BrowserResultStatus.BLOCKED,
                    reason="url: about:blank is blocked by policy",
                )
            return BrowserPolicyDecision(allowed=True)

        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()

        if scheme == "data":
            if not self.allow_data_urls:
                return BrowserPolicyDecision(
                    allowed=False,
                    status=BrowserResultStatus.BLOCKED,
                    reason="url: data: URLs are blocked by policy",
                )
            return BrowserPolicyDecision(allowed=True)

        if scheme == "file":
            if not self.allow_file_urls:
                return BrowserPolicyDecision(
                    allowed=False,
                    status=BrowserResultStatus.BLOCKED,
                    reason="url: file:// URLs are blocked by policy",
                )
            return BrowserPolicyDecision(allowed=True)

        if scheme not in _ALLOWED_SCHEMES:
            return BrowserPolicyDecision(
                allowed=False,
                status=BrowserResultStatus.BLOCKED,
                reason=f"url: scheme {scheme!r} is not allowed",
            )

        if self.host_allowlist is not None:
            host = (parsed.hostname or "").lower()
            if host and host not in self.host_allowlist:
                return BrowserPolicyDecision(
                    allowed=False,
                    status=BrowserResultStatus.BLOCKED,
                    reason=(
                        f"url: host {host!r} is not in the host allowlist"
                    ),
                )

        return BrowserPolicyDecision(allowed=True)


__all__ = ["BrowserSafetyPolicy", "BrowserPolicyDecision"]
