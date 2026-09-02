"""
Shared test helpers for browser subsystem tests.

A minimal in-memory "fake Playwright" that satisfies the small
surface :class:`browser.session.session.BrowserSession` uses when
``playwright_factory`` is supplied.  It is intentionally tiny:

* Holds a list of in-memory pages.
* Pages hold a small DOM (id, tag, text, attrs).
* ``locator(selector)`` returns a small locator handle that
  supports the methods the session actually uses.

Tests build a DOM as Python dicts; the helpers then implement
the resolution order.  No network, no subprocesses, no
JavaScript evaluation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Fake DOM
# ---------------------------------------------------------------------------


@dataclass
class FakeNode:
    """One fake DOM node."""

    tag: str
    text: str = ""
    attrs: Dict[str, str] = field(default_factory=dict)
    children: List["FakeNode"] = field(default_factory=list)
    parent: Optional["FakeNode"] = None

    def get(self, name: str, default: Any = None) -> Any:
        return self.attrs.get(name, default)

    @property
    def id(self) -> str:
        return self.attrs.get("id", "")

    @property
    def role(self) -> str:
        """Mimic Playwright's implicit role mapping for the few tags we model.

        ``<a>`` → ``link`` so the session's ``_ALLOWED_ROLES`` accepts it.
        Everything else returns the explicit ``role`` attribute if set,
        else the tag name.
        """
        if "role" in self.attrs:
            return self.attrs["role"]
        if self.tag == "a":
            return "link"
        return self.tag

    @property
    def name(self) -> str:
        return self.attrs.get("aria-label", self.attrs.get("name", ""))

    @property
    def href(self) -> str:
        return self.attrs.get("href", "")

    @property
    def value(self) -> str:
        return self.attrs.get("value", "")

    @property
    def test_id(self) -> str:
        return self.attrs.get("data-testid", "")

    def find_css(self, selector: str) -> List["FakeNode"]:
        return _css_select(self, selector)


# ---------------------------------------------------------------------------
# CSS subset
# ---------------------------------------------------------------------------


def _css_select(root: FakeNode, selector: str) -> List[FakeNode]:
    """A *very* small CSS subset: id, tag, [attr=val], .class, xpath= prefix."""

    sel = selector.strip()
    out: List[FakeNode] = []

    if sel.startswith("xpath="):
        return _xpath_select(root, sel[len("xpath=") :])

    # A single part (we don't model descendant combinators — the session
    # only uses flat selectors with id / class / attr / tag).
    candidates: List[FakeNode] = []

    def walk(n: FakeNode) -> None:
        candidates.append(n)
        for c in n.children:
            walk(c)

    walk(root)

    if not _match_compound(candidates, sel, out):
        return out
    return out


def _match_compound(candidates: List[FakeNode], sel: str, out: List[FakeNode]) -> bool:
    for c in candidates:
        if _match_one_part(c, sel):
            out.append(c)
    return True


def _match_one_part(node: FakeNode, part: str) -> bool:
    i = 0
    # tag
    tag = ""
    while i < len(part) and (part[i].isalpha() or part[i] == "*"):
        tag += part[i]
        i += 1
    if tag and tag != "*" and node.tag != tag:
        return False
    rest = part[i:]
    while rest:
        if rest.startswith("#"):
            rest = rest[1:]
            j = 0
            while j < len(rest) and rest[j] not in ".[":
                j += 1
            if rest[:j] != node.id:
                return False
            rest = rest[j:]
        elif rest.startswith("."):
            rest = rest[1:]
            j = 0
            while j < len(rest) and rest[j] not in ".[":
                j += 1
            classes = node.attrs.get("class", "").split()
            if rest[:j] not in classes:
                return False
            rest = rest[j:]
        elif rest.startswith("["):
            end = rest.find("]")
            if end < 0:
                return False
            inner = rest[1:end]
            rest = rest[end + 1 :]
            if "=" not in inner:
                if inner not in node.attrs:
                    return False
            else:
                k, _, v = inner.partition("=")
                v = v.strip('"').strip("'")
                if node.attrs.get(k) != v:
                    return False
        else:
            return False
    return True


# ---------------------------------------------------------------------------
# XPath subset
# ---------------------------------------------------------------------------


def _xpath_select(root: FakeNode, expr: str) -> List[FakeNode]:
    """A *very* small XPath subset: //tag, //tag[@attr='val']."""

    expr = expr.strip()
    out: List[FakeNode] = []

    if not expr.startswith("//"):
        return out
    rest = expr[2:]

    if "[" in rest:
        tag, _, cond = rest.partition("[")
        cond = cond.rstrip("]")
        attr_name = ""
        attr_val = ""
        text_val = ""
        if cond.startswith("@"):
            inner = cond[1:]
            attr_name, _, attr_val = inner.partition("=")
            attr_val = attr_val.strip("'").strip('"')
        elif cond.startswith("text()="):
            inner = cond[len("text()=") :]
            text_val = inner.strip("'").strip('"')

        def walk(n: FakeNode) -> None:
            if n.tag == tag:
                if attr_name and n.attrs.get(attr_name) == attr_val:
                    out.append(n)
                elif text_val and n.text == text_val:
                    out.append(n)
            for c in n.children:
                walk(c)

        walk(root)
    else:

        def walk(n: FakeNode) -> None:
            if n.tag == rest:
                out.append(n)
            for c in n.children:
                walk(c)

        walk(root)
    return out


