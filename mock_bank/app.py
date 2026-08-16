"""
Mock credit-union back office — the deliberately messy, no-API surface
the agent automates against.

Design choices worth noting for /REPORT.md:
  - Server-rendered, full page reloads only. No fetch/XHR, no SPA
    niceties -- this is the "legacy web app" case from the brief, not
    a modern one.
  - No test IDs anywhere. Locators in our artifacts have to survive
    on class names, table structure, or accessibility roles instead.
  - Deliberately inconsistent markup between pages (search results use
    a plain table, detail page nests a second table inside a panel)
    so a single locator strategy can't be assumed to work everywhere.
  - A few realistic annoyances are injected on purpose: an artificial
    delay on account creation (forces the replay engine to actually
    wait/retry rather than assume instant load), and a large-deposit
    threshold that triggers a "requires supervisor approval" business
    outcome instead of silently succeeding.
"""

import random
import string
import time
from flask import Flask, request, render_template, redirect, url_for

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Fake in-memory "core banking" data. Never real PII -- all synthetic.
# ---------------------------------------------------------------------------

MEMBERS = {
    "12345": {"name": "Alice Whitfield", "savings_balance": 4210.55, "phone": "555-010-1111", "status": "active"},
    "67890": {"name": "Marcus Doyle", "savings_balance": 812.00, "phone": "555-010-2222", "status": "active"},
    "24680": {"name": "Priya Nandakumar", "savings_balance": 15320.10, "phone": "555-010-3333", "status": "active"},
    "13579": {"name": "Owen Castellano", "savings_balance": 0.00, "phone": "555-010-4444", "status": "frozen"},
}

# Deposits at or above this trigger the "requires supervisor approval"
# business outcome on the open_sub_account flow.
LARGE_DEPOSIT_THRESHOLD = 10000


def gen_confirmation_number() -> str:
    return "CU-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=8))


def gen_session_id() -> str:
    return "".join(random.choices(string.hexdigits.lower(), k=12))


@app.context_processor
def inject_session_chrome():
    # Fake "internal tool" chrome shared by every page -- session id,
    # build tag -- rendered fresh per-request, nothing persisted.
    return {"session_id": gen_session_id(), "build_tag": "CU-OPS v3.4.1"}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    return redirect(url_for("search"))


@app.route("/search", methods=["GET", "POST"])
def search():
    results = None
    query = ""
    if request.method == "POST":
        query = request.form.get("member_id", "").strip()
        if query in MEMBERS:
            results = [{"member_id": query, **MEMBERS[query]}]
        else:
            results = []  # deliberately empty, not an error -- rendered as "no matches"
    return render_template("search.html", results=results, query=query)


@app.route("/member/<member_id>")
def member_detail(member_id):
    member = MEMBERS.get(member_id)
    return render_template("detail.html", member_id=member_id, member=member)


@app.route("/member/<member_id>/update-phone", methods=["GET", "POST"])
def update_phone(member_id):
    member = MEMBERS.get(member_id)
    if member is None:
        return render_template("detail.html", member_id=member_id, member=None)

    error = None
    success = False
    if request.method == "POST":
        new_phone = request.form.get("new_phone", "").strip()
        # Deliberately picky validation so replay has a real validation-error
        # business outcome to detect, not just happy path.
        if not new_phone or len(new_phone.replace("-", "")) < 10:
            error = "Invalid phone number format."
        else:
            member["phone"] = new_phone
            success = True

    return render_template("update_phone.html", member_id=member_id, member=member, error=error, success=success)


@app.route("/member/<member_id>/open-account", methods=["GET", "POST"])
def open_account(member_id):
    member = MEMBERS.get(member_id)
    if member is None:
        return render_template("detail.html", member_id=member_id, member=None)

    if request.method == "POST":
        account_type = request.form.get("account_type", "")
        try:
            initial_deposit = float(request.form.get("initial_deposit", "0"))
        except ValueError:
            initial_deposit = -1  # forces validation_error below

        if initial_deposit <= 0:
            return render_template(
                "open_account.html", member_id=member_id, member=member,
                error="Initial deposit must be a positive amount."
            )

        # Simulate a slow legacy backend call -- replay must wait, not
        # assume instant load.
        time.sleep(1.5)

        if initial_deposit >= LARGE_DEPOSIT_THRESHOLD:
            return render_template(
                "open_account.html", member_id=member_id, member=member,
                requires_approval=True, account_type=account_type,
                initial_deposit=initial_deposit,
            )

        confirmation = gen_confirmation_number()
        return render_template(
            "confirmation.html", member_id=member_id, member=member,
            account_type=account_type, initial_deposit=initial_deposit,
            confirmation_number=confirmation,
        )

    return render_template("open_account.html", member_id=member_id, member=member)


if __name__ == "__main__":
    app.run(debug=True, port=5000)