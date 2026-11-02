# CLAUDE.md

Guidance for Claude Code sessions working in this repository.

## What this is

A Home Assistant custom integration (HACS) that signs in to the CitiPower &
Powercor **myEnergy** portal (`https://myenergy.powercor.com.au`), downloads
smart-meter interval data (NEM12) and writes it into Home Assistant's
long-term statistics for the Energy dashboard. The design copies
[gheydon/ha-yvw](https://github.com/gheydon/ha-yvw), which does the same for
Yarra Valley Water on the same Salesforce Experience Cloud platform.

**Current state:** the build is complete. The code was written and the pure
parsers tested in a sandbox *without* Home Assistant or aiohttp installed. The
HA-level code (config flow, coordinator, statistics, sensors) and the live
sign-in have **never been run**. Finishing that work is the job; follow
[`PLAN.md`](PLAN.md).

## Start here

1. Read `PLAN.md`. Work through the unchecked items in order and tick them off
   as you go. Record findings under each item. Don't just tick the box.
2. Read `docs/ARCHITECTURE.md` for the module map and `docs/PORTAL_PROTOCOL.md`
   for exactly what the portal sends and expects (taken from real HAR captures).
3. `docs/TESTING.md` covers how to run the tests and the live test procedure.

## Commands

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt     # pins pytest-homeassistant-custom-component (HA 2025.12)
pytest -q                               # all tests; test_ha_* need the HA test package
ruff check custom_components tests
ruff format custom_components tests
```

Without Home Assistant installed, `tests/conftest.py` skips `test_ha_*` and
registers the package as a bare namespace, so the parser tests still run.

## Layout

```
custom_components/citipower_myenergy/
  const.py          URLs, Apex class/method names, tunables, config keys
  exceptions.py     MyEnergyError hierarchy (no HA imports)
  portal.py         PURE: Aura + Visualforce-remoting parsing, MFA page parsing
  nem12.py          PURE: NEM12 parser → hourly kWh per channel (AEST)
  models.py         PURE: NmiInfo/AccountInfo + response parsers, date chunking
  aura.py           aiohttp: page loads, Aura POSTs, MyEnergyAuraClient
  auth.py           aiohttp: MyEnergyLogin (password → MFA → session)
  api.py            MyEnergyApi: list NMIs, account info, meter data, session clock
  statistics.py     HA recorder: external statistics with repair-on-change
  session_store.py  HA Store: cookies + Aura context across restarts
  coordinator.py    DataUpdateCoordinator + keep-alive loop + reauth trigger
  config_flow.py    user → method (email/SMS) → code → nmi; reauth; options
  sensor.py         latest data, latest-day kWh per channel, session status
  diagnostics.py    redacted diagnostics
tests/
  fixtures/         anonymised real portal responses (see docs/TESTING.md)
  test_portal.py    pure
  test_nem12.py     pure
  test_ha_*.py      need pytest-homeassistant-custom-component
```

## Rules

- **Keep `portal.py`, `nem12.py`, `models.py`, `exceptions.py` free of Home
  Assistant and aiohttp imports.** They are the part that can be tested
  anywhere, and the parsing is where most portal changes will break things.
- **Never log or store secrets.** No passwords, verification codes, `sid`
  values, cookie values or CSRF tokens in logs, diagnostics, fixtures or
  commits. Log URLs only via `portal.describe_url()`, which masks query values.
  The password is used once in the config flow and never written anywhere.
- **Fixtures must be anonymised.** Real HARs contain the plaintext password,
  a live session id, the account holder's name, mobile number, address and NMI.
  Never commit a HAR. Never paste one into a file in this repo. See
  `docs/TESTING.md` → *Capturing new portal traffic*.
- **NEM12 times are market time (AEST, UTC+10, no DST).** Use
  `const.MARKET_TZ`, never `Australia/Melbourne`, for interval timestamps.
- **The statistics running sum must never double-count.** Any change to
  `statistics.py` needs a test in `test_ha_statistics.py`.
- Mirror the browser. When the portal does something odd (literal `"null"`
  CSRF token on the login page, `.VPN` username suffix, `jsfcljs` form
  fields), copy it exactly and leave a comment saying why.
- Code style: ruff, line length 100, Python 3.13, `from __future__ import
  annotations`. Comments explain *why*, not *what*.
- Commits: small and descriptive. Don't push or publish without the user asking.

## Things that are assumptions, not facts

These are the most likely causes of a first-run failure. Each one has a
`PLAN.md` item:

1. The login page (`/s/login/`) HTML was not captured. `auth.py` assumes it
   carries an Aura descriptor like the signed-in pages, with no `eikoocnekot`
   (so `aura.token` is the literal `"null"`, which is what the browser sent).
2. The response to a **wrong password** has not been seen. `portal.login_error`
   guesses at `errorMessage`/`message` keys.
3. The response to a **wrong code** has not been seen. The page script reads
   `result.message` when `result.success` is false.
4. The keep-alive assumes any authenticated Aura call
   (`retrieveAccountHolderInfo`) extends the 2-hour Salesforce session, and
   that `SessionTimeServlet`'s `sr` is "seconds remaining".
5. `isRegistration: true` (first-time MFA enrolment) is not handled. It raises
   an error asking the user to finish setup in a browser.
6. Accounts with solar should produce a `B1` channel. Only an `E1`-only account
   has been seen.
