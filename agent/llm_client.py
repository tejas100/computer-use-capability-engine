"""
Thin wrapper around the OpenAI API for the discovery loop's "decide"
step: given a screenshot + goal + action history, get back exactly
one structured AgentAction.

We use function calling (not free-text parsing) so the loop never
has to guess what the model meant -- this is the same reliability
argument as the artifact schema itself: force structure at the
boundary.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Optional

from openai import OpenAI

from agent.actions import AgentAction, AgentActionType

MODEL = "gpt-4o"

_DECIDE_ACTION_TOOL = {
    "type": "function",
    "function": {
        "name": "decide_action",
        "description": "Choose exactly one action to take next in the browser, given the current screenshot and the goal.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [a.value for a in AgentActionType],
                    "description": "The single action to perform next.",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Brief reasoning for why this action moves toward the goal.",
                },
                "x": {"type": "integer", "description": "X pixel coordinate, for 'click'."},
                "y": {"type": "integer", "description": "Y pixel coordinate, for 'click'."},
                "target_description": {"type": "string", "description": "Human-readable name of what's being clicked, e.g. 'the Search button'."},
                "text": {"type": "string", "description": "Text to type, for 'type'. Assumes a field is already focused."},
                "url": {"type": "string", "description": "URL to navigate to, for 'navigate'."},
                "key": {"type": "string", "description": "Key to press, for 'press_key', e.g. 'Enter'."},
                "dialog_action": {"type": "string", "enum": ["accept", "dismiss"], "description": "For 'handle_dialog'."},
                "outcome_summary": {"type": "string", "description": "For 'done': what was observed that confirms the goal was reached."},
                "stuck_reason": {"type": "string", "description": "For 'stuck': why the agent cannot safely proceed."},
            },
            "required": ["action", "reasoning"],
        },
    },
}

SYSTEM_PROMPT = """You are an automation agent operating a web browser to accomplish a goal \
inside an internal credit-union back-office tool. You act ONLY through the decide_action tool \
-- one action per turn.

The screenshot you are shown has a red coordinate grid overlaid on it: a labeled gridline every \
100 pixels (0, 100, 200, ...) and an unlabeled fine gridline every 50 pixels in between. This \
grid is a visual aid only -- it is not part of the real page.

To pick a coordinate: find the two nearest labeled gridlines to your target (one on each side), \
then count fine gridlines from there to land precisely on it. Do NOT estimate a target's position \
based on where a *different* element was -- e.g. do not reason "the button is probably 150px to \
the right of the input field I clicked earlier." Always re-read the grid fresh against the current \
screenshot for the specific element you are targeting now.

Rules:
- Look carefully at the screenshot before deciding. Coordinates are in CSS pixels from the \
top-left of the viewport, using the overlaid grid to pinpoint the exact position.
- Take one small, verifiable action at a time. Don't try to plan multiple steps into a single action.
- If you see a clear business outcome (e.g. "no member found", "requires supervisor approval", \
a validation error) that answers the goal, treat that as task completion: use 'done' and \
summarize the outcome, even if it isn't the "happy path."
- If you are uncertain, about to take an irreversible action outside what the goal describes, \
or the UI is in a state you don't recognize after 2 attempts, use 'stuck' and explain why -- \
do not guess repeatedly.
- Never invent data (member IDs, amounts) beyond what the goal specifies.
- If your history shows a previous click missed its target (landed on empty page background), \
look at the gridlines more carefully and re-estimate -- don't repeat the same coordinates.
"""


def _encode_screenshot(png_bytes: bytes) -> str:
    return base64.b64encode(png_bytes).decode("utf-8")


def decide_next_action(
    goal: str,
    screenshot_png: bytes,
    history: list[str],
    dialog_showing: Optional[str] = None,
) -> AgentAction:
    """
    Ask GPT-4o to look at the current screenshot and decide the single
    next action toward `goal`. `history` is a list of short strings
    describing prior actions this run, most recent last.
    """
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    history_text = "\n".join(f"{i+1}. {h}" for i, h in enumerate(history)) or "(no actions taken yet)"
    dialog_note = (
        f"\n\nA native browser dialog is currently showing with this message: \"{dialog_showing}\". "
        f"You must handle_dialog before anything else."
        if dialog_showing else ""
    )

    user_text = (
        f"GOAL: {goal}\n\n"
        f"ACTIONS TAKEN SO FAR:\n{history_text}"
        f"{dialog_note}\n\n"
        f"Here is the current screenshot. Decide the single next action."
    )

    response = client.chat.completions.create(
        model=MODEL,
        max_tokens=1024,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{_encode_screenshot(screenshot_png)}"
                        },
                    },
                ],
            },
        ],
        tools=[_DECIDE_ACTION_TOOL],
        tool_choice={"type": "function", "function": {"name": "decide_action"}},
    )

    message = response.choices[0].message
    if message.tool_calls:
        call = message.tool_calls[0]
        if call.function.name == "decide_action":
            args = json.loads(call.function.arguments)
            return AgentAction(**args)

    raise RuntimeError("Model did not return a decide_action tool call")