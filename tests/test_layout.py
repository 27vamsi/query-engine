"""Resolve the CSS cascade and assert the shell layout at real viewport widths.

Why this exists: the shell layout broke twice in ways that every other test
was blind to. A page can return HTTP 200 with perfect context and still render
the whole application inside a 240px strip, because the bug lives in the
cascade -- a rule whose only positioning sat inside a media query, so it
applied to phones and not to desktops.

So this module implements just enough of the cascade to answer "at viewport
W, what is the winning value of property P for element E?": stylesheets are
read in <link> order, @media blocks are filtered by width, and ties break on
(specificity, source order). No browser and no new dependencies.

It is not a general CSS engine. It handles the simple class/element selectors
this project uses and nothing more; if a selector grows a combinator the
helper should be extended rather than worked around.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS_DIR = Path(__file__).resolve().parent.parent / "static" / "css"

# Must mirror the <link> order in base.html: source order breaks cascade ties.
SHEETS = ["tokens.css", "base.css", "shell.css", "components.css", "workspace.css"]

DESKTOP = [1920, 1440, 1280]
DRAWER = [1024, 768, 375]


def _strip_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def _media_applies(query: str, viewport: int) -> bool:
    # Non-width queries never participate in layout decisions here.
    if "print" in query or "prefers-reduced-motion" in query:
        return False
    if m := re.search(r"max-width:\s*(\d+)px", query):
        return viewport <= int(m.group(1))
    if m := re.search(r"min-width:\s*(\d+)px", query):
        return viewport >= int(m.group(1))
    return False


def _rules(chunk: str):
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", chunk):
        decls = {}
        for decl in m.group(2).split(";"):
            if ":" in decl:
                prop, _, val = decl.partition(":")
                decls[prop.strip()] = val.strip()
        if decls:
            for sel in m.group(1).split(","):
                yield sel.strip(), decls


def _blocks(css: str, viewport: int):
    """Top-level rules plus the rules of every @media block that applies."""
    i = 0
    while i < len(css):
        at = css.find("@media", i)
        yield from _rules(css[i:at if at != -1 else len(css)])
        if at == -1:
            return
        open_brace = css.index("{", at)
        query = css[at + 6:open_brace]
        depth, j = 1, open_brace + 1
        while depth and j < len(css):
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            j += 1
        if _media_applies(query, viewport):
            yield from _rules(css[open_brace + 1:j - 1])
        i = j


def _specificity(sel: str) -> tuple[int, int, int]:
    return (
        sel.count("#"),
        sel.count(".") + len(re.findall(r"\[|:(?!:)", sel)),
        len(re.findall(r"(?:^|[\s>+~])([a-zA-Z]+)", sel)),
    )


def _root_vars() -> dict[str, str]:
    css = _strip_comments((CSS_DIR / "tokens.css").read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for sel, decls in _blocks(css, 1440):
        if sel == ":root":
            out.update({k: v for k, v in decls.items() if k.startswith("--")})
    return out


VARS = _root_vars()


def _expand(value: str) -> str:
    for _ in range(3):
        value = re.sub(
            r"var\(\s*(--[\w-]+)\s*\)",
            lambda m: VARS.get(m.group(1), m.group(0)),
            value,
        )
    return value


def computed(viewport: int, selector: str, prop: str) -> str | None:
    """Winning value of `prop` for an element matching `selector` exactly."""
    best: tuple[tuple, int] | None = None
    winner: str | None = None
    order = 0
    for sheet in SHEETS:
        css = _strip_comments((CSS_DIR / sheet).read_text(encoding="utf-8"))
        for sel, decls in _blocks(css, viewport):
            order += 1
            if sel != selector or prop not in decls:
                continue
            key = (_specificity(sel), order)
            if best is None or key > best:
                best, winner = key, decls[prop]
    return _expand(winner) if winner is not None else None


# --- The resolver has to be trustworthy before its results mean anything ---


def test_resolver_picks_the_later_rule_at_equal_specificity():
    """tokens.css declares --w-sidebar once; shell.css consumes it. If source
    order were mishandled every assertion below would be meaningless."""
    assert computed(1440, ".shell", "display") == "grid"
    assert VARS["--w-sidebar"] == "240px"


def test_resolver_respects_media_query_width():
    """.shell collapses to one column only at <=1024px."""
    assert "240px" in computed(1440, ".shell", "grid-template-columns")
    assert computed(768, ".shell", "grid-template-columns") == "minmax(0, 1fr)"


# --- Desktop: sidebar left, content right ---------------------------------


@pytest.mark.parametrize("viewport", DESKTOP)
def test_desktop_is_fixed_sidebar_plus_flexible_content(viewport):
    cols = computed(viewport, ".shell", "grid-template-columns")
    assert cols == "240px minmax(0, 1fr)", (
        f"at {viewport}px the shell must be a 240px sidebar column plus a "
        f"flexible content column, got {cols!r}"
    )


@pytest.mark.parametrize("viewport", DESKTOP)
def test_collapsed_sidebar_is_64px(viewport):
    cols = computed(viewport, ".shell.is-collapsed", "grid-template-columns")
    assert cols == "64px minmax(0, 1fr)", cols


@pytest.mark.parametrize("viewport", DESKTOP)
def test_scrim_is_out_of_flow_on_desktop(viewport):
    """.scrim is the third in-flow child of a 2-column grid. In flow it takes
    the sidebar column, pushes .sidebar into the content column and wraps
    .main onto row 2 at sidebar width -- the entire app in a 240px strip.
    This is the exact bug that broke every desktop viewport."""
    display = computed(viewport, ".scrim", "display")
    position = computed(viewport, ".scrim", "position")
    assert display == "none" or position in {"fixed", "absolute"}, (
        f"at {viewport}px .scrim is in flow (display={display!r}, "
        f"position={position!r}) and will consume a grid column"
    )


@pytest.mark.parametrize("viewport", DESKTOP)
def test_sidebar_is_sticky_full_height_on_desktop(viewport):
    assert computed(viewport, ".sidebar", "position") == "sticky"
    assert computed(viewport, ".sidebar", "height") == "100vh"


# --- Overflow containment -------------------------------------------------


@pytest.mark.parametrize("viewport", DESKTOP + DRAWER)
def test_grid_children_can_shrink(viewport):
    """Without min-width:0 a wide table or a long SQL line forces the content
    column wider than its track and the page scrolls horizontally."""
    assert computed(viewport, ".main", "min-width") == "0"
    assert computed(viewport, ".page", "min-width") == "0"


@pytest.mark.parametrize("viewport", DESKTOP)
def test_content_column_track_can_shrink(viewport):
    """minmax(0, 1fr) rather than 1fr, for the same reason."""
    assert "minmax(0, 1fr)" in computed(
        viewport, ".shell", "grid-template-columns"
    )


# --- Drawer behaviour below 1024px ---------------------------------------


@pytest.mark.parametrize("viewport", DRAWER)
def test_sidebar_becomes_an_offcanvas_drawer(viewport):
    assert computed(viewport, ".shell", "grid-template-columns") == "minmax(0, 1fr)"
    assert computed(viewport, ".sidebar", "position") == "fixed"
    assert computed(viewport, ".sidebar", "transform") == "translateX(-100%)"


@pytest.mark.parametrize("viewport", DRAWER)
def test_scrim_covers_the_viewport_when_the_drawer_is_open(viewport):
    assert computed(viewport, ".scrim", "display") == "block"
    assert computed(viewport, ".scrim", "position") == "fixed"


@pytest.mark.parametrize("viewport", DRAWER)
def test_collapsed_width_does_not_apply_to_the_drawer(viewport):
    """The drawer must open at full width even if the desktop sidebar was left
    collapsed, otherwise the mobile menu is a 64px sliver of icons."""
    assert computed(viewport, ".sidebar", "width") == "240px"


# --- Padding ------------------------------------------------------------


@pytest.mark.parametrize("viewport", DESKTOP)
def test_desktop_content_padding_is_in_range(viewport):
    padding = computed(viewport, ".page", "padding")
    top, sides = padding.split()[0], padding.split()[1]
    assert 24 <= int(top.rstrip("px")) <= 32, padding
    assert 24 <= int(sides.rstrip("px")) <= 32, padding
