# CLAUDE.md

Guidance for Claude Code sessions working in this repository.

## What this is

A Home Assistant custom integration (HACS) that signs in to the CitiPower &
Powercor **myEnergy** portal (`https://myenergy.powercor.com.au`), downloads
smart-meter interval data (NEM12) and writes it into Home Assistant's
long-term statistics for the Energy dashboard. The design copies
[gheydon/ha-yvw](https://github.com/gheydon/ha-yvw), which does the same for
Yarra Valley Water on the same Salesforce Experience Cloud platform.

**Current state:** the build is complete and has been installed in a live Home
Assistant (v0.1.2): sign-in with MFA works and data is imported. Still to verify
are the statistics against the portal, the Energy dashboard, session lifetime
and the edge cases in [`PLAN.md`](PLAN.md); follow it.

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
  tls.py            HTTP sessions with the portal's missing TLS intermediate (certs/)
  coordinator.py    DataUpdateCoordinator + keep-alive loop + reauth trigger
  config_flow.py    user → method (email/SMS) → code → nmi; reauth; options
  sensor.py         latest data, latest-day kWh per channel, session status; with a tariff,
                    live period / rate / next-change sensors on one boundary timer (TariffTimeline)
  button.py         Refresh data button: an immediate poll, always available, raises on failure
  diagnostics.py    redacted diagnostics
  tariff.py         PURE: tariff model + per-day pricing on a selectable clock, per-period breakdown with supply-charge modes, slugs
  tariff_form.py    PURE: helpers for the tariff options flow (form <-> dict, review text)
  tariff_flow.py    HA: the tariff steps of the options flow (menu-driven)
  chain_guard.py    HA: per-NMI write lock, last-20-writes trail, broken-chain repair notice
  reprice.py        HA: reprice an entry's history (options flow and the Repairs fix share it)
  repairs.py        HA: the Repairs fix flow for a cost statistic that does not join
  (cost statistics and the opt-in per-period statistics live in statistics.py)
tests/
  fixtures/         anonymised real portal responses (see docs/TESTING.md)
  test_portal.py    pure
  test_nem12.py     pure
  test_tariff*.py   pure (tariff pricing, per-period breakdown, form helpers)
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
  `const.MARKET_TZ`, never `Australia/Melbourne`, for interval timestamps. The *tariff's*
  pricing clock is a separate, user-selectable setting (`tariff.py`: whole-hour Australian
  zones plus GMT; a version with no zone means fixed AEST), so a tariff day can be 23 or 25
  hours. Never use it to read meter data, and never assume a tariff day has 24 hours.
- **The statistics running sum must never double-count.** Any change to
  `statistics.py` needs a test in `test_ha_statistics.py`.
- **Statistics writes take the NMI's lock and wait for the recorder.** Every public write
  in `statistics.py` runs under `chain_guard.guard_for(hass, nmi).lock`; never call
  `async_add_external_statistics` for these statistics from elsewhere, and never read a
  starting total without `_async_settled`. `_async_write_series` checks that the stored
  window joins the row before it (running total = previous total + own state); keep that.
  Logs and the write trail name a statistic by its short label, never the NMI.
- **All portal traffic goes through `tls.async_create_session`.** The portal omits its
  TLS intermediate certificate, so HA's own client session fails verification. Never
  disable verification to work around it.
- Mirror the browser. When the portal does something odd (literal `"null"`
  CSRF token on the login page, `.VPN` username suffix, `jsfcljs` form
  fields), copy it exactly and leave a comment saying why.
- Code style: ruff, line length 100, Python 3.13, `from __future__ import
  annotations`. Comments explain *why*, not *what*.
- Commits: small and descriptive. Don't push or publish without the user asking.

## Things that are assumptions, not facts

These are the most likely causes of a first-run failure. Each one has a
`PLAN.md` item:

1. ~~The login page HTML was not captured.~~ **Confirmed working** (live sign-in
   and a scripted login attempt): the page carries an Aura descriptor and no
   `eikoocnekot`, so `aura.token` is the literal `"null"`.
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
