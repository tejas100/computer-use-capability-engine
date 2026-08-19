"""
One-off script to generate a clean, real human-handoff evidence
example for /evidence/replay_human_handoff/.

Builds a temporary artifact with a deliberately unresolvable checkpoint
(so replay is guaranteed to hit a genuine HARD_FAILURE and escalate),
runs it, and copies the resulting evidence into place.

This mirrors exactly what a real hard failure looks like -- the
checkpoint locator strategy, fallback chain, and escalation/handoff
code paths are all the real ones, only the specific locator value is
deliberately wrong so the demo is reproducible on demand rather than
waiting for an organic failure.

Usage:
    python3 make_handoff_evidence.py
Then, in ANOTHER terminal, once it prints the resume instructions:
    touch evidence/<the printed run id>/resume.signal
"""

import json
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
ARTIFACTS_DIR = REPO_ROOT / "artifacts"
TARGET_DIR = REPO_ROOT / "evidence" / "replay_human_handoff"

TEST_CAPABILITY = "_handoff_demo"


def build_test_artifact():
    real = json.loads((ARTIFACTS_DIR / "get_member_balance.json").read_text())
    real["capability"] = TEST_CAPABILITY
    # Break the checkpoint on purpose -- guarantees a real HARD_FAILURE
    # and a real escalation, using the actual checkpoint-resolution and
    # handoff code paths (not a separate mocked path).
    real["checkpoint"]["locator"]["value"] = "#this-does-not-exist-on-the-page"
    (ARTIFACTS_DIR / f"{TEST_CAPABILITY}.json").write_text(json.dumps(real, indent=2))


def cleanup_test_artifact():
    path = ARTIFACTS_DIR / f"{TEST_CAPABILITY}.json"
    if path.exists():
        path.unlink()


def collect_evidence():
    """Find the most recent evidence dir for the demo capability, copy
    its contents into evidence/replay_human_handoff/, and remove the
    temporary test artifact + its raw evidence dir so the repo doesn't
    carry throwaway test scaffolding."""
    candidates = sorted(
        (REPO_ROOT / "evidence").glob(f"replay_{TEST_CAPABILITY}_*"),
        key=lambda p: p.stat().st_mtime,
    )
    if not candidates:
        print("No evidence dir found -- did you run replay.engine for the demo capability first?")
        return

    source_dir = candidates[-1]
    for item in source_dir.iterdir():
        shutil.copy2(item, TARGET_DIR / item.name)
    print(f"Copied {len(list(source_dir.iterdir()))} files from {source_dir} to {TARGET_DIR}")

    shutil.rmtree(source_dir)
    cleanup_test_artifact()
    print("Cleaned up temporary test artifact and raw evidence dir.")


if __name__ == "__main__":
    import sys

    if "--collect" in sys.argv:
        collect_evidence()
    else:
        TARGET_DIR.mkdir(parents=True, exist_ok=True)
        build_test_artifact()
        print(f"Test artifact written: artifacts/{TEST_CAPABILITY}.json")
        print(f"Run this now:")
        print(f'  python3 -m replay.engine --capability {TEST_CAPABILITY} --params \'{{"member_id": "12345"}}\'')
        print(f"\nWhen it prints resume instructions, create the resume.signal file it names.")
        print(f"\nOnce replay finishes, run this script again with --collect to copy evidence and clean up:")
        print(f"  python3 make_handoff_evidence.py --collect")