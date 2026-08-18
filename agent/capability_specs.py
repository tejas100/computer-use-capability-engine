"""
Per-capability contract specifications.

The discovery loop records *what the agent did* (the steps). It does
not, and should not, try to infer *what varies between invocations*
from a single run -- a single successful trace can't tell you which
value was incidental (this particular member ID) versus structural
(the URL pattern). So the parts of the artifact contract that require
that judgment -- input_params, outputs, checkpoint, known_outcomes,
safety -- are supplied here, per capability, and merged with the
recorded steps in discover.py::_finalize_artifact.

This is a deliberate scope decision (see /REPORT.md section 7): a
more sophisticated version could diff multiple discovery runs against
each other to infer which literals are parameters, but that's a
meaningfully harder problem than this assignment calls for.
"""

from schemas.artifact import (
    InputParam, ParamType, Checkpoint, OutputField, Locator,
    LocatorStrategy, KnownOutcome, OutcomeClassification,
    RecoveryAction, SafetyPolicy, RiskLevel,
)

CAPABILITY_SPECS = {

    "get_member_balance": {
        "description": "Look up a member by ID and read their current savings balance.",
        "input_params": [
            InputParam(name="member_id", type=ParamType.STRING, required=True,
                       description="The member ID to look up, e.g. '12345'."),
        ],
        "checkpoint": Checkpoint(
            description="The balance value is visible on the member detail page.",
            locator=Locator(strategy=LocatorStrategy.CSS, value="#balance-value"),
        ),
        "outputs": [
            OutputField(name="balance", type=ParamType.NUMBER,
                        description="The member's current savings balance.",
                        extract_from=Locator(strategy=LocatorStrategy.CSS, value="#balance-value")),
        ],
        "known_outcomes": [
            KnownOutcome(
                name="member_not_found",
                description="No member exists with the given ID.",
                condition=Locator(strategy=LocatorStrategy.TEXT, value="No member found for ID"),
                classification=OutcomeClassification.BUSINESS_OUTCOME,
            ),
        ],
        "safety": SafetyPolicy(risk_level=RiskLevel.SAFE, requires_confirmation=False,
                                notes="Read-only lookup; no state change."),
    },

    "update_member_phone": {
        "description": "Update the phone number on file for a member.",
        "input_params": [
            InputParam(name="member_id", type=ParamType.STRING, required=True,
                       description="The member ID whose phone number is being updated."),
            InputParam(name="new_phone", type=ParamType.STRING, required=True,
                       description="The new phone number, e.g. '555-123-4567'."),
        ],
        "checkpoint": Checkpoint(
            description="A success confirmation message is shown after submitting.",
            locator=Locator(strategy=LocatorStrategy.CSS, value="#update-success"),
        ),
        "outputs": [
            OutputField(name="confirmed_phone", type=ParamType.STRING,
                        description="The phone number now on file, as confirmed by the page.",
                        extract_from=Locator(strategy=LocatorStrategy.CSS, value="#update-success")),
        ],
        "known_outcomes": [
            KnownOutcome(
                name="member_not_found",
                description="No member exists with the given ID.",
                condition=Locator(strategy=LocatorStrategy.TEXT, value="No member found for ID"),
                classification=OutcomeClassification.BUSINESS_OUTCOME,
            ),
            KnownOutcome(
                name="invalid_phone_format",
                description="The submitted phone number failed validation.",
                condition=Locator(strategy=LocatorStrategy.CSS, value="#update-error"),
                classification=OutcomeClassification.BUSINESS_OUTCOME,
            ),
        ],
        "safety": SafetyPolicy(risk_level=RiskLevel.SAFE, requires_confirmation=False,
                                notes="Reversible: phone number can be updated again if wrong."),
    },

    "open_sub_account": {
        "description": "Open a new sub-account for a member with a given type and initial deposit.",
        "input_params": [
            InputParam(name="member_id", type=ParamType.STRING, required=True,
                       description="The member ID to open the sub-account for."),
            InputParam(name="account_type", type=ParamType.STRING, required=True,
                       description="One of: savings, money_market, cd."),
            InputParam(name="initial_deposit", type=ParamType.NUMBER, required=True,
                       description="Initial deposit amount in USD. Deposits >= $10,000 require supervisor approval and will not complete automatically."),
        ],
        "checkpoint": Checkpoint(
            description="A confirmation number is shown on the confirmation page.",
            locator=Locator(strategy=LocatorStrategy.CSS, value="#confirmation-number"),
        ),
        "outputs": [
            OutputField(name="confirmation_number", type=ParamType.STRING,
                        description="The confirmation number for the newly opened sub-account.",
                        extract_from=Locator(strategy=LocatorStrategy.CSS, value="#confirmation-number")),
        ],
        "known_outcomes": [
            KnownOutcome(
                name="member_not_found",
                description="No member exists with the given ID.",
                condition=Locator(strategy=LocatorStrategy.TEXT, value="No member found for ID"),
                classification=OutcomeClassification.BUSINESS_OUTCOME,
            ),
            KnownOutcome(
                name="requires_supervisor_approval",
                description="Initial deposit is at or above the threshold requiring supervisor approval; not completed automatically.",
                condition=Locator(strategy=LocatorStrategy.CSS, value="#approval-required"),
                classification=OutcomeClassification.BUSINESS_OUTCOME,
            ),
            KnownOutcome(
                name="invalid_deposit_amount",
                description="The submitted initial deposit failed validation (e.g. zero or negative).",
                condition=Locator(strategy=LocatorStrategy.CSS, value="#form-error"),
                classification=OutcomeClassification.BUSINESS_OUTCOME,
            ),
            KnownOutcome(
                name="confirmation_dialog",
                description="A native browser confirmation dialog appears before submission and must be accepted to proceed.",
                condition=Locator(strategy=LocatorStrategy.TEXT, value="Confirm: open a new sub-account"),
                classification=OutcomeClassification.RECOVERABLE,
                recovery=RecoveryAction(action="dismiss_dialog", max_attempts=1),
            ),
        ],
        # This is the risky/irreversible capability: opening an account is a
        # consequential action, so replay requires an explicit confirmation
        # flag from the caller before it will proceed past the point of
        # submission. See replay/guardrails.py.
        "safety": SafetyPolicy(risk_level=RiskLevel.RISKY, requires_confirmation=True,
                                notes="Irreversible account-opening action; large deposits are blocked and routed to supervisor approval rather than silently succeeding."),
    },
}