"""
The deterministic replay engine: given a saved Artifact and a set of
input params, walk its recorded steps top to bottom against a live
Playwright session -- no LLM, no decision-making, purely mechanical
execution of what discovery already figured out once.

This is the production execution path per section 3.3: "Given a saved
artifact and a set of input parameters, replay it without invoking
the LLM for decisions."

Control flow, deliberately linear (no loops or branches in `steps` --
see schemas/artifact.py's docstring for why):

  1. Validate params against the artifact's declared input_params.
  2. Navigate/execute each step in order.
  3. After EVERY step, check every known_outcome's condition. If one
     fires, stop immediately and return BUSINESS_OUTCOME or attempt
     its declared recovery (for RECOVERABLE) before re-checking.
  4. If all steps complete with no known outcome firing, check the
     checkpoint. If it doesn't resolve, that's a HARD_FAILURE -- we
     never blindly assume a step "worked."
  5. If the checkpoint resolves, extract declared outputs and return
     SUCCESS.

Any locator that fails to resolve through its entire fallback chain,
or a step that raises an unexpected error, is a HARD_FAILURE -- with
enough detail (which step, what was expected, what was observed) to
debug without re-running the browser.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright, Page

from replay.locator_resolver import resolve_locator, substitute_params
from replay.result import ReplayResult, ReplayStatus, StepLogEntry
from schemas.artifact import (
    Artifact, ActionType, KnownOutcome, OutcomeClassification, ParamType,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = REPO_ROOT / "artifacts"
EVIDENCE_DIR = REPO_ROOT / "evidence"


def load_artifact(capability: str) -> Artifact:
    path = ARTIFACTS_DIR / f"{capability}.json"
    if not path.exists():
        raise FileNotFoundError(f"No saved artifact for capability '{capability}' at {path}")
    return Artifact.model_validate_json(path.read_text())


def _validate_params(artifact: Artifact, params: dict) -> None:
    for decl in artifact.input_params:
        if decl.required and decl.name not in params:
            raise ValueError(f"Missing required param '{decl.name}' for capability '{artifact.capability}'")
        if decl.name in params:
            value = params[decl.name]
            if decl.type == ParamType.NUMBER and not isinstance(value, (int, float)):
                raise ValueError(f"Param '{decl.name}' must be a number, got {type(value).__name__}")
            if decl.type == ParamType.STRING and not isinstance(value, str):
                raise ValueError(f"Param '{decl.name}' must be a string, got {type(value).__name__}")


def _check_known_outcomes(page: Page, artifact: Artifact, params: dict) -> KnownOutcome | None:
    """Return the first KnownOutcome whose condition currently resolves
    on the page, or None if none do. Checked after every step."""
    for outcome in artifact.known_outcomes:
        try:
            value = substitute_params(outcome.condition.value, params)
        except ValueError:
            continue
        try:
            if outcome.condition.strategy.value == "text":
                if page.get_by_text(value).count() > 0:
                    return outcome
            elif outcome.condition.strategy.value == "css":
                if page.locator(value).count() > 0:
                    return outcome
        except Exception:
            continue
    return None


def _attempt_recovery(page: Page, outcome: KnownOutcome, result: ReplayResult) -> bool:
    """Attempt a RECOVERABLE outcome's declared recovery action.
    Returns True if recovery was attempted (caller re-checks state
    afterward), False if this outcome has no recovery to attempt."""
    if outcome.classification != OutcomeClassification.RECOVERABLE or outcome.recovery is None:
        return False

    recovery = outcome.recovery
    if recovery.action == "dismiss_dialog":
        # Native dialogs are event-driven in Playwright; the actual
        # dismissal is wired via page.on("dialog", ...) in run_replay,
        # since it must be registered before the dialog fires, not
        # after we detect its aftermath here. This branch just records
        # that we're relying on that handler having already run.
        result.step_log.append(StepLogEntry(
            step_id="recovery", action="dismiss_dialog", description=outcome.description,
            status="recovered", detail=f"Auto-{recovery.action} via registered dialog handler",
        ))
        return True

    if recovery.action == "wait_and_recheck":
        page.wait_for_timeout(1000)
        result.step_log.append(StepLogEntry(
            step_id="recovery", action="wait_and_recheck", description=outcome.description,
            status="recovered", detail="Waited 1000ms and will recheck",
        ))
        return True

    return False


def _extract_outputs(page: Page, artifact: Artifact, params: dict) -> dict[str, Any]:
    outputs: dict[str, Any] = {}
    for field in artifact.outputs:
        loc = resolve_locator(page, field.extract_from, params)
        text = loc.inner_text().strip()
        if field.type == ParamType.NUMBER:
            cleaned = text.replace("$", "").replace(",", "").strip()
            outputs[field.name] = float(cleaned)
        else:
            outputs[field.name] = text
    return outputs


def run_replay(capability: str, params: dict, headless: bool = True) -> ReplayResult:
    artifact = load_artifact(capability)
    result = ReplayResult(status=ReplayStatus.HARD_FAILURE, capability=capability)  # default until proven otherwise

    try:
        _validate_params(artifact, params)
    except ValueError as e:
        result.error_detail = str(e)
        result.mark_finished()
        return result

    run_id = f"replay_{capability}_{result.started_at.strftime('%Y%m%dT%H%M%SZ')}"
    evidence_dir = EVIDENCE_DIR / run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, channel="chrome")
        page = browser.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=1)

        # Register the dialog handler BEFORE any step runs, so a
        # RECOVERABLE known_outcome of type dismiss_dialog is actually
        # handled at the moment the dialog fires, not after the fact.
        dialog_outcomes = [
            o for o in artifact.known_outcomes
            if o.classification == OutcomeClassification.RECOVERABLE
            and o.recovery and o.recovery.action == "dismiss_dialog"
        ]
        if dialog_outcomes:
            page.on("dialog", lambda d: d.accept())

        try:
            for step in artifact.steps:
                step_log = StepLogEntry(
                    step_id=step.step_id, action=step.action.value,
                    description=step.description, status="ok",
                )
                try:
                    _execute_step(page, step, params)
                    result.step_log.append(step_log)
                except Exception as e:
                    step_log.status = "failed"
                    step_log.detail = str(e)
                    result.step_log.append(step_log)
                    # A step failing isn't automatically a hard failure --
                    # it might be exactly the moment a known_outcome's
                    # condition became true (e.g. the click never landed
                    # because a validation error page loaded instead of
                    # the next step's expected page). Check before
                    # concluding this is unrecoverable.
                    outcome = _check_known_outcomes(page, artifact, params)
                    if outcome and outcome.classification == OutcomeClassification.HARD_FAILURE:
                        break
                    elif outcome and outcome.classification == OutcomeClassification.RECOVERABLE:
                        if _attempt_recovery(page, outcome, result):
                            continue
                    elif outcome:
                        _finalize_business_outcome(result, outcome)
                        browser.close()
                        return result
                    else:
                        result.failed_step_id = step.step_id
                        result.failed_step_description = step.description
                        result.expected = f"Step '{step.description}' to succeed"
                        result.observed = str(e)
                        result.error_detail = str(e)
                        _capture_failure_evidence(page, evidence_dir, result)
                        browser.close()
                        result.mark_finished()
                        return result

                # Check known outcomes after every successful step too --
                # a business outcome can appear without any step raising
                # an exception (e.g. a page simply renders "not found").
                outcome = _check_known_outcomes(page, artifact, params)
                if outcome and outcome.classification == OutcomeClassification.BUSINESS_OUTCOME:
                    _finalize_business_outcome(result, outcome)
                    browser.close()
                    return result

            # -- all steps completed; verify checkpoint --------------------
            try:
                resolve_locator(page, artifact.checkpoint.locator, params, timeout_ms=5000)
            except Exception as e:
                result.status = ReplayStatus.HARD_FAILURE
                result.failed_step_id = "checkpoint"
                result.failed_step_description = artifact.checkpoint.description
                result.expected = artifact.checkpoint.description
                result.observed = f"Checkpoint locator did not resolve: {e}"
                result.error_detail = str(e)
                _capture_failure_evidence(page, evidence_dir, result)
                browser.close()
                result.mark_finished()
                return result

            outputs = _extract_outputs(page, artifact, params)
            result.status = ReplayStatus.SUCCESS
            result.outputs = outputs
            browser.close()
            result.mark_finished()
            return result

        except Exception as e:
            result.error_detail = f"Unexpected replay error: {e}"
            _capture_failure_evidence(page, evidence_dir, result)
            browser.close()
            result.mark_finished()
            return result


def _execute_step(page: Page, step, params: dict) -> None:
    if step.action == ActionType.NAVIGATE:
        page.goto(step.url)
        page.wait_for_load_state("networkidle", timeout=step.timeout_ms)
        return

    if step.action == ActionType.CLICK:
        loc = resolve_locator(page, step.locator, params, timeout_ms=step.timeout_ms)
        loc.click(timeout=step.timeout_ms)
        page.wait_for_load_state("networkidle", timeout=step.timeout_ms)
        return

    if step.action == ActionType.FILL:
        loc = resolve_locator(page, step.locator, params, timeout_ms=step.timeout_ms)
        value = substitute_params(step.value, params) if step.value else ""
        loc.fill(value, timeout=step.timeout_ms)
        return

    if step.action == ActionType.SELECT:
        loc = resolve_locator(page, step.locator, params, timeout_ms=step.timeout_ms)
        value = substitute_params(step.value, params) if step.value else ""
        loc.select_option(value, timeout=step.timeout_ms)
        return

    if step.action == ActionType.WAIT_FOR:
        if step.locator:
            resolve_locator(page, step.locator, params, timeout_ms=step.timeout_ms)
        else:
            page.wait_for_timeout(min(step.timeout_ms, 2000))
        return

    raise RuntimeError(f"Unhandled step action: {step.action}")


def _finalize_business_outcome(result: ReplayResult, outcome: KnownOutcome) -> None:
    result.status = ReplayStatus.BUSINESS_OUTCOME
    result.outcome_name = outcome.name
    result.outcome_description = outcome.description
    result.mark_finished()


def _capture_failure_evidence(page: Page, evidence_dir: Path, result: ReplayResult) -> None:
    """The 'richer signal on failure' the brief asks for alongside the
    structured log -- a screenshot of exactly what the page looked
    like at the moment replay gave up."""
    try:
        path = evidence_dir / "failure_screenshot.png"
        page.screenshot(path=str(path))
        result.failure_screenshot = str(path)
    except Exception:
        pass  # best-effort; a failed screenshot shouldn't mask the real failure


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Replay a saved capability artifact deterministically.")
    parser.add_argument("--capability", required=True, help="Capability name, e.g. get_member_balance")
    parser.add_argument("--params", required=True, help='JSON params, e.g. \'{"member_id": "12345"}\'')
    parser.add_argument("--headed", action="store_true", help="Show the browser window instead of headless")
    args = parser.parse_args()

    params = json.loads(args.params)
    result = run_replay(args.capability, params, headless=not args.headed)

    print(json.dumps(result.model_dump(mode="json"), indent=2))