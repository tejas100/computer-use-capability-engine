# computer-use-capability-engine

An LLM-driven computer-use system: an agent discovers how to accomplish a
goal inside a real (mock) legacy web UI, records the successful run as a
typed, reusable **capability artifact**, and that artifact then replays
**deterministically** — no LLM in the loop — for future invocations.

Built for interface.ai's Computer-Use Automation System take-home assignment.

## Architecture at a glance

```
goal (natural language)
  -> agent/discover.py       LLM (GPT-4o) + screenshots + Playwright drive
                              a real browser, observe -> decide -> act
  -> artifacts/<name>.json    saved capability: steps, locators, params,
                              outputs, checkpoint, known outcomes, safety
  -> replay/engine.py         re-runs the artifact deterministically,
                              no LLM, with locator fallback chains and
                              a 3-way result contract
  -> guardrails/              allowlist + risky-action confirmation +
                              redaction, enforced on every replay
  -> human_handoff/           on a hard failure, pause and hand the SAME
                              live browser session to a human via CDP
```

See `/REPORT.md` for the full design write-up and reasoning.

## Setup

Requires Python 3.11+ and Google Chrome installed locally.

```bash
git clone https://github.com/tejas100/computer-use-capability-engine
cd computer-use-capability-engine

python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

pip install -r requirements.txt
playwright install chrome
```

### API key

Discovery uses OpenAI's GPT-4o for vision-driven decisions. Set:

```bash
export OPENAI_API_KEY=your_key_here
```

**Replay does not need an API key at all** — it's the whole point: replay
never calls an LLM. You can run every replay command below with no
`OPENAI_API_KEY` set.

## The mock target application

Since a real bank system isn't available (and shouldn't be used per the
assignment), the target is a self-built Flask app simulating a credit
union back-office tool: server-rendered, table-based layout, no test IDs,
inconsistent markup between pages, an injected slow endpoint, and a native
browser confirmation dialog on the risky action — see `/REPORT.md` section
4 for why it's built this way.

Run it in its own terminal, and leave it running for everything below:

```bash
cd mock_bank
python3 app.py
```

It serves at `http://127.0.0.1:5000`. Sample member IDs: `12345`, `67890`,
`24680`, `13579` (frozen status). Any other ID is a legitimate "not found."

## Demo path

Three capabilities are already discovered and saved in `/artifacts/`.
Both the discovery and replay commands below work for any of them.

### 1. Discover a capability (LLM-driven, real API calls)

```bash
python3 -m agent.discover \
  --goal "Look up member 12345 and read their savings balance" \
  --capability get_member_balance
```

A visible Chromium window opens; you'll see it screenshot, decide, and
act step by step. On success, the artifact is saved to
`artifacts/get_member_balance.json` and a full step-by-step log +
screenshots land in `evidence/<run_id>/`.

The other two capabilities, if you want to re-discover them:

```bash
python3 -m agent.discover \
  --goal 'Update the phone number for member 12345 to 555-123-9876' \
  --capability update_member_phone

python3 -m agent.discover \
  --goal 'Open a new savings sub-account for member 12345 with an initial deposit of $500' \
  --capability open_sub_account
```

**Note the single-quoted `$500`** — bash/zsh will otherwise interpret `$5`
as a variable and mangle the goal string.

### 2. Replay a capability deterministically (no LLM, no API key needed)

```bash
python3 -m replay.engine \
  --capability get_member_balance \
  --params '{"member_id": "67890"}'
```

Prints a structured JSON result: `status` is one of `success`,
`business_outcome`, or `hard_failure`. Try a nonexistent member to see
the business-outcome path instead of a crash:

```bash
python3 -m replay.engine \
  --capability get_member_balance \
  --params '{"member_id": "99999"}'
```

Add `--headed` to any replay command to watch the browser drive itself.

### 3. The risky action requires explicit confirmation

`open_sub_account` is marked `risk_level: risky` in its artifact. Replay
refuses to run it without an explicit flag:

```bash
# Blocked -- no browser even opens
python3 -m replay.engine \
  --capability open_sub_account \
  --params '{"member_id": "12345", "account_type": "savings", "initial_deposit": 250}'

# Proceeds
python3 -m replay.engine \
  --capability open_sub_account \
  --params '{"member_id": "12345", "account_type": "savings", "initial_deposit": 250}' \
  --confirm-risky-action
```

Try `"initial_deposit": 15000` (with `--confirm-risky-action`) to see the
`requires_supervisor_approval` business outcome instead of a silent success.

### 4. Human-in-the-loop escalation

If replay hits a hard failure (or you want to see it deliberately), it
pauses, writes `evidence/<run>/intervention_request.json`, and prints
instructions for connecting to the **same live browser session** via
Chrome's `chrome://inspect` (the browser is launched with
`--remote-debugging-port=9222`). Resume by creating the signal file it
prints, e.g.:

```bash
touch evidence/<run_id>/resume.signal
```

Pass `--no-human-escalation` to fail immediately instead (useful for
unattended/CI runs).

## Running without live services

The mock_bank Flask app is the only "live service" this project depends
on — there's no external API besides OpenAI (discovery only). Replay
against an existing artifact needs no external calls at all beyond the
mock_bank app itself.

## Project layout

```
mock_bank/       the mock legacy target application (Flask)
schemas/         the artifact schema (Pydantic models)
agent/           discovery: the LLM-driven observe/decide/act loop
replay/          the deterministic replay engine
guardrails/      allowlist, risky-action confirmation, redaction
human_handoff/   CDP-based human takeover of a live session
artifacts/       saved capability artifacts (JSON)
evidence/        per-run logs, screenshots, intervention/handoff records
REPORT.md        design write-up
```