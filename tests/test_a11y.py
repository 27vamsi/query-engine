"""Contrast guards for the design tokens.

The palette is dark and the dim text tiers sit close to their backgrounds, so
"looks fine on my monitor" is not a safe check. These tests parse
``static/css/tokens.css`` and compute real WCAG 2.1 contrast ratios, which
means a future tweak that darkens a text token fails the suite instead of
quietly making the pruning explanations unreadable.

Deliberately parsing the stylesheet rather than duplicating the hex values
here: a test that hardcoded its own copy of the palette would keep passing
after someone edited the CSS.
"""

import re
from pathlib import Path

import pytest

TOKENS_CSS = Path(__file__).resolve().parent.parent / "static" / "css" / "tokens.css"

# The surfaces a text token can legally be painted on. --c-raised is the
# lightest, so it is the worst case for light-on-dark text. --c-raised-hi is
# excluded on purpose: it is only ever the generic `button:hover` fill, and
# both components that put dim text inside a <button> (.tab, .col-btn)
# override that fill with their own background.
SURFACES = ["--c-canvas", "--c-surface", "--c-panel", "--c-panel-2", "--c-raised"]

# Ordered brightest to dimmest. All four carry small body text somewhere in
# the UI, so all four are held to AA 4.5:1.
TEXT_TIERS = ["--c-text", "--c-text-dim", "--c-text-mute", "--c-text-faint"]

# Status / accent colors are used for numbers and short labels at >= 14px and
# for 1px UI boundaries, so AA Large (3:1) is the applicable threshold.
ACCENTS = [
    "--c-accent",
    "--c-accent-hi",
    "--c-good",
    "--c-warn",
    "--c-bad",
    "--c-info",
    "--c-violet",
]

AA_SMALL = 4.5
AA_LARGE = 3.0


def _tokens() -> dict[str, str]:
    css = TOKENS_CSS.read_text(encoding="utf-8")
    return dict(re.findall(r"(--c-[\w-]+):\s*(#[0-9a-fA-F]{6});", css))


def _rgb(value: str) -> tuple[int, int, int]:
    return tuple(int(value[i : i + 2], 16) for i in (1, 3, 5))


def _channel(c: int) -> float:
    srgb = c / 255
    return srgb / 12.92 if srgb <= 0.04045 else ((srgb + 0.055) / 1.055) ** 2.4


def _luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = (_channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: str, bg: str) -> float:
    """WCAG 2.1 relative-contrast ratio between two #rrggbb strings."""
    a, b = _luminance(_rgb(fg)), _luminance(_rgb(bg))
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def test_contrast_helper_matches_known_reference_values():
    """Guards the math itself, so a broken formula cannot green-light the rest."""
    assert contrast("#ffffff", "#000000") == pytest.approx(21.0, abs=0.01)
    assert contrast("#000000", "#000000") == pytest.approx(1.0, abs=0.01)
    # Mid grey on white, a widely published reference point.
    assert contrast("#777777", "#ffffff") == pytest.approx(4.48, abs=0.02)


def test_every_token_referenced_by_the_tests_exists():
    tokens = _tokens()
    for name in [*SURFACES, *TEXT_TIERS, *ACCENTS]:
        assert name in tokens, f"{name} is missing from tokens.css"


@pytest.mark.parametrize("tier", TEXT_TIERS)
@pytest.mark.parametrize("surface", SURFACES)
def test_text_tiers_meet_aa_for_small_text(tier, surface):
    tokens = _tokens()
    ratio = contrast(tokens[tier], tokens[surface])
    assert ratio >= AA_SMALL, (
        f"{tier} on {surface} is {ratio:.2f}:1, below the {AA_SMALL}:1 "
        f"WCAG AA floor for small text"
    )


@pytest.mark.parametrize("accent", ACCENTS)
@pytest.mark.parametrize("surface", SURFACES)
def test_accents_meet_aa_for_large_text_and_ui(accent, surface):
    tokens = _tokens()
    ratio = contrast(tokens[accent], tokens[surface])
    assert ratio >= AA_LARGE, (
        f"{accent} on {surface} is {ratio:.2f}:1, below the {AA_LARGE}:1 floor"
    )


def test_text_ladder_is_strictly_dimmer_at_each_step():
    """The four tiers have to stay visually ordered, or the hierarchy they
    exist to express is gone even though each one is individually readable."""
    tokens = _tokens()
    ratios = [contrast(tokens[t], tokens["--c-raised"]) for t in TEXT_TIERS]
    assert ratios == sorted(ratios, reverse=True), dict(zip(TEXT_TIERS, ratios))


def test_surfaces_are_ordered_darkest_to_lightest():
    """tokens.css documents these as 'darkest to lightest'; keep that true so
    the worst-case surface in the tests above really is --c-raised."""
    tokens = _tokens()
    lums = [_luminance(_rgb(tokens[s])) for s in SURFACES]
    assert lums == sorted(lums), dict(zip(SURFACES, lums))
