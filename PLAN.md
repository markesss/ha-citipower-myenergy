# Plan: CitiPower & Powercor myEnergy integration

Status key: `[x]` done · `[ ]` to do · `[~]` partly done / see notes.
Record findings under each item as you go. The next session picks up from the
first unchecked box.

---

## Phase 0: Discovery ✅

- [x] Study gheydon/ha-yvw (MIT): config flow, Aura client, keep-alive,
      statistics repair, session persistence.
- [x] Confirm myEnergy is Salesforce Experience Cloud (Lightning/Aura) at
      `myenergy.powercor.com.au/s/`, not the old Visualforce `customermeterdata` site.
- [x] Confirm MFA is required at every sign-in, offered by email or SMS, with no
      "remember this device".
- [x] HAR 1 (signed-in usage page): data calls identified:
      `EnergyUsageDetailPageController.getEnergyUsageDetailWrapper`,
      `EnergyInformationController.retrieveAccountHolderInfo`,
      `EnergyInformationController.getMeterData` (format "Customer Access NEM12").
- [x] HAR 2 (full sign-in): `LightningLoginFormController.communityLogin` →
      `frontdoor.jsp` → `/apex/MFA_Enabler` → remoting `initiateMFA` /
      `verifyMFA` → form post `/MFA_Enabler` → `/s/`.
