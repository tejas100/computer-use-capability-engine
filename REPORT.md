# Design Report — Computer-Use Capability Engine

## 1. Architecture

```
goal (NL) -> discovery (agent/) -> artifact (artifacts/*.json) -> replay (replay/) -> result
                                          ^
                              guardrails/ + human_handoff/ wrap replay
```

**Discovery** (`agent/discover.py`) runs observe -> decide -> act: a
screenshot goes to GPT-4o via forced tool-calling (never free-text
parsing), Playwright executes the returned action, and the loop repeats
until `done`, `stuck`, or an automatic stall-detector fires. The decision
is vision-only (the model never sees raw HTML), but execution is
DOM-grounded: after a click, `locator_capture.py` asks Playwright what
element is actually at that pixel, and *that* — not the model's guess —
becomes the artifact's locator. Vision decides; the DOM records. This
lets discovery work with no clean DOM while still producing stable,
inspectable replay locators instead of brittle coordinates.

**Replay** (`replay/engine.py`) is the production path: it loads an
artifact and walks its `steps` with no LLM call anywhere, returning one
of three outcomes (section 3). It is a fully separate code path from
discovery, so replay's cost and correctness don't depend on the model
that discovered the flow.

**Trade-offs:** single process, no queue (the brief discourages premature
scaling infra); LLM provider is OpenAI/GPT-4o (swapped from Anthropic
mid-build for available credits — isolated to `agent/llm_client.py`);
capabilities are invoked by exact name, not matched from free text —
building an NL router is a separate, harder problem, deliberately not
built (section 7).

## 2. Artifact schema

```
Artifact: capability, version, description, target.base_url,
          input_params[], steps[], checkpoint, outputs[],
          known_outcomes[], safety, metadata
```

**Steps are a flat, ordered list — no loops or branches.** Back-office
wizards are linear; control flow would be complexity with no observed
payoff at this scope.

**Locators carry a fallback chain** with four strategies, added as real
failures demanded them: `css` (primary), `role` (accessible name,
survives markup churn), `text`, and `row_contains` — added after a real
bug where a repeating search-result row's link was recorded positionally
(`tr:nth-of-type(2)`, correct only by coincidence for a single result)
and, when I tried fixing it with static text instead, that broke too the
moment more than one row shares the same link text. `row_contains` scopes
the target to "the row whose own text contains a value the caller
supplied," identifying *which record*, not a position or a label. This
is the single most important robustness fix in the project.

**Parameterization is explicit.** When discovery types a value into a
field matching a declared `input_param`, that literal is rewritten to
`{{param_name}}` everywhere it reappears (a fill value, a `row_contains`
match) once the run succeeds. Verified to generalize: the same
`get_member_balance` artifact returns correct, different balances for
member IDs never seen during discovery.

## 3. Determinism & error handling

Replay only *executes* — every decision was already made at discovery
time. Locator resolution tries each fallback strategy in order; no step
guesses a target at replay time.

**Three-way result contract** (`replay/result.py`):
- `SUCCESS` — checkpoint resolved, outputs populated.
- `BUSINESS_OUTCOME` — a `known_outcome` fired (e.g. "no such member,"
  "requires supervisor approval"). Checked after *every* step, not just
  on failure, since an outcome can appear without any exception at all.
- `HARD_FAILURE` — a locator's whole fallback chain was exhausted, or the
  checkpoint never resolved, with nothing to explain it. Carries the
  failed step, what was expected, what was observed.

A `RECOVERABLE` class handles the confirmation dialog on
`open_sub_account` inline, via an unconditional `accept()` on Playwright's
`dialog` event. This was a real bug: an earlier version deferred handling
the dialog to the *next* loop iteration, which hung indefinitely — a
Playwright `click()` that triggers a dialog blocks synchronously until
it's resolved, so nothing (not even a screenshot) can run in between. Found
by testing the failure directly, not by assumption.

**Verified against real outcomes, not described hypothetically:**
validation errors, a "not found" result distinguished from a crash, a
large-deposit business rule that blocks rather than silently succeeds,
and the native dialog.

## 4. Heterogeneity & multi-tenant

The target (`mock_bank/`) is deliberately legacy-flavored: server-rendered,
table-based, no test IDs, inconsistent DOM between pages, an injected
slow endpoint, a native dialog — chosen specifically to exercise the "no
clean DOM" case, and it did: `row_contains` and the coordinate-grounding
fixes below both exist because this surface was genuinely hostile, not
hypothetically so.

