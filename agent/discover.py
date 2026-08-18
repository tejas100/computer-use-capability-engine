"""
The discovery loop: observe -> decide -> act, driven by an LLM
looking at screenshots, until the goal is met, a stopping condition
is hit, or the agent flags itself as stuck.

On success, the full action trace (with captured locators) is
converted into a schemas.artifact.Artifact and written to
/artifacts/<capability_name>.json -- the reusable capability that
replay/engine.py will later execute with no LLM in the loop.

Every step is also written to /evidence/<run_id>/ as a structured
log entry plus a screenshot, satisfying the observability
requirement independent of whether the run succeeds.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, Page, Dialog

from agent.actions import AgentAction, AgentActionType
from agent.llm_client import decide_next_action
from agent.locator_capture import capture_locator, find_nearest_interactive_hint
from agent.grid_overlay import add_grid_overlay
from schemas.artifact import (
    Artifact, Target, InputParam, ParamType, Step, ActionType, Locator,
    LocatorStrategy, Checkpoint, OutputField, KnownOutcome,
    OutcomeClassification, SafetyPolicy, RiskLevel, Metadata,
)

MAX_STEPS = 15
STEP_TIMEOUT_S = 30

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = REPO_ROOT / "artifacts"
EVIDENCE_DIR = REPO_ROOT / "evidence"


class DiscoveryRun:
    """
    Tracks one discovery run's mutable state: the recorded steps (as
    schema Step objects, built incrementally as actions succeed), the
    action-history strings shown to the LLM each turn, and the
    dialog state (Playwright dialogs are event-driven, so we stash
    the pending dialog here until the agent explicitly handles it).
    """

    def __init__(self, goal: str, capability_name: str, base_url: str, discovered_by_model: str):
        self.goal = goal
        self.capability_name = capability_name
        self.base_url = base_url
        self.discovered_by_model = discovered_by_model

        self.history: list[str] = []
        self.steps: list[Step] = []
        self.pending_dialog: Optional[Dialog] = None
        self.pending_dialog_message: Optional[str] = None

        self.run_id = f"{capability_name}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        self.evidence_dir = EVIDENCE_DIR / self.run_id
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.evidence_dir / "discovery_log.jsonl"

    def log(self, entry: dict) -> None:
        entry["ts"] = datetime.now(timezone.utc).isoformat()
        with open(self.log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def save_screenshot(self, page: Page, step_num: int) -> Path:
        path = self.evidence_dir / f"step_{step_num:02d}.png"
        page.screenshot(path=str(path))
        # Diagnostic: confirm the screenshot's actual pixel dimensions
        # match the viewport size we asked for. A mismatch here is
        # exactly what produces "clicks land nowhere near the target"
        # -- the model reasons over the screenshot's pixel grid, but
        # page.mouse.click(x, y) operates in the page's CSS pixel
        # space. If these two don't agree, every coordinate is wrong.
        from PIL import Image
        img = Image.open(path)
        viewport = page.viewport_size
        self.log({
            "event": "screenshot_dimensions",
            "step": step_num,
            "screenshot_px": [img.width, img.height],
            "viewport_css_px": [viewport["width"], viewport["height"]] if viewport else None,
        })
        return path


def _handle_dialog_event(run: DiscoveryRun, dialog: Dialog) -> None:
    # Playwright requires a dialog to be accepted/dismissed before any
    # further page interaction works. We don't auto-resolve it -- we
    # stash it and let the LLM see + explicitly handle it, since a
    # confirmation dialog is exactly the kind of "unexpected runtime
    # condition" the assignment wants surfaced, not silently eaten.
    run.pending_dialog = dialog
    run.pending_dialog_message = dialog.message
    run.log({"event": "dialog_appeared", "message": dialog.message})


def run_discovery(goal: str, capability_name: str, base_url: str) -> Optional[Path]:
    run = DiscoveryRun(goal, capability_name, base_url, discovered_by_model="gpt-4o")
    run.log({"event": "discovery_start", "goal": goal, "capability_name": capability_name, "base_url": base_url})

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="chrome")
        page = browser.new_page(
            viewport={"width": 1280, "height": 900},
            device_scale_factor=1,
        )
        page.on("dialog", lambda d: _handle_dialog_event(run, d))

        page.goto(base_url)
        run.steps.append(Step(
            step_id="s0", action=ActionType.NAVIGATE,
            description=f"Navigate to {base_url}", url=base_url,
        ))
        run.history.append(f"Navigated to {base_url}")

        outcome: Optional[AgentAction] = None
        consecutive_failures = 0
        MAX_CONSECUTIVE_FAILURES = 4

        for step_num in range(1, MAX_STEPS + 1):
            screenshot_path = run.save_screenshot(page, step_num)
            screenshot_bytes = screenshot_path.read_bytes()
            # The evidence screenshot stays clean (no grid); the LLM
            # sees a gridded copy to help it ground pixel coordinates
            # -- see grid_overlay.py for why this matters.
            gridded_bytes = add_grid_overlay(screenshot_bytes)

            decision = decide_next_action(
                goal=goal,
                screenshot_png=gridded_bytes,
                history=run.history,
                dialog_showing=run.pending_dialog_message,
            )
            run.log({
                "event": "llm_decision", "step": step_num,
                "action": decision.action.value, "reasoning": decision.reasoning,
                "screenshot": screenshot_path.name,
            })

            if decision.action == AgentActionType.DONE:
                outcome = decision
                run.log({"event": "goal_reached", "summary": decision.outcome_summary})
                break

            if decision.action == AgentActionType.STUCK:
                run.log({"event": "agent_stuck", "reason": decision.stuck_reason})
                _raise_escalation(run, page, decision)
                break

            try:
                result_desc = _execute_action(run, page, decision, step_num)
                run.history.append(f"Step {step_num}: {result_desc}")
                consecutive_failures = 0
            except Exception as e:
                consecutive_failures += 1
                run.log({"event": "action_execution_error", "step": step_num, "error": str(e),
                          "consecutive_failures": consecutive_failures})
                run.history.append(
                    f"Step {step_num}: {decision.action.value} FAILED -- {e}. "
                    f"Do not repeat the same action -- try a different approach."
                )
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    # The model isn't recovering on its own -- force an
                    # escalation rather than burn the remaining step
                    # budget on a repeated no-op. This is discovery's
                    # analogue of replay's hard-failure classification:
                    # don't blindly keep trying past a clear stuck state.
                    run.log({"event": "auto_escalation_stall_detected", "consecutive_failures": consecutive_failures})
                    stall_action = AgentAction(
                        action=AgentActionType.STUCK,
                        reasoning="Automatic stall detection",
                        stuck_reason=f"{consecutive_failures} consecutive action failures with no progress: {e}",
                    )
                    _raise_escalation(run, page, stall_action)
                    break

        else:
            run.log({"event": "max_steps_exceeded"})
            browser.close()
            return None

        if outcome is None:
            browser.close()
            return None

        # -- success: build and save the artifact -----------------------
        artifact_path = _finalize_artifact(run, outcome)
        browser.close()
        return artifact_path


def _execute_action(run: DiscoveryRun, page: Page, decision: AgentAction, step_num: int) -> str:
    """Perform the action and return a short, specific description of
    what actually happened -- this becomes the history line the model
    sees next turn, so it must say enough to distinguish "this landed
    on the real target" from "this landed somewhere ambiguous" instead
    of just echoing the model's own stated intent back at it."""
    if decision.action == AgentActionType.HANDLE_DIALOG:
        if run.pending_dialog is None:
            raise RuntimeError("handle_dialog called but no dialog is pending")
        if decision.dialog_action == "accept":
            run.pending_dialog.accept()
        else:
            run.pending_dialog.dismiss()
        run.log({"event": "dialog_handled", "action": decision.dialog_action})
        run.pending_dialog = None
        run.pending_dialog_message = None
        if run.steps:
            run.steps[-1].description += " (dismisses a confirmation dialog on submit)"
        return f"Dialog {decision.dialog_action}ed."

    if decision.action == AgentActionType.CLICK:
        locator = capture_locator(page, decision.x, decision.y)
        run.log({
            "event": "click_target_resolved", "step": step_num,
            "requested_xy": [decision.x, decision.y],
            "resolved_css": locator.value,
        })
        # A click that resolves to the page background, or to a
        # non-interactive container (a <td>, a layout <div>), is
        # treated as a miss -- not just an exact html/body hit. Only
        # actually-interactive elements (input/button/select/a) count
        # as landing on something clickable. This closes the gap where
        # a click on "table > tbody > tr > td" was silently counted as
        # a success even though clicking a table cell focuses nothing.
        NON_INTERACTIVE_HINTS = ("html", "body", "table", "tbody", "tr", "td", "div")
        looks_non_interactive = any(
            locator.value == h or locator.value.split(" > ")[-1].split(":")[0].split("[")[0] == h
            for h in NON_INTERACTIVE_HINTS
        )
        if looks_non_interactive:
            hint = find_nearest_interactive_hint(page, decision.x, decision.y)
            raise RuntimeError(
                f"Click at ({decision.x}, {decision.y}) resolved to '{locator.value}', which is "
                f"not an interactive element (input/button/select/link). This click will not focus "
                f"anything. {hint}"
            )
        page.mouse.click(decision.x, decision.y)
        page.wait_for_load_state("networkidle", timeout=STEP_TIMEOUT_S * 1000)
        run.steps.append(Step(
            step_id=f"s{step_num}", action=ActionType.CLICK,
            description=decision.target_description or decision.reasoning,
            locator=locator, timeout_ms=STEP_TIMEOUT_S * 1000,
        ))
        return f"Click SUCCEEDED -- landed on and focused '{locator.value}'."

    if decision.action == AgentActionType.TYPE:
        # The field must already be focused (typically by a preceding
        # CLICK). If nothing is focused, page.keyboard.type() would
        # silently no-op -- that's exactly the failure mode that
        # caused the model to loop on "the field is empty" without
        # ever getting an error to react to. Fail loudly instead, so
        # the loop sees a real action_execution_error and the model
        # gets that fact in its next turn's history.
        active_tag = page.evaluate(
            "document.activeElement && document.activeElement.tagName"
        )
        if active_tag not in ("INPUT", "TEXTAREA", "SELECT"):
            raise RuntimeError(
                f"'type' requires a focused input field, but the focused element is "
                f"<{active_tag or 'none'}>. Click the target field first."
            )
        locator = _locator_for_focused_element(page)
        page.keyboard.type(decision.text)
        run.steps.append(Step(
            step_id=f"s{step_num}", action=ActionType.FILL,
            description=f"Type '{decision.text}' into {decision.target_description or 'the focused field'}",
            locator=locator, value=decision.text, timeout_ms=STEP_TIMEOUT_S * 1000,
        ))
        return f"Type SUCCEEDED -- typed '{decision.text}' into '{locator.value}'."

    if decision.action == AgentActionType.NAVIGATE:
        page.goto(decision.url)
        run.steps.append(Step(
            step_id=f"s{step_num}", action=ActionType.NAVIGATE,
            description=decision.reasoning, url=decision.url,
        ))
        return f"Navigated to {decision.url}."

    if decision.action == AgentActionType.PRESS_KEY:
        page.keyboard.press(decision.key)
        page.wait_for_load_state("networkidle", timeout=STEP_TIMEOUT_S * 1000)
        run.steps.append(Step(
            step_id=f"s{step_num}", action=ActionType.WAIT_FOR,
            description=f"Press {decision.key}", timeout_ms=STEP_TIMEOUT_S * 1000,
        ))
        return f"Pressed key '{decision.key}'."

    raise RuntimeError(f"Unhandled action type: {decision.action}")


