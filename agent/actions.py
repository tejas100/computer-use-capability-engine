"""
Action schema: the structured output the discovery LLM must produce
each step of the observe -> decide -> act loop.

We force Claude to respond with one of these action types via tool
use (see llm_client.py) rather than parsing free text, so the loop
never has to guess what the model "meant."

Design note: the LLM reasons over a *screenshot* (pixels), and
CLICK/coordinate-based actions carry an (x, y) point. It is the agent
loop's job -- not the LLM's -- to resolve whatever DOM element sits
under that point into a real locator, immediately after the click
succeeds. That's what makes the resulting artifact locator-based
even though the decision was vision-based. See agent/discover.py.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class AgentActionType(str, Enum):
    CLICK = "click"                # click at a screen coordinate
    TYPE = "type"                  # type text into whatever currently has focus
    NAVIGATE = "navigate"          # go to a URL directly
    PRESS_KEY = "press_key"        # e.g. "Enter", "Tab"
    HANDLE_DIALOG = "handle_dialog"  # accept or dismiss a native browser dialog
    DONE = "done"                  # goal is complete, stop the loop
    STUCK = "stuck"                # agent cannot safely proceed, escalate to a human


class AgentAction(BaseModel):
    action: AgentActionType
    reasoning: str = Field(description="Why the model chose this action -- kept for the evidence log")

    # click
    x: Optional[int] = None
    y: Optional[int] = None
    target_description: Optional[str] = None  # human-readable, e.g. "the Search button"

    # type
    text: Optional[str] = None

    # navigate
    url: Optional[str] = None

    # press_key
    key: Optional[str] = None

    # handle_dialog
    dialog_action: Optional[str] = None  # "accept" | "dismiss"

    # done — what checkpoint/output was observed, for logging
    outcome_summary: Optional[str] = None

    # stuck — why, for the escalation payload
    stuck_reason: Optional[str] = None