# ---------------------------------------------------------------------------
# Fake Locator
# ---------------------------------------------------------------------------


@dataclass
class _FakeElementHandle:
    """Minimal element handle: just enough to satisfy ``evaluate``."""

    node: "FakeNode"

    def evaluate(self, script: str, *, arg: Any = None) -> Any:
        s = script.replace(" ", "").replace("\n", "")
        if "tagName.toLowerCase()" in s:
            return self.node.tag.lower()
        if "innerText" in s or "textContent" in s:
            return self.node.text or ""
        if "el.value" in s or "el=>el.value" in s:
            return self.node.attrs.get("value", "")
        if "el.outerHTML" in s:
            return _outer_html(self.node)
        return ""


def _outer_html(n: "FakeNode") -> str:
    attrs = " ".join(f'{k}="{v}"' for k, v in n.attrs.items() if not k.startswith("__"))
    attrs = (" " + attrs) if attrs else ""
    inner = "".join(_outer_html(c) for c in n.children)
    return f"<{n.tag}{attrs}>{n.text}{inner}</{n.tag}>"


@dataclass
class FakeLocator:
    nodes: List[FakeNode]
    nth_idx: Optional[int] = None

    def _current(self) -> Optional[FakeNode]:
        if self.nth_idx is None:
            return self.nodes[0] if self.nodes else None
        if 0 <= self.nth_idx < len(self.nodes):
            return self.nodes[self.nth_idx]
        return None

    def count(self) -> int:
        return len(self.nodes)

    def nth(self, i: int) -> "FakeLocator":
        return FakeLocator(self.nodes, nth_idx=i)

    @property
    def first(self) -> "FakeLocator":
        """Property to match Playwright's ``Locator.first``."""
        return FakeLocator(self.nodes, nth_idx=0)

    # --- handle / inspect (used by session.safe_tag and friends) ---
    def element_handle(self) -> Optional["_FakeElementHandle"]:
        n = self._current()
        if n is None:
            return None
        return _FakeElementHandle(n)

    def evaluate(self, script: str, *, arg: Any = None) -> Any:
        """A *very* small evaluate: only the few expressions the session uses.

        Supported: ``el => el.tagName.toLowerCase()`` and ``el => el.innerText``.
        Anything else returns ``""`` so the session's safety net catches it.
        """
        n = self._current()
        if n is None:
            return None
        s = script.replace(" ", "").replace("\n", "")
        if "tagName.toLowerCase()" in s:
            return n.tag.lower()
        if "el.innerText" in s or "innerText" in s:
            return n.text or ""
        if "el.value" in s or "el=>el.value" in s.replace(" ", ""):
            return n.attrs.get("value", "")
        if "el.outerHTML" in s:
            return _outer_html(n)
        return ""

    def click(
        self,
        *,
        button: str = "left",
        click_count: int = 1,
        delay: Optional[float] = None,
        force: bool = False,
        timeout: int = 30_000,
    ) -> None:
        n = self._current()
        if n is None:
            raise RuntimeError("no element to click")
        n.attrs["__clicked__"] = "1"
        n.attrs["__click_button__"] = button
        n.attrs["__click_count__"] = str(click_count)

    def hover(self, *, timeout: int = 30_000) -> None:
        n = self._current()
        if n is None:
            raise RuntimeError("no element to hover")
        n.attrs["__hovered__"] = "1"

    def fill(self, text: str, *, timeout: int = 30_000) -> None:
        n = self._current()
        if n is None:
            raise RuntimeError("no element to fill")
        n.attrs["value"] = text
        n.text = text

    def type(
        self, text: str, *, delay: Optional[float] = None, timeout: int = 30_000
    ) -> None:
        self.fill(text, timeout=timeout)

    def select_option(
        self,
        *,
        value: Optional[str] = None,
        label: Optional[str] = None,
        timeout: int = 30_000,
    ) -> None:
        n = self._current()
        if n is None:
            raise RuntimeError("no element to select")
        if value is not None:
            n.attrs["value"] = value
        if label is not None:
            n.attrs["selected_label"] = label

    def inner_text(self, *, timeout: int = 30_000) -> str:
        n = self._current()
        return n.text if n is not None else ""

    def text_content(self, *, timeout: int = 30_000) -> str:
        return self.inner_text(timeout=timeout)

    def get_attribute(self, name: str, *, timeout: int = 30_000) -> Optional[str]:
        n = self._current()
        if n is None:
            return None
        return n.attrs.get(name)

    def is_visible(self) -> bool:
        n = self._current()
        if n is None:
            return False
        return n.attrs.get("hidden") != "1"

    def wait_for(self, *, state: str, timeout: int = 30_000) -> None:
        n = self._current()
        if state == "visible":
            if n is None or n.attrs.get("hidden") == "1":
                raise RuntimeError(f"not visible: {state}")
        elif state == "hidden":
            if n is not None and n.attrs.get("hidden") != "1":
                raise RuntimeError(f"not hidden: {state}")
        elif state == "attached":
            if n is None:
                raise RuntimeError(f"not attached: {state}")

    def scroll_into_view_if_needed(self, *, timeout: int = 30_000) -> None:
        n = self._current()
        if n is None:
            raise RuntimeError("no element to scroll")
        n.attrs["__scrolled_into_view__"] = "1"

    def all(self) -> List["FakeLocator"]:
        return [FakeLocator(self.nodes, nth_idx=i) for i in range(len(self.nodes))]