- [x] Confirm reCAPTCHA is loaded but **not** part of the login request.
- [x] Session timeout ≈ 2 h (`SessionTimeServlet` → `sr≈7170`).
- [x] Data: 5-minute intervals, `E1` only on the test account, ~1 day lag
      (each day's row is updated ≈01:00 AEST the next morning), history from 2025-06-18.

## Phase 1: Build ✅

- [x] `const.py`, `exceptions.py`
- [x] `portal.py`: Aura parsing, client redirects, MFA page and remoting parsing
- [x] `nem12.py`: 200/300/400 records, unit conversion, hourly roll-up in AEST
- [x] `models.py`: NMI list, account info, meter data parsers, date chunking
- [x] `aura.py`: page context loading, Aura POST, client with stale-context retry
- [x] `auth.py`: password → frontdoor → MFA page → send code → verify → final form post
- [x] `api.py`: chunked meter data (≤90 days per call, sequential `jobId`)
- [x] `statistics.py`: external statistics per channel, repair-from-first-disagreement
- [x] `session_store.py`: cookies + Aura context persisted, tied to the `signed_in_at` of the sign-in
- [x] `coordinator.py`: 4-hourly poll, first-run full backfill, 7-day refresh
      window, keep-alive (default 45 min) and reauth on expiry
- [x] `config_flow.py`: user → method → code (with resend) → nmi; reauth; options
- [x] `sensor.py`, `diagnostics.py`, `manifest.json`, `strings.json`, `translations/en.json`
- [x] `hacs.json`, `LICENSE` (MIT, credits ha-yvw), CI workflow (hassfest, HACS, ruff, pytest)
- [x] Anonymised fixtures extracted from both HARs; personal data scrubbed and scanned
- [x] Pure tests: 29 passing (`test_portal.py`, `test_nem12.py`)
- [x] NEM12 roll-up cross-checked against the portal's own "MDPP Interval
      Summary": all 17 months matched to the Wh
- [x] `ruff check` / `ruff format --check` clean
- [x] HA tests written (`test_ha_config_flow.py`, `test_ha_statistics.py`), **not yet run**
- [x] README, CLAUDE.md, docs/

**Build complete.** Phases 2–3 were started on 2 Nov 2026; see the notes under each item.

---

## Phase 2: Automated tests with Home Assistant

- [x] Create a Python 3.13 venv; `pip install -r requirements-dev.txt`.
      (Done in CI. Not run locally: the development machine has Python 3.11 and no HA.)
- [x] `pytest -q`: get all tests passing, including `test_ha_*`.
      CI (Gitea) ran 42 tests: the 41 existing ones passed first time. The one failure was
      a bug in the new `test_ha_tls.py` (a session's `.connector` is `None` after
      `close()`), fixed in `c42d4ec`.
      Likely trouble spots: `MockConfigEntry.start_reauth_flow`, `_get_reauth_entry`,
      `_abort_if_unique_id_mismatch` (need HA ≥ 2024.11, so these should exist),
      `AddConfigEntryEntitiesCallback` (HA ≥ 2025.3), and `StatisticMetaData` fields
      (`mean_type`, `unit_class`) on the pinned HA version.
- [ ] Add a coordinator test with a mocked `MyEnergyApi`:
      first run backfills from `account.start`; second run starts 7 days before
      the newest stored hour; `MyEnergyAuthError` → `ConfigEntryAuthFailed`.
- [ ] Add a keep-alive test: ping skipped when a poll was recent; an auth error
      marks the session dead, starts reauth and stops the loop; other errors retry
      after `KEEPALIVE_RETRY`.
- [ ] Add a setup test: an entry loads, sensors appear, and the session store
      round-trips cookies/context and ignores records from a different `signed_in_at`.
- [ ] Add an `auth.py` test against a fake portal (`aiohttp.web` test server, or
      patch `const` URLs) that replays the fixtures through the full sign-in.
- [x] Run hassfest and HACS validation locally or via CI and fix anything they report
      (manifest key order, strings, missing brand assets: `ignore: brands` is set).
      hassfest passes on both Gitea and GitHub. `hacs/action` cannot run on Gitea (it calls
      the GitHub API with the runner token: "Bad credentials"), so the job is skipped
      unless `github.server_url` is github.com. On GitHub it needed a repo description,
      topics, and a `LICENSE` GitHub can identify as MIT (a free-text "Portions adapted"
      line broke detection; the ha-yvw copyright is now its own line). Green after that.

## Phase 3: Live test in Home Assistant

Use a dev instance where possible (e.g. Docker `homeassistant/home-assistant:2025.12`).
Turn on debug logging first (see docs/TESTING.md). Note the result of each step here.

- [x] Install: copy `custom_components/citipower_myenergy` into `config/custom_components/`, restart.
      Installed through HACS as a custom repository. HACS needs a **GitHub Release**
      (not just a commit): with none, it tried `archive/refs/heads/<sha>.zip` and got a 404.
- [x] Add integration → email/password accepted → **method** step shows
      "SMS to 04** *** nnn" and "Email to …".
      The login page assumption (CLAUDE.md #1) held: a scripted run of
      `async_submit_credentials` from a PC reached the portal's rejection of a bogus login.
      **Finding: the portal serves an incomplete TLS chain.** It sends only its leaf
      certificate, not the DigiCert intermediate ("DigiCert Global G2 TLS RSA SHA256 2020
      CA1"). Browsers and Windows fetch the intermediate on demand; Python/OpenSSL on Linux
      does not, so HA reported "Could not reach myEnergy" with
      `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`. Fixed in v0.1.2
      by shipping the public intermediate (`certs/`) and building the sessions' trust
      store from certifi plus it (`tls.py`); verification stays on. The intermediate expires
      2031-03-29. The cause was invisible because the config flow swallowed the
      exception text; `cannot_connect` is now logged.
- [x] Choose SMS → code arrives → **code** step accepts it → entry created.
      Reported working. Which method (SMS or email) was used was not recorded.
- [ ] Repeat with **email** as the method.
- [ ] Deliberately enter a **wrong password**. Capture the debug-logged
      response shape and make `portal.login_error` read it. Add a fixture and test.
- [ ] Deliberately enter a **wrong code**. Confirm the portal's message is shown and a
      second attempt (or resend) works. Add a fixture and test.
- [~] First refresh: backfill completes (≈7 requests). Note how long it takes.
      Reported: data pulled in. Duration and statistic ids not recorded yet.
      Developer tools → Statistics shows `citipower_myenergy:<nmi>_e1` from
      the account's start date.
- [ ] Compare a few days' totals in HA with the myEnergy usage page.
- [ ] Energy dashboard: add the statistic as grid consumption. Check that hours
      line up (a summer evening peak shows at the right local time).
- [ ] Sensors: Latest data, Latest day grid import, Session = active.

## Phase 4: Session lifetime

- [ ] Leave it running ≥ 6 hours. Session sensor stays `active`, and
      `seconds_remaining` in its attributes recovers after each keep-alive.
      Note the values seen.
      *If the session dies:* test whether `retrieveAccountHolderInfo` really
      extends it (CLAUDE.md #4). Try loading `USAGE_PAGE` as the ping instead,
      or lower the interval.
- [ ] Restart HA mid-session: it resumes without asking for a code (the session
      store is used, and no page load is needed).
- [ ] Stop HA for > 2 h, then start it: a reauth prompt appears; signing in again
      resumes and fills the gap in statistics without duplicates.
- [ ] Options: change the keep-alive interval; the entry reloads and uses it.
- [ ] Sign out of myEnergy in a browser: confirm whether that kills the HA
      session (expected: it does, then reauth is prompted). Document the result in README.

## Phase 5: Data correctness edge cases

- [ ] Day-after behaviour: poll before and after ~01:00 AEST; the new day appears
      and the next poll changes nothing (`hours_added_last_fetch` = 0).
- [ ] Repair path: if an estimated (`E`/`S`) day is ever seen being replaced, confirm
      the statistics were rewritten from that hour (log line "Rewriting …").
- [ ] Solar/export account (if one is available): `B1` appears as a separate
      statistic and sensor.
- [ ] Multiple NMIs on one login (if available): the nmi step appears, and two
      entries can coexist.

## Phase 6: Publish

- [~] Create the GitHub repo; replace `OWNER` in `manifest.json`
      (`documentation`, `issue_tracker`) and add `codeowners`.
      Repo `markesss/ha-citipower-myenergy` created (a push mirror of the Gitea repo),
      `OWNER` replaced. `codeowners` is still empty.
- [x] CI green (hassfest, HACS, ruff, pytest).
- [ ] Update README with live-test results and anything learnt in Phases 3–5.
- [x] Bump `version` in `manifest.json`; tag a `v0.1.0` release.
      Now at v0.1.2 (v0.1.1 added the connection-error logging, v0.1.2 the TLS fix). Each
      tag also needs a GitHub Release for HACS.
- [ ] Optional: post on the HA community forum (thread "Reading Australian
      smart meter data" has people asking for exactly this).

## Backlog / ideas

- [ ] United Energy support: same portal, different username suffix (brand `UE`).
- [ ] First-time MFA enrolment (`verifyMFARegister`, `isRegistration: true`).
- [ ] Run the first backfill as a background task so setup doesn't wait ~30 s.
- [ ] Brand assets (icon/logo) for the HACS default store.
