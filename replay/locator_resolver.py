"""
Locator resolution: given a schemas.artifact.Locator (possibly with a
fallback chain) and the current input params, find the real Playwright
element it refers to.

This is the replay-side counterpart to agent/locator_capture.py, which
built these locators at discovery time. Resolution tries the primary
strategy first; if it fails (element not found, or found but not
visible/enabled within the step timeout), it falls back to the next
locator in the chain. This is the concrete mechanism behind "replay
must use stable element/control targeting" and "handle the errors
that legitimately occur at runtime" -- a locator chain gives replay
something to fall back on before concluding a step has truly failed,
rather than failing on the first strategy that doesn't immediately
resolve.

Parameter substitution: any locator value containing {{param_name}}
is substituted with the caller-supplied param value before resolution
-- this is how a ROW_CONTAINS locator recorded as "{{member_id}}"
becomes "12345" (or whichever ID the caller passed) at replay time.
"""

from __future__ import annotations

import re
from typing import Optional

from playwright.sync_api import Page, Locator as PWLocator

from schemas.artifact import Locator, LocatorStrategy

_PARAM_PATTERN = re.compile(r"\{\{(\w+)\}\}")


def substitute_params(value: str, params: dict) -> str:
    """Replace every {{param_name}} in `value` with params[param_name].
    Raises if a referenced param wasn't supplied -- a locator that
    can't be fully resolved is a hard failure, not a partial match."""
    def _replace(match: re.Match) -> str:
        name = match.group(1)
        if name not in params:
            raise ValueError(f"Locator references {{{{{name}}}}} but no such param was supplied")
        return str(params[name])
    return _PARAM_PATTERN.sub(_replace, value)


def _resolve_one(page: Page, locator: Locator, params: dict) -> Optional[PWLocator]:
    """Try exactly one Locator (no fallback traversal) and return a
    Playwright Locator handle if it resolves to at least one visible
    element, else None. Never raises for "not found" -- that's a
    normal, expected outcome the caller uses to decide whether to try
    the fallback."""
    value = substitute_params(locator.value, params)

    if locator.strategy == LocatorStrategy.CSS:
        pw_locator = page.locator(value)

    elif locator.strategy == LocatorStrategy.ROLE:
        name = substitute_params(locator.name, params) if locator.name else None
        pw_locator = page.get_by_role(value, name=name) if name else page.get_by_role(value)

    elif locator.strategy == LocatorStrategy.TEXT:
        pw_locator = page.get_by_text(value)

    elif locator.strategy == LocatorStrategy.TEST_ID:
        pw_locator = page.get_by_test_id(value)

    elif locator.strategy == LocatorStrategy.ROW_CONTAINS:
        # value is the (now-substituted) content to search for within a
        # row; `name` is "role::accessibleName" identifying the target
        # element within that row -- see locator_capture.py for how
        # this was captured.
        row = page.locator("tr", has_text=value)
        role, _, accessible_name = (locator.name or "").partition("::")
        pw_locator = row.get_by_role(role, name=accessible_name) if role else row

    else:
        raise ValueError(f"Unknown locator strategy: {locator.strategy}")

    try:
        count = pw_locator.count()
    except Exception:
        return None

    if count == 0:
        return None
    return pw_locator.first


def resolve_locator(page: Page, locator: Locator, params: dict, timeout_ms: int = 5000) -> PWLocator:
    """
    Resolve a Locator (trying primary, then each fallback in the
    chain) to a real, visible Playwright element. Raises RuntimeError
    with a clear, debuggable message -- naming every strategy that was
    tried -- if the entire chain fails to resolve within timeout_ms.
    This exhausted-chain case is what the replay engine treats as a
    hard failure per the result contract (result.py).
    """
    tried: list[str] = []
    current: Optional[Locator] = locator

    while current is not None:
        tried.append(f"{current.strategy.value}='{current.value}'")
        pw_locator = _resolve_one(page, current, params)
        if pw_locator is not None:
            try:
                pw_locator.wait_for(state="visible", timeout=timeout_ms)
                return pw_locator
            except Exception:
                pass  # visible-wait failed -- fall through to next in chain
        current = current.fallback

    raise RuntimeError(
        f"Could not resolve locator after trying all strategies in the fallback chain: "
        f"{' -> '.join(tried)}"
    )