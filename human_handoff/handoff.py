"""
Human-in-the-loop handoff: the mechanism by which a human operator
takes control of the SAME live browser session automation was using,
and hands it back so the run can resume or complete.

Per section 3.6: "Let the human operate the same live session the
automation was using -- not a fresh one -- perform the manual steps,
and then hand control back so the run can resume or complete...
automation must be able to pause, cede control, and resume on the
same session, and there must be a way to know who is (or should be)
in control."

Scope note (per section 4's "Scope note" and section 5): a full
real-time co-browsing operator console is explicitly out of scope.
What's real here, and load-bearing:

  1. The browser is launched with a CDP remote-debugging port open.
     This is what makes "the same live session" literal, not
     figurative: a human can connect a second, completely independent
     tool (Chrome's own chrome://inspect page, or any CDP client) to
     the EXACT running browser instance -- same cookies, same DOM
     state, same everything -- and manually drive it. This is a real
     mechanism, not a simulation of one.
  2. Control state is tracked explicitly (AUTOMATION vs HUMAN) so
     there is always a documented answer to "who is in control."
  3. Resume is signaled via a file the human creates when done
     (evidence/<run>/resume.signal) -- a deliberately minimal,
     scriptable resume mechanism rather than a built interactive
     console, per the brief's explicit scope note that a bare/mock
     operator surface is acceptable as long as the handoff mechanism
     and control-transfer model are real.
  4. What the human did is captured as evidence (a post-intervention
     screenshot at minimum) before automation resumes.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from playwright.sync_api import Page


class SessionControl(str, Enum):
    AUTOMATION = "automation"
    HUMAN = "human"


RESUME_POLL_INTERVAL_S = 2
DEFAULT_RESUME_TIMEOUT_S = 600  # 10 minutes -- long enough for a real human to notice and act


def pause_for_human(
    page: Page,
    evidence_dir: Path,
    intervention: dict,
    resume_timeout_s: int = DEFAULT_RESUME_TIMEOUT_S,
) -> dict:
    """
    Pause automation and cede control of `page`'s underlying browser
    session to a human, per the intervention request already written
    to evidence_dir/intervention_request.json (see escalation.py).

    Blocks (polling, not busy-waiting) until either:
      - evidence_dir/resume.signal appears (human is done), or
      - resume_timeout_s elapses with no signal (treated as an
        abandoned intervention -- the caller decides what to do with
        that, this function just reports it).

    Returns a handoff record describing what happened, and writes it
    to evidence_dir/handoff_record.json as durable evidence of the
    control transfer -- independent of whether replay ultimately
    succeeds after resuming.
    """
    cdp_endpoint = _extract_cdp_endpoint(page)

    record = {
        "intervention_id": intervention.get("intervention_id"),
        "paused_at": datetime.now(timezone.utc).isoformat(),
        "control": SessionControl.HUMAN.value,
        "cdp_endpoint": cdp_endpoint,
        "resumed_at": None,
        "resume_method": None,
        "post_intervention_screenshot": None,
    }
    _write_record(evidence_dir, record)

    resume_signal_path = evidence_dir / "resume.signal"
    print(f"\n{'='*70}")
    print("HUMAN INTERVENTION REQUIRED")
    print(f"{'='*70}")
    print(f"Reason: {intervention.get('reason')}")
    print(f"\nThe live browser session is still open. To take control of it:")
    if cdp_endpoint:
        print(f"  1. Open Chrome and navigate to: chrome://inspect")
        print(f"     (or connect any CDP client to: {cdp_endpoint})")
        print(f"  2. Find this page under 'Remote Target' and click 'inspect'")
        print(f"     -- this opens the SAME live tab automation was using.")
    else:
        print(f"  (CDP endpoint unavailable -- see automation's own browser window directly.)")
    print(f"\nWhen you have manually resolved the issue, create this file to resume:")
    print(f"  {resume_signal_path}")
    print(f"  e.g.:  touch {resume_signal_path}")
    print(f"{'='*70}\n")

    waited = 0
    while not resume_signal_path.exists():
        time.sleep(RESUME_POLL_INTERVAL_S)
        waited += RESUME_POLL_INTERVAL_S
        if waited >= resume_timeout_s:
            record["resume_method"] = "timeout"
            record["control"] = SessionControl.HUMAN.value  # never reclaimed
            _write_record(evidence_dir, record)
            return record

    # Human signaled done -- capture what the page looks like now,
    # before handing control back, as evidence of what they did.
    screenshot_path = evidence_dir / "post_intervention.png"
    try:
        page.screenshot(path=str(screenshot_path))
        record["post_intervention_screenshot"] = screenshot_path.name
    except Exception:
        pass  # best-effort; don't let a screenshot failure block the resume

    record["resumed_at"] = datetime.now(timezone.utc).isoformat()
    record["resume_method"] = "resume_signal"
    record["control"] = SessionControl.AUTOMATION.value
    _write_record(evidence_dir, record)

    resume_signal_path.unlink()  # consumed -- so a stale signal can't affect a future run
    return record


def _extract_cdp_endpoint(page: Page) -> Optional[str]:
    """
    Best-effort: verify a CDP endpoint is actually reachable (the
    browser was launched with --remote-debugging-port=9222, per
    discover.py / engine.py's launch calls) and return it, or None if
    not reachable. Actually checks via Chrome's own /json/version
    endpoint rather than assuming the port is open -- if the browser
    was launched without that flag, this correctly reports
    unavailable instead of handing back a URL that won't connect.
    """
    import urllib.request

    endpoint = "http://localhost:9222"
    try:
        with urllib.request.urlopen(f"{endpoint}/json/version", timeout=2) as resp:
            if resp.status == 200:
                return endpoint
    except Exception:
        pass
    return None


def _write_record(evidence_dir: Path, record: dict) -> None:
    path = evidence_dir / "handoff_record.json"
    path.write_text(json.dumps(record, indent=2))