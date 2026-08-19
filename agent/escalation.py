"""
Human-in-the-loop escalation: when the discovery agent or the replay
engine cannot safely proceed, this writes an intervention request
carrying enough context for a human operator to act on it.

Scope note (see /REPORT.md section 5): a full real-time co-browsing
operator console is out of scope per the brief. What's real here is
the escalation payload itself, and the pause/resume contract -- see
human_handoff/handoff.py for how a human actually takes control of the
*same* live session and hands it back. This module only handles
raising and recording the request.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path


def raise_intervention_request(
    capability_or_goal: str,
    current_step: int,
    reason: str,
    screenshot_path: Path,
    evidence_dir: Path,
) -> dict:
    """
    Write an intervention request to evidence_dir/intervention_request.json
    and return it. In a full system this would also push to wherever a
    human operator watches for these (a queue, a dashboard) -- that
    transport is stubbed here (see operator/handoff.py) since the brief
    scopes the operator console out, but the request shape itself is
    real and is what a human-facing surface would consume.
    """
    request = {
        "intervention_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "capability_or_goal": capability_or_goal,
        "current_step": current_step,
        "reason": reason,
        "screenshot": screenshot_path.name,
        "status": "awaiting_human",
    }

    path = evidence_dir / "intervention_request.json"
    path.write_text(json.dumps(request, indent=2))
    return request