def _locator_for_focused_element(page: Page) -> Locator:
    info = page.evaluate("""
        () => {
            const el = document.activeElement;
            if (!el) return null;
            const css = el.id ? '#' + el.id :
                        (el.getAttribute('name') ? el.tagName.toLowerCase() + '[name="' + el.getAttribute('name') + '"]' : el.tagName.toLowerCase());
            return {css: css};
        }
    """)
    if info is None:
        raise RuntimeError("No focused element found to attach typed text to")
    return Locator(strategy=LocatorStrategy.CSS, value=info["css"])


def _raise_escalation(run: DiscoveryRun, page: Page, decision: AgentAction) -> None:
    """Write an intervention request to evidence -- see replay/escalation.py
    for the shared escalation payload shape used by both discovery and replay."""
    from agent.escalation import raise_intervention_request
    screenshot_path = run.evidence_dir / "stuck_state.png"
    page.screenshot(path=str(screenshot_path))
    raise_intervention_request(
        capability_or_goal=run.goal,
        current_step=len(run.steps),
        reason=decision.stuck_reason or "Agent flagged itself as stuck with no reason given",
        screenshot_path=screenshot_path,
        evidence_dir=run.evidence_dir,
    )


def _finalize_artifact(run: DiscoveryRun, outcome: AgentAction) -> Path:
    """
    Build the Artifact from the recorded steps. Input params, outputs,
    checkpoint, and known_outcomes are capability-specific -- for this
    project they're supplied per-capability in agent/capability_specs.py
    rather than inferred automatically, since inferring "what varies
    per invocation" from a single run is a much harder problem than
    this assignment's scope calls for. See /REPORT.md section 7 (Cuts).
    """
    from agent.capability_specs import CAPABILITY_SPECS

    spec = CAPABILITY_SPECS[run.capability_name]

    artifact = Artifact(
        capability=run.capability_name,
        version=1,
        description=spec["description"],
        target=Target(base_url=run.base_url),
        input_params=spec["input_params"],
        steps=run.steps,
        checkpoint=spec["checkpoint"],
        outputs=spec["outputs"],
        known_outcomes=spec["known_outcomes"],
        safety=spec["safety"],
        metadata=Metadata(discovered_by_model=run.discovered_by_model),
    )

    ARTIFACTS_DIR.mkdir(exist_ok=True)
    artifact_path = ARTIFACTS_DIR / f"{run.capability_name}.json"
    artifact_path.write_text(artifact.model_dump_json(indent=2))

    run.log({"event": "artifact_saved", "path": str(artifact_path)})
    return artifact_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run an LLM-driven discovery session against the mock bank.")
    parser.add_argument("--goal", required=True, help="Natural language goal for this run")
    parser.add_argument("--capability", required=True, help="Name for the resulting capability, e.g. get_member_balance")
    parser.add_argument("--base-url", default="http://127.0.0.1:5000", help="Target application base URL")
    args = parser.parse_args()

    result = run_discovery(args.goal, args.capability, args.base_url)
    if result:
        print(f"Discovery succeeded. Artifact saved to: {result}")
    else:
        print("Discovery did not complete successfully. Check evidence/ for the log.")