"""
Locator capture: given a screen coordinate the LLM decided to click,
figure out the actual DOM element there and produce a robust locator
pair (primary CSS, fallback accessibility role+name) for the artifact.

This is the seam between "the model discovers visually" and "the
artifact replays via stable locators." The LLM never invents a CSS
selector -- it only ever points at pixels; we derive the selector
from the real DOM after the fact, which is far more reliable than
asking a vision model to hallucinate a correct selector string.

Locator preference order, encoded in _css_for_element's JS:
  1. id                      -- most stable, if present
  2. name attribute           -- common on form inputs in legacy apps
  3. tag + nth-of-type within parent -- last resort, still deterministic
The accessibility role/name is always computed as a fallback,
independent of which CSS strategy was used, since it survives
markup/class changes that would break a structural CSS path.
"""

from __future__ import annotations

from playwright.sync_api import Page

from schemas.artifact import Locator, LocatorStrategy

# Executed in-page via page.evaluate. Returns {css, tag, id, name, role, accessibleName}
# for whatever element is at the given point, or null if nothing there.
_INSPECT_JS = """
([x, y]) => {
    const el = document.elementFromPoint(x, y);
    if (!el) return null;

    function cssPath(node) {
        if (node.id) return '#' + node.id;
        if (node.getAttribute('name')) {
            return node.tagName.toLowerCase() + '[name="' + node.getAttribute('name') + '"]';
        }
        // fall back to tag + nth-of-type chain, bounded to 4 levels up
        let path = [];
        let cur = node;
        let depth = 0;
        while (cur && cur.nodeType === 1 && depth < 4) {
            let selector = cur.tagName.toLowerCase();
            if (cur.id) {
                selector = '#' + cur.id;
                path.unshift(selector);
                break;
            }
            const parent = cur.parentElement;
            if (parent) {
                const siblings = Array.from(parent.children).filter(c => c.tagName === cur.tagName);
                if (siblings.length > 1) {
                    const idx = siblings.indexOf(cur) + 1;
                    selector += ':nth-of-type(' + idx + ')';
                }
            }
            path.unshift(selector);
            cur = parent;
            depth++;
        }
        return path.join(' > ');
    }

    const role = el.getAttribute('role') ||
                 ({BUTTON: 'button', A: 'link', INPUT: 'textbox', SELECT: 'combobox'}[el.tagName] || null);
    const accessibleName = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 60);

    return {
        css: cssPath(el),
        tag: el.tagName.toLowerCase(),
        id: el.id || null,
        name: el.getAttribute('name') || null,
        role: role,
        accessibleName: accessibleName,
    };
}
"""


def capture_locator(page: Page, x: int, y: int) -> Locator:
    """
    Inspect the DOM at (x, y) and return a Locator with a CSS primary
    strategy and, when available, a role-based fallback. Raises if
    nothing is found at that point (the caller should treat this as a
    hard failure -- the click target vanished).
    """
    info = page.evaluate(_INSPECT_JS, [x, y])
    if info is None:
        raise RuntimeError(f"No DOM element found at ({x}, {y}) to capture a locator for")

    fallback = None
    if info["role"] and info["accessibleName"]:
        fallback = Locator(
            strategy=LocatorStrategy.ROLE,
            value=info["role"],
            name=info["accessibleName"],
        )

    return Locator(
        strategy=LocatorStrategy.CSS,
        value=info["css"],
        fallback=fallback,
    )


# Finds the nearest actually-interactive element (button/input/select/a)
# to a given point and reports its center + distance/direction. Used
# only on a miss, to turn "that click didn't work" into a precise
# correction the model can act on directly, instead of an undirected
# retry that (as observed) tends to drift further from the target with
# each attempt rather than converging on it.
_NEAREST_INTERACTIVE_JS = """
([x, y]) => {
    const candidates = Array.from(document.querySelectorAll('input, button, select, a, [role="button"]'));
    let best = null;
    let bestDist = Infinity;
    for (const el of candidates) {
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) continue;
        const cx = rect.left + rect.width / 2;
        const cy = rect.top + rect.height / 2;
        const dist = Math.hypot(cx - x, cy - y);
        if (dist < bestDist) {
            bestDist = dist;
            best = {cx: Math.round(cx), cy: Math.round(cy), tag: el.tagName.toLowerCase(),
                     text: (el.innerText || el.value || '').trim().slice(0, 40)};
        }
    }
    if (!best) return null;
    return {...best, distance: Math.round(bestDist)};
}
"""


def find_nearest_interactive_hint(page: Page, x: int, y: int) -> str:
    """
    Return a short, human-readable correction hint pointing at the
    nearest real interactive element to (x, y), e.g. "the nearest
    clickable element is a button ~62px to the right and 20px up, at
    approximately (650, 128)." Returns an empty string if no
    interactive elements exist on the page.
    """
    result = page.evaluate(_NEAREST_INTERACTIVE_JS, [x, y])
    if result is None:
        return ""

    dx = result["cx"] - x
    dy = result["cy"] - y
    horiz = f"{abs(dx)}px {'right' if dx > 0 else 'left'}" if dx else "0px horizontally"
    vert = f"{abs(dy)}px {'down' if dy > 0 else 'up'}" if dy else "0px vertically"
    label = f'"{result["text"]}"' if result["text"] else f"a {result['tag']}"

    return (
        f"The nearest actual clickable element to ({x}, {y}) is {label}, "
        f"located {horiz} and {vert} from your click, at approximately "
        f"({result['cx']}, {result['cy']})."
    )