# ---------------------------------------------------------------------------
# Fake Mouse / Keyboard
# ---------------------------------------------------------------------------


@dataclass
class FakeMouse:
    actions: List[Tuple[int, int]] = field(default_factory=list)

    def wheel(self, dx: int, dy: int) -> None:
        self.actions.append((dx, dy))


@dataclass
class FakeKeyboard:
    presses: List[str] = field(default_factory=list)

    def press(self, key: str) -> None:
        self.presses.append(key)


# ---------------------------------------------------------------------------
# Fake Page
# ---------------------------------------------------------------------------


@dataclass
class FakePage:
    url: str = "about:blank"
    title: str = ""
    dom: FakeNode = field(default_factory=lambda: FakeNode(tag="#document"))
    actions_log: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)
    mouse: FakeMouse = field(default_factory=FakeMouse)
    keyboard: FakeKeyboard = field(default_factory=FakeKeyboard)
    download: Dict[str, Any] = field(default_factory=dict)

    # --- navigation
    def goto(self, url: str, wait_until: str = "load", timeout: int = 30_000) -> None:
        self.url = url
        self.actions_log.append(("goto", {"url": url, "wait_until": wait_until}))

    def go_back(self, timeout: int = 30_000, wait_until: str = "load") -> None:
        self.actions_log.append(("go_back", {"timeout": timeout}))

    def go_forward(self, timeout: int = 30_000, wait_until: str = "load") -> None:
        self.actions_log.append(("go_forward", {"timeout": timeout}))

    def reload(self, timeout: int = 30_000, wait_until: str = "load") -> None:
        self.actions_log.append(("reload", {"timeout": timeout}))

    # --- locating
    def locator(self, selector: str) -> FakeLocator:
        nodes = self.dom.find_css(selector)
        return FakeLocator(nodes=nodes)

    def get_by_role(self, role: str, name: Optional[str] = None) -> FakeLocator:
        out: List[FakeNode] = []

        def walk(n: FakeNode) -> None:
            if n.role == role:
                if name is None or n.name == name:
                    out.append(n)
            for c in n.children:
                walk(c)

        walk(self.dom)
        return FakeLocator(nodes=out)

    def get_by_text(self, text: str, *, exact: bool = False) -> FakeLocator:
        out: List[FakeNode] = []

        def walk(n: FakeNode) -> None:
            nt = n.text or ""
            if exact:
                if nt == text:
                    out.append(n)
            else:
                if text in nt:
                    out.append(n)
            for c in n.children:
                walk(c)

        walk(self.dom)
        return FakeLocator(nodes=out)

    def get_by_test_id(self, test_id: str) -> FakeLocator:
        out: List[FakeNode] = []

        def walk(n: FakeNode) -> None:
            if n.test_id == test_id:
                out.append(n)
            for c in n.children:
                walk(c)

        walk(self.dom)
        return FakeLocator(nodes=out)

    # --- extracts (used by _snapshot)
    def inner_text(self, selector: str) -> str:
        nodes = self.dom.find_css(selector)
        if not nodes:
            return ""
        out: List[str] = []

        def collect(n: FakeNode) -> None:
            if n.text:
                out.append(n.text)
            for c in n.children:
                collect(c)

        collect(nodes[0])
        return "\n".join(out)

    def content(self) -> str:
        # Build a coarse HTML string from the DOM.
        def html(n: FakeNode) -> str:
            attrs = " ".join(
                f'{k}="{v}"' for k, v in n.attrs.items() if not k.startswith("__")
            )
            attrs = (" " + attrs) if attrs else ""
            inner = "".join(html(c) for c in n.children)
            return f"<{n.tag}{attrs}>{n.text}{inner}</{n.tag}>"

        return html(self.dom)

    def wait_for_load_state(self, state: str, *, timeout: int = 30_000) -> None:
        self.actions_log.append(("wait_for_load_state", {"state": state}))

    def screenshot(self, *, path: str) -> None:
        # Fake: write an empty file.
        with open(path, "wb") as f:
            f.write(b"")

    def expect_download(self):
        return _ExpectDownload(self)

    def cookies(self) -> List[Dict[str, str]]:
        return []


