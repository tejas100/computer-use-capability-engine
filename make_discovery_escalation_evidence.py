"""
One-off script to reliably reproduce a discovery-side stall ->
auto-escalation for clean evidence, rather than hoping one occurs
naturally.

Gives the agent an impossible goal against the real mock_bank app: a
member ID that doesn't exist ANYWHERE, combined with an instruction
that assumes success, which reliably makes the model try, fail,
retry, and eventually get caught by the consecutive-failure stall
detector in agent/discover.py -- the exact same detector and escalation
path a genuine stuck run would hit, just triggered deliberately and
reproducibly instead of waiting for one.

Usage:
    python3 make_discovery_escalation_evidence.py
Then move the resulting evidence/get_member_balance_<timestamp>/
folder into evidence/discovery_escalation/ (this script tells you
which one at the end).
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
EVIDENCE_DIR = REPO_ROOT / "evidence"

# A goal designed to genuinely confuse the model rather than cleanly
# resolve as a business outcome: it references a UI element ("the
# Export button") that does not exist anywhere on this app, so there
# is no clean "not found" page to land on -- the model has to keep
# looking for something that was never there, which is what actually
# exercises the consecutive-failure stall detector rather than the
# clean "no such member" business-outcome path.
IMPOSSIBLE_GOAL = "Look up member 12345, then click the Export button to download their statement as a PDF"


def main():
    before = set(EVIDENCE_DIR.glob("get_member_balance_*"))

    print("Running discovery with a goal that cannot succeed (member 00000 does not exist)...")
    print("This is expected to end in either a business-outcome 'done' or a stall escalation.")
    subprocess.run([
        sys.executable, "-m", "agent.discover",
        "--goal", IMPOSSIBLE_GOAL,
        "--capability", "get_member_balance",
    ])

    after = set(EVIDENCE_DIR.glob("get_member_balance_*"))
    new_dirs = after - before
    if new_dirs:
        new_dir = new_dirs.pop()
        has_intervention = (new_dir / "intervention_request.json").exists()
        print(f"\nNew evidence dir: {new_dir}")
        print(f"Contains intervention_request.json: {has_intervention}")
        if has_intervention:
            print(f"\nThis is a real escalation example. Move it into place:")
            print(f"  mv {new_dir} evidence/discovery_escalation")
        else:
            print(f"\nThe model resolved this as a business outcome ('member not found') "
                  f"rather than stalling -- that's also valid evidence, just not an "
                  f"escalation example. Re-run this script if you specifically need a stall.")
    else:
        print("No new evidence directory found -- check for errors above.")


if __name__ == "__main__":
    main()