**Surface abstraction:** the seam is the locator's `strategy` field. A
legacy frameset app would add a `frame_path` to `Locator`; a desktop app
would add an OS-accessibility-tree strategy and swap
`document.elementFromPoint` for the OS accessibility API in
`locator_capture.py`. The artifact's shape (steps, checkpoint, outputs,
known_outcomes) needs no change either way — none of it is web-specific
by construction.

**Multi-tenant reuse** is one level up from the per-record
parameterization already built: canonicalize tenant-specific literals the
same way `member_id` already is, and add a cheap compatibility probe
(resolve the checkpoint's fallback locator against a second tenant's page
before running the full sequence) to flag likely drift before it causes a
confusing mid-flow failure. Not built — see section 7.

## 5. Escalation & handoff

**Detection:** the model can self-report `stuck`; separately, an
automatic stall-detector force-escalates after 4 consecutive action
failures regardless of what the model reports — added after a real run
spent its whole step budget retrying a failing action without ever
recognizing it wasn't converging. Replay's `HARD_FAILURE` path triggers
the same mechanism.

**Intervention requests** (`agent/escalation.py`) use one shared payload
for both discovery and replay: capability/goal, current step, reason,
screenshot.

**Taking control of the live session** needed a real mechanism, not a
mock. Both browsers launch with `--remote-debugging-port=9222` (CDP), so
a human can open `chrome://inspect` and connect to the *exact running
tab* — same cookies, same DOM state — not a fresh one. Endpoint detection
makes a real HTTP call to Chrome's `/json/version`, not an assumption.

**Resume** is a deliberately minimal, real mechanism: replay polls for
`evidence/<run>/resume.signal`. On resume it **re-verifies** the
checkpoint/outcome rather than trusting the human — tested directly: a
simulated human created the signal without fixing anything, and replay
correctly re-checked and failed honestly.

**Control state** is tracked explicitly (`automation`/`human`) with both
timestamps and before/after screenshots in `handoff_record.json`.

No operator UI was built — the human's "surface" is Chrome's own
`chrome://inspect`, per the brief's explicit allowance for a bare
operator surface as long as the handoff mechanism itself is real.

## 6. Safety

**Allowlist** (`guardrails/allowlist.py`) is enforced before every
navigation and action type in replay — a static, file-based policy
nothing at runtime can widen. Refuses to run at all with no policy file.
Tested against an artifact pointing at an off-policy domain: blocked
before any browser opened, via a distinct `PolicyViolation` class that's
always a hard failure, checked before outcome-matching so a blocked
action can never be reclassified as ordinary.

**Risky actions:** `open_sub_account` is marked `risk_level: risky,
requires_confirmation: true`. Replay checks this *before opening a
browser* and refuses without an explicit `--confirm-risky-action` flag —
a non-interactive gate, since the brief frames replay as what an AI agent
invokes in production, and an agent can supply a flag but can't answer a
terminal prompt. Separately, the app itself blocks deposits >= $10,000
regardless of the flag, routing to supervisor approval — two independent
layers.

**Redaction** (`guardrails/redaction.py`) is pattern-based (passwords,
tokens, API keys, SSNs), applied at the final output boundary. The mock
domain has little to actually catch, but a real deployment would have
exactly these patterns flowing through the same path — the boundary
needs to exist regardless of today's demo data.

## 7. Cuts

- **Capability routing** — invoked by exact name; NL-to-capability
  matching is a separate, harder problem. The schema's typed params and
  descriptions are shaped to support this later as a tool manifest.
- **Parameterization beyond declared params** — a literal with no
  matching input_param stays a literal, a visible limitation rather than
  a silent guess.
- **Multi-tenant reuse and drift detection** — designed for (section 4),
  not built.
- **Legacy-frame and desktop surfaces** — schema has the extension seam;
  only standard web-DOM strategies are implemented.
- **Confidence scoring, approval gating, multi-run stability** — named
  stretch goals, not built, in favor of full outcome coverage on the
  three required capabilities.
- **No queue/multi-process architecture** — per the brief's explicit
  guidance against premature scaling infrastructure.

**Next with more time:** a capability router using function-calling
against the existing artifact schema as a tool manifest; a cheap
compatibility probe before cross-tenant replay; confidence scoring from
multi-run data, gating unattended replay below a threshold.