class _ExpectDownload:
    """Minimal implementation of ``page.expect_download`` context manager."""

    def __init__(self, page: FakePage) -> None:
        self.page = page

    def __enter__(self) -> "_ExpectDownload":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        pass

    @property
    def value(self) -> "_FakeDownload":
        return _FakeDownload(self.page)


@dataclass
class _FakeDownload:
    page: FakePage

    def save_as(self, path: str) -> None:
        with open(path, "wb") as f:
            f.write(b"FAKE-DOWNLOAD")


# ---------------------------------------------------------------------------
# Fake Context / Browser
# ---------------------------------------------------------------------------


class FakeContext:
    def __init__(self) -> None:
        self.pages: List[FakePage] = []

    def new_page(self) -> FakePage:
        p = FakePage()
        self.pages.append(p)
        return p


class FakeBrowser:
    def __init__(self) -> None:
        self.contexts: List[FakeContext] = []
        self._closed = False

    def new_context(self, viewport: Optional[Dict[str, int]] = None) -> FakeContext:
        c = FakeContext()
        self.contexts.append(c)
        return c

    def close(self) -> None:
        self._closed = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_simple_dom() -> FakeNode:
    """Build a small DOM that exercises every locator kind."""

    root = FakeNode(tag="#document")
    body = FakeNode(tag="body", parent=root)
    root.children.append(body)

    # Headline
    h1 = FakeNode(tag="h1", text="Phase 8 Browser Tests", parent=body)
    body.children.append(h1)

    # Form
    form = FakeNode(tag="form", parent=body, attrs={"id": "login"})
    body.children.append(form)
    label = FakeNode(tag="label", text="Username", parent=form, attrs={"for": "u"})
    form.children.append(label)
    inp = FakeNode(
        tag="input",
        parent=form,
        attrs={"id": "u", "name": "username", "type": "text"},
    )
    form.children.append(inp)
    inp2 = FakeNode(
        tag="input",
        parent=form,
        attrs={"id": "p", "name": "password", "type": "password"},
    )
    form.children.append(inp2)
    submit = FakeNode(
        tag="button",
        text="Sign in",
        parent=form,
        attrs={"type": "submit", "id": "go", "data-testid": "submit-btn"},
    )
    form.children.append(submit)

    # Select
    sel = FakeNode(
        tag="select",
        parent=form,
        attrs={"id": "country", "name": "country"},
    )
    form.children.append(sel)
    for v, t in [("us", "United States"), ("ca", "Canada"), ("mx", "Mexico")]:
        opt = FakeNode(tag="option", parent=sel, attrs={"value": v}, text=t)
        sel.children.append(opt)

    # Link with aria-label
    link = FakeNode(
        tag="a",
        text="Help",
        parent=body,
        attrs={"href": "/help", "aria-label": "Help center"},
    )
    body.children.append(link)

    # Ambiguous button (two buttons with the same text)
    body2 = FakeNode(tag="body", parent=root)
    root.children.append(body2)
    for i in range(2):
        b = FakeNode(
            tag="button",
            text="Delete",
            parent=body2,
            attrs={"id": f"del-{i}", "data-testid": f"delete-{i}"},
        )
        body2.children.append(b)

    # Hidden element
    hidden = FakeNode(
        tag="div",
        text="You can't see me",
        parent=body2,
        attrs={"id": "hidden", "hidden": "1"},
    )
    body2.children.append(hidden)

    return root


def fake_browser_factory() -> FakeBrowser:
    """A factory suitable for BrowserSession(playwright_factory=...)."""
    return FakeBrowser()


def install_fixture_page(
    browser: FakeBrowser, dom: Optional[FakeNode] = None
) -> FakePage:
    """Pre-create a context + page with a fixture DOM; return the page."""
    ctx = browser.new_context()
    page = ctx.new_page()
    page.dom = dom if dom is not None else build_simple_dom()
    page.url = "file:///fixture/index.html"
    page.title = "Phase 8 Fixture"
    return page

def fixture_session(*, headless: bool = True) -> BrowserSession:
    """Create an open BrowserSession with the simple fixture page attached.

    ``BrowserSession.open()`` already creates a context and a page, so we
    reuse that page and attach the fixture DOM to it.  Creating a second
    context would leave ``s._page`` pointing at the empty first page.
    """
    from browser.session.session import BrowserSession

    browser = fake_browser_factory()
    s = BrowserSession("test", headless=headless, playwright_factory=lambda: browser)
    s.open()
    # s.open() already produced s._page; just give it a populated DOM.
    s._page.dom = build_simple_dom()  # type: ignore[attr-defined]
    s._page.url = "file:///fixture/index.html"  # type: ignore[attr-defined]
    s._page.title = "Phase 8 Fixture"  # type: ignore[attr-defined]
    return s
