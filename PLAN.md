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
- [x] Add a coordinator test with a mocked `MyEnergyApi`:
      first run backfills from `account.start`; second run starts 7 days before
      the newest stored hour; `MyEnergyAuthError` → `ConfigEntryAuthFailed`.
      `tests/test_ha_coordinator.py` (16 Nov 2026). Before it, the cost tests ran the real
      coordinator but never asserted the fetch start or end. Also guards the clamps to
      `account.start` and `MAX_HISTORY_DAYS`, the window counted in market time (not UTC), the
      oldest channel setting the window, and a channel with nothing stored forcing a backfill.
- [x] Add a keep-alive test: ping skipped when a poll was recent; an auth error
      marks the session dead, starts reauth and stops the loop; other errors retry
      after `KEEPALIVE_RETRY`.
      Also in `tests/test_ha_coordinator.py`, on frozen time with no real sleeps. It also covers
      the interval coming from the options, stop cancelling the timer, and a dead session not
      restarting the loop. Every earlier test had patched the keep-alive out.
- [x] Add a setup test: an entry loads, sensors appear, and the session store
      round-trips cookies/context and ignores records from a different `signed_in_at`.
      `tests/test_ha_session_store.py`. Covers the round trip across a simulated restart,
      records for another `signed_in_at` or entry being ignored, unusable records, a new
      sign-in keeping the channels, setup preferring stored cookies and falling back to the
      entry's, the first poll persisting them, and removal forgetting only its own record.
- [x] Add an `auth.py` test against a fake portal (`aiohttp.web` test server, or
      patch `const` URLs) that replays the fixtures through the full sign-in.
      `tests/test_ha_auth.py`: a local `aiohttp` test server replays the anonymised fixtures
      through the real `MyEnergyLogin` (password, then email or SMS code), refusing
      out-of-order requests as the real portal does. **Limits:** the login page was never
      captured (derived from `community_page.html`), and no real wrong-password or wrong-code
      response has been seen, so those two tests prove control flow, not the portal's wording
      (CLAUDE.md assumptions 2 and 3, still open under Phase 3). The "no verification step"
      and first-time MFA enrolment paths have no fixtures and are not replayed. The agent broke
      the code 15 ways on a scratch copy and each break was caught by a new test.
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

- [x] Leave it running ≥ 6 hours. Session sensor stays `active`, and
      `seconds_remaining` in its attributes recovers after each keep-alive.
      Note the values seen.
      **Reported working by the user (16 Nov 2026).** The session has stayed `active` across
      long runs and several restarts. The `seconds_remaining` values were not recorded.
      *If the session dies:* test whether `retrieveAccountHolderInfo` really
      extends it (CLAUDE.md #4). Try loading `USAGE_PAGE` as the ping instead,
      or lower the interval.
- [x] Restart HA mid-session: it resumes without asking for a code (the session
      store is used, and no page load is needed).
      **Passed (16 Nov 2026):** HA was restarted several times during 0.2.0b5 to 0.2.0b7
      testing and the integration came back with the session `active` and no sign-in prompt.
- [ ] Stop HA for > 2 h, then start it: a reauth prompt appears; signing in again
      resumes and fills the gap in statistics without duplicates.
- [ ] Options: change the keep-alive interval; the entry reloads and uses it.
- [x] Sign out of myEnergy in a browser: confirm whether that kills the HA
      session (expected: it does, then reauth is prompted). Document the result in README.
      **Confirmed by the user (16 Nov 2026).** Recorded here as the expected behaviour: signing
      out ends the integration's session and HA asks to sign in again. The README's "Sessions
      and re-signing in" section already lists signing out everywhere as a cause.

## Phase 5: Data correctness edge cases

- [ ] Day-after behaviour: poll before and after ~01:00 AEST; the new day appears
      and the next poll changes nothing (`hours_added_last_fetch` = 0).
- [x] **Accepted limitation (user, 16 Nov 2026): daylight saving versus NEM time at day edges.**
      The meter data is on fixed AEST (NEM time) and a daylight-saving tariff prices on the
      wall clock, so an hour near a day edge, or on a 23- or 25-hour changeover day, can be
      counted differently from the retailer's bill. Seen as a small shortfall on a monthly bill
      (about 1.5 kWh of Off Peak, under 1%). The cause was not confirmed against daily figures
      and the user accepts the difference for an estimate. This is separate from the unticked
      item above, which is about when the portal publishes a day.
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
      `OWNER` replaced. `codeowners` is `["@markesss"]` (set 16 Nov 2026).
- [x] CI green (hassfest, HACS, ruff, pytest).
- [ ] Update README with live-test results and anything learnt in Phases 3–5.
- [x] Bump `version` in `manifest.json`; tag a `v0.1.0` release.
      Was v0.1.2 (v0.1.1 added the connection-error logging, v0.1.2 the TLS fix); now v0.2.1,
      below. Each tag also needs a GitHub Release for HACS (GitHub releases do not mirror from
      Gitea, so they are created by hand).
- [x] **0.2.0 released (16 Nov 2026).** The 0.2.0b1 to 0.2.0b10 work on
      `feature/tariff-cost-tracking` was squash-merged into `main` by PR #2 (`60b2a70`), tagged
      `v0.2.0` and released on GitHub (Latest). `main` now takes changes through pull requests
      only. The `v0.2.0b*` tags still point at the old branch commits, which are not in `main`'s
      history after the squash.
- [x] **0.2.1 (CI only, 16 Nov 2026).** PR #3 removed `ignore: brands` from the HACS job (the
      check passes on GitHub with the images in `brand/`), and PR #4 bumped the manifest to 0.2.1
      (`368f7ca`, tag `v0.2.1`). The Validate run on `main` for that commit passed (hassfest, HACS
      and ruff + pytest, with the coverage gate), and the GitHub release `v0.2.1` was created
      after it, which is the order the HACS default store's PR template asks for.
- [~] **HACS default store submission (16 Nov 2026).** `hacs/default` PR #14280, from
      `markesss/default` branch `add-ha-citipower-myenergy`: one commit adding
      `"markesss/ha-citipower-myenergy"` to the `integration` file (alphabetical, case-insensitive,
      between `Marker284/ha-minecraft-realms` and `markfrancisonly/ha-cameralux`; the repo's own
      `scripts/is_sorted.py` passes). The template's boxes are ticked and its links are the
      `v0.2.1` release and the passing Validate run, which holds both the HACS and hassfest jobs.
      Its lint checks passed and its Check workflow's jobs passed or were skipped. **Now waiting
      on the HACS queue** (new additions take months; do not request reviews, which closes the
      PR). If a bot drafts it, fix the point it names and press Ready for review. The commit
      message carries a `Co-Authored-By` trailer for Claude. To check on the repo page: the topic
      `smart-merge` looks like a typo for `smart-meter`, and `v0.2.0b8` and `v0.2.0b9` should be
      marked Pre-release.
- [ ] Optional: post on the HA community forum (thread "Reading Australian
      smart meter data" has people asking for exactly this).

## Phase 7: Cost tracking (flat and time-of-use)

Goal: the Energy dashboard shows cost for grid import and export. The portal has no
price data (the retailer sets prices, not the network), so the user enters their tariff
and the integration writes companion **cost statistics** that the dashboard reads as
"Use an entity tracking the total costs".

### Research findings (GloBird Victoria, 15 Nov 2026)

Sources: `globirdenergy.com.au/legal/flexible/` and the CitiPower FOUR4FREE fact sheet
(offer GLO812620MR, CZ 6). Use them to keep the data model honest, not as a spec for one
retailer.

- **Time windows are the same on all five Victorian networks** (CitiPower, Powercor,
  United Energy, AusNet, Jemena). Only the distributor tariff codes and the **rates**
  differ. So the model is per config entry (one NMI, one network); no network-keyed table.
- Residential windows apply **every day**. There is no weekday/weekend split and no
  seasons. Business tariffs are weekday-only (e.g. 9am–9pm), so allow an optional
  day-of-week set per window.
- Windows **cross midnight** (off-peak 9pm–11am; EASYEV 12am–6am). Some plans have a
  "shoulder = all other times" catch-all.
- **Per-day allowances inside a period.** FOUR4FREE 11am–3pm: first 30 kWh/day at
  0.00 c/kWh, the remainder at 11.00 c/kWh. Export 4pm–11pm: first 30 kWh/day at
  5.00 c, remainder 3.00 c. A flat rate per period is not enough; a period needs
  optional tiers with a per-day threshold that resets each local day.
- Real CitiPower FOUR4FREE (inc. GST): supply 129.80 c/day; 4pm–11pm 36.63 c;
  11am–3pm tiered as above; 3pm–4pm and 11pm–11am 22.00 c; feed-in 11pm–4pm 0.00 c.
  Good as a test fixture.
- **Discounts** (2% pay on time + 1% direct debit) apply to supply and usage charges,
  **not** to solar credit. Model one percentage discount applied to charges only.
- **GST:** fact-sheet rates are inc. GST; other retailers quote ex. GST. Needs a toggle.
- **Timezone.** GloBird says "local time (NO change for daylight savings)", which is
  ambiguous on its own. Decided by the user: always fixed **AEST (NEM market time,
  UTC+10, no DST)**, the same clock as the NEM12 timestamps. No wall-clock mode.
- Rates are reviewed whenever the network tariffs change, and window definitions depend on
  sign-up date (e.g. FOUR4FREE off-peak moved from 10am–2pm to 11am–3pm on 1 Jul 2026).
  So the tariff must be **versioned by effective date**.
- Not covered, deliberately: controlled load (a separate channel, charged regardless of
  time), demand charges, public-holiday rules, concessions. None of the residential plans
  read needed holidays. Revisit if a user needs them.
- Not found: the fact-sheet rates for the other plans, the WHOLESAVE schedule mapping,
  and GST wording on the flexible page. Do not hard-code presets until verified.

### Data model (pure, no HA imports; new `tariff.py`)

```
Tariff
  versions: list[TariffVersion]          # sorted by effective_from, first covers all earlier dates
TariffVersion
  effective_from: date
  prices_include_gst: bool               # if false, multiply by 1.1 when costing
  supply_c_per_day: float                # charged once per local day
  discount_pct: float                    # on supply + usage, not export
  import_periods: list[Period]
  export_periods: list[Period]
Period
  name: str                              # "Peak", "Free window"...
  windows: list[Window]                  # one rate can cover several windows (export 9pm–10am + 2pm–4pm)
Window
  start: time, end: time                 # end <= start means it crosses midnight
  days: set[int] | None                  # None = every day
  tiers: list[Tier]                      # one open-ended tier = a flat rate
Tier
  up_to_kwh_per_day: float | None        # None = remainder
  c_per_kwh: float
```

Validation: every minute of a day on every applicable day is covered by exactly one period
(overlap and gap checks), or one period is marked `catch_all`. Tiers: only the last may be
open-ended, thresholds ascending.

### Tasks

- [x] **`tariff.py` (pure).** Parse and validate the model above; `period_for(local_dt)`;
      `cost_for_day(hours: dict[datetime, float], version) -> dict[datetime, float]` that
      walks a local day's hours in order, tracking per-period tier usage, and returns
      each hour's cost. A period may span part of an hour boundary only if windows are on
      whole hours, so restrict window edges to the data resolution (whole hours) and
      say so in the UI. Tests: flat rate, three-band TOU, the FOUR4FREE fixture
      (free first 30 kWh), midnight-crossing window, day-of-week window, gap and overlap
      rejection, windows read in fixed AEST across Melbourne's DST changes, GST on and
      off, discount.
      Done: `tariff.py` and `tests/test_tariff.py` (62 tests; whole suite 91 passed on
      Python 3.11 without HA), `TariffError` added to `exceptions.py`. Decisions:
      - **API differs from the plan in one way:** `cost_for_day(hours, tariff, kind, day)`
        takes the local `day` explicitly, because the tariff version is chosen by that date.
        Every hour must fall in that local day or `TariffError` is raised. Helpers
        `local_day(moment)` and `local_day_hours(day)` give callers the grouping and the
        24 UTC hours of a day. `TariffVersion.period_for(local_dt, kind)` takes an AEST
        datetime (only weekday and hour are read).
      - **Local day:** always midnight to midnight AEST (`const.MARKET_TZ`), exactly 24 h.
        The Melbourne wall-clock mode (`timezone_mode: clock`, 23/25-hour days) was built
        and then **removed** on 15 Nov 2026 per the user's decision (AEST/NEM time is
        firm). `from_dict` rejects a stored `timezone_mode` key with a clear message so a
        tariff written for a wall clock can never be silently priced on the wrong one.
        The tariff tests no longer need `tzdata`.
      - **Window days** are the days a window *starts* on, so Friday 10pm–2am covers
        Saturday 1am. `end == 24` is midnight; `start == end` is rejected (use 0–24).
      - **Tiers** are cumulative per-day thresholds, tracked per period; the last tier must
        be open-ended. Period names must be unique; `catch_all` periods have no windows.
      - **GST is applied to import charges only.** Feed-in is paid as entered
        (fact sheets say "incl. GST if any"). Discount: import supply + usage only. Supply
        charge: first hour of the day, import only (earliest hour given if the first is
        missing). Say if GST should also uplift export.
      - Costs are returned in dollars, unrounded; export is a positive credit. Rounding and
        sign are the statistics writer's job (see the sign-convention test).
      - `flat_tariff(...)` convenience builder added for the "flat rate" UI path.
- [ ] **Persistence.** Store the tariff in `entry.options` (JSON-serialisable; add a
      schema version key). No migration needed for entries without it: cost tracking is
      off until a tariff is configured.
- [x] **Options flow UI.** Built (`tariff_flow.py`, pure helpers in `tariff_form.py`, strings,
      `tests/test_ha_options_flow.py` and `tests/test_tariff_form.py`); see *Findings* below
      this task's step list. Home Assistant has no table editor, so build a menu-driven flow:
      1. *Tariff type*: flat rate or time of use.
      2. *Basics*: effective-from date, the fixed AEST/NEM-time note (text only, see the
         timezone note under *Verify first*), GST included, supply charge, discount %.
      3. *Import periods*: list view with add / edit / delete. Each period form: name,
         start hour, end hour, optional weekday selector, and a rate (flat) or tiers.
         Flat rate is a single "All day" period, so both modes share one code path.
      4. *Export periods*: same, optional (skip for accounts with no `B1`).
      5. *Review*: show the resolved 24-hour map per day type and any gap/overlap error
         before saving.
      6. *Advanced*: edit the whole tariff as YAML via `ObjectSelector`, for copy-paste
         and for tariffs the guided steps do not cover.
      Reuse the same validation as `tariff.py`. Add strings and translations.

      *Findings (options flow UI):*
      - **Step map.** `init` menu → `keepalive` (unchanged form, now merges into the existing
        options so the tariff survives) | `tariff` menu. No tariff: `tariff_setup`
        (`tariff_type` → `tariff_basics` → `tariff_flat_rate` or `tariff_import_periods`) or
        `tariff_advanced`. With a tariff: `tariff_edit_latest`, `tariff_add_version` (copies
        the latest, today's date, refuses a date already used), `tariff_recalculate` (from-date
        form → `statistics.async_recalculate_costs` per known channel, then aborts with
        "rewrote N hours"), `tariff_advanced`, `tariff_remove`. Periods menu: `period_add`,
        `period_edit` / `period_delete` (pick by name), `periods_done`. Import periods →
        (export periods, only with a `B1` channel in the session store or an existing export
        tariff) → `tariff_review` → `review_save`.
      - **Period form** is flat: name, rate (c/kWh), hours as text (`16-21`, `4pm-9pm`,
        `9pm-10am, 2pm-4pm`; whole hours, end <= start crosses midnight, `0-24` all day) and an
        optional weekday multi-select shared by every window of the period.
      - **Tier preservation (decision).** The guided editor *refuses* to edit a period with tiers,
        an "all other times" rule or windows on different days (shown as advanced in the list;
        edit → error `period_not_guided`, pointing to the YAML editor). Untouched periods are
        carried through byte-for-byte when other fields or periods change; deleting is allowed.
      - **Review** is a menu so a gap or overlap can't be saved: the description carries the
        resolved hour map (grouped by day pattern, labelled AEST/NEM) and, when invalid, the
        `tariff.py` error; `review_save` is only offered when `Tariff.from_dict` accepts it.
      - **Timezone** is fixed text (the exact `TIMEZONE_NOTE` sentence) in every step that takes
        times; a test keeps strings.json in step with the constant. No choice is offered.
      - **Options saves take effect** through the existing update listener in `__init__.py`
        (no change needed): saving reloads the entry, whose first refresh writes cost rows for
        the new tariff. Recalculate changes no options, so it runs immediately and does not reload.
      - Stored but invalid tariff: the tariff menu says so and offers a fresh start or YAML.
      - **Not covered / follow-up.** Export steps were tested with synthetic `B1` suffixes only
        (the user does not export). `tariff_remove` keeps already-written cost statistics.
        Tests run on Linux in Docker (HA 2026.2.3 as pinned by the test package, Python 3.13):
        the HA test package cannot import on Windows (`fcntl`).
- [x] **Cost statistics.** In `statistics.py`, write `…_e1_cost` (and `_b1_cost`) as
      external statistics: `unit_of_measurement="AUD"`, `has_sum=True`, `unit_class=None`,
      `mean_type=NONE`. Export is a *compensation* statistic, written as **positive** money
      earned (confirmed by the live sign test in *Verify first*).
      Compute costs per **whole local day**, because tiers and the supply charge are
      per-day. Charge the supply charge on the first hour of each local day, in the import
      cost statistic only.
      Done (15 Nov 2026), import side; export is built but untested against live data.
      Decisions:
      - **Ids and units:** `citipower_myenergy:<nmi>_e1_cost` (`cost_statistic_id_for`);
        export compensation is `…_b1_compensation`, not `_b1_cost`, because the dashboard
        treats it as compensation. The unit is `hass.config.currency`, **not** a hard-coded
        `AUD`, so other users' pickers keep listing it. `unit_class=None`.
      - **Only E1 and B1 are priced.** E2 (usually controlled load) follows different
        rules, so it is skipped. B1 is written only if some tariff version has export periods.
      - **Rounding:** each hour's `state` is rounded to a millionth of a dollar and `sum`
        accumulates those rounded states, so the sum is exactly what the rows add up to.
        Per-hour rounding to cents would drift by dollars over a year; micro-dollars cannot.
      - **Partial days:** only whole, finished AEST days (all 24 hours present, day over) are
        priced; anything else writes nothing, so a running total never has to be undone. A
        day that arrives late is slotted in by the repair rule.
      - **First run with a tariff** prices the stored kWh history too (not just the poll's
        7-day window), by merging stored kWh rows under the freshly fetched ones.
      - **Tariff edits only reprice the poll window automatically** (the last ~7 days, where
        stored cost disagrees with the new price); older days keep their old price until
        `async_recalculate_costs` is run. Surface that in the UI ("Recalculate history").
- [x] **No double counting.** Reuse the existing repair logic: whenever kWh hours are
      rewritten, rewrite the cost rows for every affected local day with the running sum
      recalculated. A tariff change must not silently reprice history: new versions apply
      from `effective_from`, and a "Recalculate history" action (options step or service)
      rewrites cost from a chosen date. Tests in `test_ha_statistics.py`: first import,
      incremental import, estimated-then-actual repair, tariff version boundary,
      recalculation, idempotent re-run (no change in sums).
      Done: the repair rule is "from the first day whose stored cost differs from what the
      tariff now gives (or that was skipped mid-history), rewrite every complete day on with
      the sum recalculated from the row before it". An unchanged re-run writes nothing.
      `async_recalculate_costs(hass, nmi, name, suffix, tariff, from_day)` reprices from the
      stored kWh statistic (no portal call) and is **not yet exposed in the UI**.
      16 tests in `tests/test_ha_statistics.py` (first import, incremental, idempotent,
      corrected reading on day one and on day two, tariff change, version boundary, partial
      day, skipped day filled later, first-run backfill, recalculate, currency, E1/B1 only,
      export positive, kWh untouched), each also checking every sum equals the running
      total of the states.
- [x] **Coordinator.** After inserting kWh, call the cost writer for the same channels. If
      no tariff is configured, skip it. Add a *Latest day cost* sensor per channel
      (monetary device class, AUD) if it proves useful; the statistics are the point.
      Done: `coordinator.py` reads `entry.options[CONF_TARIFF]` (new const, `Tariff.to_dict`
      form), skips cost when absent, ignores a saved tariff that fails validation with a
      warning, and logs (with traceback) any other error in the cost step without stopping
      the kWh import. `tests/test_ha_coordinator_cost.py` (5 tests) covers all four paths.
      No *Latest day cost* sensor: not needed yet.
      Test environment: native Windows cannot import the HA test package (`fcntl`), and only
      Python 3.11 is installed, so the suite ran in Docker (`python:3.13`, HA 2026.2.3 as
      pinned by the test package; the live HA is 2026.11.1, whose metadata fields matched).
- [ ] **Diagnostics.** Include the tariff structure (prices are not secret), redacted
      like everything else.
- [ ] **Docs.** README section: how to configure a flat rate and a TOU tariff, worked
      example from the FOUR4FREE fact sheet, and how to select the cost statistic in
      *Settings → Dashboards → Energy → Grid consumption → Use an entity tracking the
      total costs*. State plainly that costs are estimates, not a bill.
- [ ] **Decisions (user, 15 Nov 2026):** tiers are not in the guided UI (YAML editor only);
      tariff changes are versioned by effective date and history is repriced only by an
      explicit "recalculate" action; **no retailer presets**.
- [ ] **Per-period statistics (coloured bars).** Live probe, 15 Nov 2026, HA 2026.11.1:
      three real-usage kWh statistics added as three grid consumption sources, each with its
      own cost statistic, **render in different colours**, HA accepts several grid sources,
      and totals matched (kWh exactly; cost + supply = stored `_e1_cost`). Decisions (user):
      - Build it as an **opt-in** option ("Also record a statistic per tariff period").
        The combined `…_e1` and `…_e1_cost` stay exactly as they are.
      - For each import period write `…_e1_<period_slug>` (kWh) and
        `…_e1_<period_slug>_cost` (currency). Period names come from the tariff; slugs must
        be stable, unique and survive renames sensibly (decide: slug from name, collisions
        suffixed; renaming a period creates new statistics, leaving the old ones orphaned,
        so warn in the UI).
      - **Supply charge: not applied in per-period mode** (HA's daily adjustment is not
        applied with TOU billing meters; user decision 16 Nov 2026 to document it). This
        **supersedes the earlier decision to use HA's fixed daily adjustment** (`cost_adjustment_day`).
        The per-period cost statistics **exclude** the supply charge; the combined cost
        statistic keeps including it, and the values are unchanged. The UI shows the daily
        supply amount for reference only (supply charge x (1 - discount), GST-adjusted
        exactly as `tariff.py` does; GLOHOME: 92.40 c x 0.97 = $0.89628/day), says the
        dashboard does not add it, and says to leave the dashboard's daily adjustment at 0.
        To see the full cost use the combined cost statistic, never alongside the per-period
        sources (it double counts); the UI text and README say so. Possible future fix:
        attach it to the first-hour period cost or add a supply-charge source.
        **Superseded as a fixed behaviour, 16 Nov 2026 (user decision): a choice.** The
        `supply_in_periods` option (`none` default, `spread`, `first_hour`) says where the
        daily supply charge goes in the period costs. `none` is the behaviour above. `spread`
        adds one share (daily supply / hours the local day really has: 23, 24 or 25) to every
        hour, `first_hour` adds all of it to the day's first hour; both make the periods add up
        to the combined cost (per day for spread, per hour for first_hour). Flat rates have no
        choice and stay on `none`. The combined cost statistic is unchanged. The per-period
        step of the options flow chains into a "Supply charge in period costs" step (only for a
        time-of-use tariff with the option on) with a recalculation tick-box, ticked by default.
        Tests: `test_tariff_supply_modes.py`, `test_ha_statistics.py` (`test_supply_mode_*`),
        `test_ha_options_flow.py`, `test_ha_coordinator_cost.py`. Not yet live-verified.
      - Per-period costs must sum with the supply charge to the combined cost: add a test
        (sum of period costs + days x daily supply == combined cost) at hour and day level.
      - Reuse the whole-day pricing and repair logic; a tariff version or reading change
        reprices every affected day for all period statistics together; a period list
        change (rename, add, remove) needs a recalculation path. Export periods: out of
        scope (no `B1`), but keep the design from blocking it.
      - Needs `tariff.cost_for_day` to return per-period breakdowns (kWh and cost per
        period per hour) without changing its current total; add that to `tariff.py` with
        tests.
      - Probe cleanup: remove the probe sources from the dashboard, run `stat_probe.clear`,
        remove `stat_probe:` from `configuration.yaml`, delete the folder (it holds real
        usage data), restart. Done only when the user says so.

      **Built (uncommitted at the time of writing; 272 tests pass in Docker, Python 3.13.18,
      HA test package 2026.2.3, ruff clean). Not yet tried on the live HA.**
      - `tariff.py`: `cost_for_day_by_period` (kWh, cost per hour per period, supply charge
        excluded), `daily_supply_dollars`, `period_slug`/`period_slugs`. `cost_for_day` and the
        breakdown share one pricing pass (`_price_day`), so totals cannot diverge; the existing
        `cost_for_day` tests pass unchanged.
      - **Slugs:** lowercase `[a-z0-9_]`, runs collapsed, no edge underscores (HA statistic ids
        reject them), capped at 40; empty becomes `period_<n>`; clashes get `_2`, `_3`. A slug
        is never `cost` or ending `_cost` (it would make one period's kWh id equal another's
        cost id, or the combined `_e1_cost`): those get a `_period` suffix.
      - **Versions with different period sets:** the statistic set is the **union of every
        version's period names** (oldest first), so adding a newer version never changes an
        existing slug. A day priced under a version that lacks a period writes **zero** for
        it, so every statistic has a row for every hour and per-hour kWh always adds up.
      - `statistics.py`: the combined cost writer now uses a shared multi-series writer
        (`_async_write_series`), behaviour and tests unchanged. Period statistics are written
        by `async_insert_period_statistics` (E1 only, whole finished days only). **All period
        statistics are repaired together:** if any disagrees for a day, or has no rows while
        another does (a period was added), every one is rewritten from that day with each
        running sum recalculated from its own previous row. A new period triggers the
        full-history path, so it is backfilled from stored kWh. Re-runs write nothing.
        `async_recalculate_costs(..., period_statistics=True)` rewrites them too (the return
        value still counts the combined cost's hours only).
      - **Renamed/removed periods are orphaned, never deleted:** they stop being written and
        stay in the database; the UI text and README point to Developer Tools > Statistics.
      - Export is not split (no `B1` on this account); `_b1_compensation` is unchanged.
      - Option `CONF_PERIOD_STATS` (`period_statistics`, default off) in `entry.options`;
        coordinator writes the statistics after the combined cost when it is on and a tariff
        exists; a failure there is logged (with traceback) and never stops the kWh import or
        the combined cost.
      - UI: tariff menu entry "Statistics per tariff period" (only with a valid tariff): a
        switch plus text with the daily supply amount for reference only (`AUD 0.89628` for
        GLOHOME), the statistic ids and the double-count warning; it says the supply charge is
        not applied in this mode and to leave the dashboard's daily adjustment at 0 (reworded
        16 Nov 2026, see the decision above). Saving reloads the entry (existing listener) and
        the first poll backfills. Recalculate passes the option through. Strings and
        `translations/en.json` identical.
      - **Backfill cost:** pricing 515 days into 6 statistics (74,160 rows) takes ~0.09 s of
        CPU to build (measured), written through the recorder queue, so it does not block setup
        noticeably; the poll's portal fetch is still only the last 7 days.
      - Diagnostics: new `cost_tracking` summary (versions, supply, discount, GST, period names,
        slugs, option). The raw tariff was already in `options`; the NMI is not in the summary.
        Tests added in `tests/test_ha_diagnostics.py`.
      - Docs: README "Cost tracking" section, `docs/ARCHITECTURE.md` "Cost tracking" section and
        diagram nodes, `CLAUDE.md` layout lines appended.
      - **Superseded: the daily adjustment.** The build agent wrote the UI to ask for the
        adjustment on one grid source only. The live test showed the dashboard does **not**
        apply `cost_adjustment_day` to per-period (TOU) sources, so that advice was removed;
        the supply charge is simply not shown in per-period mode (see the decision above).
- [x] **Tariff timezone option (16 Nov 2026; supersedes "always fixed AEST").** The user found
      GloBird charges TOU periods on the clock *with* daylight saving, so the AEST-only rule
      was wrong for them. Decisions:
      - Each tariff version gets a `timezone` (IANA name). Periods, the local day (midnight to
        midnight, so 23 or 25 hours on changeover days) and the daily supply charge use it.
      - The UI offers a **short list**, not every zone: Australian zones plus GMT. Only zones
        with a **whole-hour UTC offset** can be offered, because the statistics are hourly
        on UTC hour boundaries and whole-hour windows must line up with them. So: Melbourne,
        Sydney, Hobart, Canberra (AEST/AEDT), Brisbane (AEST, no DST; this is also the
        NEM/market clock), Perth (AWST), and GMT (UTC). **Not offered: Adelaide, Broken Hill,
        Darwin, Eucla, Lord Howe** (half-hour or 45-minute offsets; would need half-hour
        statistics). Say so in the UI text and README.
      - **Default for new tariffs: Australia/Melbourne** (Victorian networks). **A stored tariff
        with no timezone keeps pricing on fixed AEST (UTC+10, no DST)**, i.e. exactly what
        v0.2.0b1/b2 did, so nothing reprices silently. The options flow shows the stored
        value and tells the user how to change it.
      - Changing the timezone changes how history is priced: saving a changed timezone must
        prompt for or trigger "Recalculate cost history" (and the period statistics).
      - Replaces the fixed "all times are AEST" text with a timezone selector plus a note that
        retailer sheets say "local time" and the bill is the authority; mention that the
        dashboard's own day follows Home Assistant's timezone.
      - `tzdata` goes into `requirements-dev.txt`; tests must cover DST changeover days
        (23 and 25 hour days: first-hour supply charge, tiers, per-period breakdown, the
        whole-day statistics writer, repair, recalculation) and the GMT and Brisbane cases.

      **Built (uncommitted; 394 tests pass in Docker, Python 3.13, HA test package 2026.2.3,
      ruff clean). Not yet tried on the live HA.**
      - `tariff.py`: `TariffVersion.timezone` (IANA name or `None`), `ALLOWED_TIMEZONES`
        (Melbourne, Sydney, Hobart, Canberra, Brisbane, Perth, GMT), `DEFAULT_TIMEZONE`
        (Melbourne), `LEGACY_TIMEZONE` (Brisbane), public `zone_for`. `None` is fixed AEST
        (`const.MARKET_TZ`, no tz database needed); `GMT` is `UTC`; the rest use `zoneinfo`.
        A missing database raises `TariffError` naming `tzdata`. `UTC` is accepted on input and
        stored as `GMT`. `to_dict` omits the key when unset, so a stored tariff round-trips
        unchanged and nothing reprices until the user saves a choice. `timezone_mode` is still
        rejected, now pointing at `timezone`.
      - **Local day:** `local_day(moment, tz)` and `local_day_hours(day, tz)` (default AEST, so
        existing callers are unchanged); a day is midnight to midnight on the tariff's clock,
        23 or 25 hours on changeover days. Windows match the local wall-clock hour, so on a
        25-hour day the repeated hour matches its window twice and on a 23-hour day the skipped
        hour does not exist. Supply charge: once, on the first real hour of the local day.
        Tiers reset at local midnight. Per-period breakdown invariants hold on those days.
      - **Decision: one clock per tariff.** `Tariff.validate` rejects versions with different
        zones (no zone and Brisbane count as the same clock). Mixing them would leave an hour
        belonging to no day, or to two, at the version boundary; a retailer's clock does not
        change with its prices. The UI sets the zone for every version at once.
      - **Whole-hour offsets are asserted by a test** over four years for every offered zone,
        and the refused zones (Adelaide, Darwin, Eucla, Lord Howe, Broken Hill) are checked to
        really have fractional offsets.
      - `statistics.py`: `_complete_days` and the repair start use the tariff's clock and each
        day's real hours (24, 23 or 25); a day with the wrong number of readings is not written.
      - **Bug found and fixed while testing a zone change:** a rewrite covers whole local days,
        so after a clock change the last complete day ends at a different instant and an hour
        priced on the old clock was left just beyond the rewritten rows with its OLD running
        total, which the dashboard would show as a one-hour spike or dip until the next day
        completed. `_async_keep_the_tail` now re-chains any stored rows after a rewrite (keeping
        their own state, recalculating their sum). A test fails without it (stale sum 5.30299
        against 5.46304). A stray old-clock hour at the very START of history is harmless: the
        rewrite's base sum includes it, so the rows still add up.
      - Options flow: the fixed AEST text is replaced by a **Pricing clock** drop-down in the
        Basics step (exactly the allowed zones, labelled "Melbourne (AEST/AEDT, follows
        daylight saving)", ..., "Brisbane (AEST, no daylight saving; NEM time)", "Perth (AWST,
        no daylight saving)", "GMT"; default Melbourne for a new tariff; a stored tariff with no
        zone shows Brisbane, with a note that it is fixed AEST). Help text: retailer sheets say
        "local time" so the bill is the authority; the five half-hour zones are not offered and
        why; the dashboard's day follows Home Assistant's timezone. Every step that shows times
        carries a `{timezone}` sentence naming the chosen clock; the review map is headed with
        the clock and mentions 23/25-hour days for DST zones. The tariff menu summary ends with
        "Clock: ...". Choosing Brisbane on a tariff with no stored zone writes nothing.
      - **Saving a changed clock:** the flow asks first (`tariff_timezone_changed`): a ticked box
        (default on) recalculates ALL recorded costs now, from the stored readings, using the NEW
        tariff and before the options are saved, so the reload that follows finds nothing to
        change; unticking saves anyway and the text says to run "Recalculate cost history"
        later. If the recalculation fails the question stays open with an error and nothing is
        saved, and the user can decline and save. A brand new tariff is not asked. The shared
        repricing loop is `_async_reprice`, also used by the Recalculate step.
      - Diagnostics report `timezone` and `timezone_set`. Docs: README (the clock choice table,
        GloBird note, unsupported zones, dashboard day note), CLAUDE.md rule (NEM12 stays AEST;
        the tariff clock is selectable; never assume 24-hour tariff days), docs/ARCHITECTURE.md,
        docs/TESTING.md; `tzdata` added to `requirements-dev.txt`.
      - New tests: `tests/test_tariff_timezone.py` (pure: zone list, whole-hour offsets, round
        trips, validation, GLOHOME under Melbourne vs fixed AEST with hand-computed values,
        25- and 23-hour days with the repeated/skipped hour, tiers resetting, per-period
        invariants on four changeover days, GMT, Perth, Brisbane equals old behaviour);
        `tests/test_ha_statistics.py` (25/23-hour days, wrong-length day not written, idempotent
        re-run, fixed AEST unchanged, GMT, a zone change repricing with a consistent chain,
        explicit recalculation across history and its idempotence, per-period invariants across
        daylight saving); options-flow tests (selector contents and default, legacy shown as
        Brisbane and left unwritten, the clock named on every step, the change prompt with
        repricing, declining, failure, uniform clock, advanced YAML); form and string tests.
      - **Risks for the first live run:** (1) the clock-change recalculation reprices from
        2000-01-01 over every stored hour, in one go inside the options flow (the earlier
        benchmark suggests about a tenth of a second of CPU per 74,000 rows, plus the read);
        (2) the Docker tests ran on HA 2026.2.3, not 2026.11.1; (3) production needs a time zone
        database: Home Assistant images have one, but a bare Windows dev machine needs
        `tzdata`; (4) the Energy dashboard's own day follows Home Assistant's timezone
        (Australia/Sydney on the live HA), so a few cents can move across its day edge; (5) a
        stray old-clock hour at the end of history is re-priced when the next day completes.
- [x] **Live tariff sensors (16 Nov 2026).** Requested by the user to replace their hand-built
      template package (`globird_time_of_use_rates.yaml`: current rate, period, minutes until the
      next period). Built: `period_at`, `rate_at`, `next_period_change` in `tariff.py` (pure) and
      three sensors in `sensor.py` (current tariff period, current tariff rate, next tariff period
      starts), created only when a valid tariff is saved. One timer set for the next boundary, no
      polling, no recorder access. Decisions: the rate is the headline rate including GST and
      before the discount (Off Peak reads 0.165, as the template did) with the discounted rate as
      an attribute; a tiered period reports its first tier and `tiered: true`; "next change" means
      a different period name or different rates, so a midnight-crossing Off Peak is one stretch
      and a supply-charge-only version is not a change; no minutes-until sensor (a template in the
      README). Tests: `test_tariff_live.py` (pure, incl. the 23- and 25-hour days) and
      `test_ha_tariff_sensors.py` (real setup, frozen time, boundary crossing, reload, unload).
      **Still to verify live:** the sensors against the clock on the user's HA, and that the old
      template package can be removed.
- [ ] **Later (backlog):** controlled load (`E2`), public holidays, demand charges.

### The live test plan: GloBird GLOHOME Residential (Flexible Rate), CitiPower, CZ 6

Offer GLO811981MR, released 28-10-2026
(`…/Victorian_Energy_Fact_Sheet_GLO811981MR_Electricity_CZ_6.pdf`). All rates inc. GST.

| Item | Rate |
|---|---|
| Daily supply | 92.40 c/day |
| Import 4pm–9pm | 31.46 c/kWh |
| Import 9pm–11am (crosses midnight) | 16.50 c/kWh |
| Import 11am–4pm | 13.20 c/kWh |
| Export 4pm–9pm | 3.00 c/kWh |
| Export 9pm–10am and 2pm–4pm | 0.10 c/kWh |
| Export 10am–2pm | 0.00 c/kWh |
| Discount | 2% pay on time + 1% direct debit, on supply and usage only, not on solar credit |

What this tells the design:
- **Import and export windows differ**, so the two period lists must be independent
  (confirmed; already in the model).
- One export band is **two disjoint windows** (9pm–10am and 2pm–4pm) with one rate. The
  period model must allow either several windows per rate or two periods sharing a name.
  Decision: a period holds a **list of windows**, so "Off-peak export" is one period.
- It has **no tiers**. Tiers (FOUR4FREE's 30 kWh/day allowances) stay in the data model
  and `tariff.py`, but the guided UI for them can wait; they remain reachable through the
  advanced YAML editor.
- **The user's account has no `B1` channel** (confirmed, they do not export). Export cost
  is only written if a `B1` channel appears, and the export steps are hidden otherwise.
  The export rows in the table above are retained as a `tariff.py` test fixture only.
- Use this tariff as the first `tariff.py` fixture, with the FOUR4FREE one second (tiers).
- Acceptance: compare one month's estimated cost with the real GloBird bill. Expect a
  small difference from rounding, the discount, and any bill adjustments.
- **Live check, v0.2.0b1 on HA 2026.11.1 (15 Nov 2026):** the GLOHOME tariff entered through
  the options UI saved exactly as the sheet. `…_e1_cost` (AUD, `has_sum`) covered 12,360
  hours / 515 days with 0 mismatches against an independent hour-by-hour recomputation
  (max difference $0.000001; total $1,602.37), and the running sum was consistent. The
  user selected it as the grid "total costs" source and the Energy dashboard shows it.
  **Real-bill comparison done (16 Nov 2026), after the Melbourne pricing clock was set:** one
  day matched the retailer's portal to the cent. Two monthly bills were then compared: the
  integration was 3c under on the earlier bill and about 22c under on the latest, both well
  under 1%. Both gaps are on the low side. On the latest bill the gap is about 1.5 kWh of
  Off Peak (22c at the after-discount Off Peak rate), so it is a small amount of off-peak energy
  rather than a rate or supply-charge error. Cause not confirmed: most likely a day or
  billing-period edge, perhaps the 4 Oct 2026 clock change (a 23-hour day) falling inside that
  bill. To pin it down, compare daily kWh with the retailer's portal for the days in the bill.
  Judged acceptable for an estimate. History before the tariff's `effective_from` is
  priced at the first version's rates (add earlier versions and recalculate for accuracy);
  HA's energy prefs now have a native `cost_adjustment_day`, which must stay 0 because our
  cost already includes the supply charge.

### Verify first (do before building the UI)

- [x] ~~Total costs picker accepts an external statistic.~~ Confirmed by the user: the
      dashboard UI offers a total-costs external statistic option. Still check, in the
      first live run, that *our* cost statistic (AUD, `has_sum`) is listed in the picker.
- [x] ~~Export compensation sign convention.~~ **Confirmed live (15 Nov 2026, HA 2026.11.1)
      with `tools/stat_probe`:** a **positive** compensation statistic (+$0.10/h) *lowers*
      the net grid total by $2.40 over 24 h; a **negative** one (-$0.10/h) *raises* it by
      $2.40. So export compensation is written as **positive money earned**, and
      `cost_for_day(..., kind="export")` already returns a positive credit, so no sign
      flip is needed in the writer. Import cost is positive as expected ($4.80/24 h).
      Not yet checked: which pickers listed each probe statistic, and the HA currency
      setting vs. our `AUD` unit (do this in the first live run of the real statistics).
      The probe is still installed on the live HA: run `stat_probe.clear`, remove the
      `stat_probe:` line, delete the folder and restart when finished with it.
- [x] ~~Retailer's plan found.~~ GLOHOME Flexible Rate, CitiPower (above).
- [x] ~~Timezone mode.~~ **Decided by the user: tariff periods must be fixed AEST (NEM
      market time, UTC+10, no DST).** There is a single mode; the Melbourne wall-clock
      mode was removed from `tariff.py`. **UI decision (user): show the timezone as fixed
      text, not a choice**, so people
      entering rates know what clock the windows use. Wording: "All times are AEST (UTC+10),
      with no daylight saving. This is NEM (National Electricity Market) time, which is
      the clock the meter data and network tariffs use." The *Basics* step shows it, every
      step that takes times repeats it in its description, and the *Review* step labels its
      24-hour map with it. Strings must say it is a pricing clock, not Home Assistant's own
      timezone.
- [x] ~~Our cost statistic appears in the total-costs picker.~~ Confirmed by the user
      (the probe's AUD statistic is listed). HA's currency is confirmed `AUD`
      (`/api/config`, HA 2026.11.1), matching the statistic's unit. Cost statistics should
      still use the configured `hass.config.currency` rather than a hard-coded `AUD`, so
      other users' pickers keep listing them.
- [x] ~~Feed-in option.~~ **The user does not export** (no `B1` channel). Export is
      out of scope for this account: do not build or test the export UI/statistics against
      it. Keep the export model in `tariff.py` (generic, tested, and the sign convention is
      known) but make export strictly optional and hidden unless a `B1` channel is seen.
- [ ] GST on export: moot for this account. The default in `tariff.py` (import only; export
      paid as entered) stays; revisit only if an exporting user appears.

### 0.2.0b5: running totals that do not join (found live, 9 Nov 2026)

- [x] **Found:** the Off Peak cost statistic's running total dropped by $265.83 at 00:00 on
      9 Nov (the start of the poll's re-check window), with Peak, Solar Soak, the combined cost
      and every kWh statistic intact. The gap equalled 509 days of Off Peak supply share. Most
      likely the options-flow recalculation and the first poll after the reload wrote at once,
      the poll taking its starting total from before the recalculation's last statistic had
      landed. The poll never looked at the join at its window's start. Repaired by *Recalculate
      cost history*. The log held nothing from the integration, so the order is an inference.
- [x] **Fix:** per-NMI write lock (`chain_guard.py`), recorder wait before reads, read-back
      of the last row after writes (`async_block_till_done` alone is racy: it returns at once
      while the recorder is mid-task), and a join check in `_async_write_series` that heals a bad join on the next poll.
      Tests: `test_chain_*` in `test_ha_statistics.py` (two seeded breaks failed before the fix).
- [x] **Detector and trail:** after-write check, short-label warning, one Repairs notice per
      entry with a fix that reprices, and a last-20-writes trail in diagnostics. Local only.
- [ ] Not verified live: the Repairs fix on the user's HA, and `async_block_till_done` on HA
      2026.11 (tests ran on 2026.2.3, where it is what the recorder test helpers use).
- [ ] Not covered: a break in older history that no write touches (writes start only at a
      poll window or a chosen repair day, which are checked, so this should not arise).

- [x] **Refresh data button** (`button.py`, `tests/test_ha_button.py`): polls the portal on demand
      with `coordinator.async_refresh()`. Wanted for testing and as a recovery path after a failed
      refresh, so it is always available (also with the session dead). A failed poll raises
      `HomeAssistantError`: an ended session says to sign in again (the coordinator has already
      started reauth), other failures say why with any URL query stripped. A press while its own
      poll runs is ignored. **Verified live on 0.2.0b7 (16 Nov 2026):** the button is present on
      the device and a press polls successfully.

### 0.2.0b6: setup waited for the recorder (found live, 16 Nov 2026)

- [x] **Found:** on 0.2.0b5, `integration/setup_info` showed this integration's setup took 299.6 s
      (Home Assistant's 300 s limit), against 16 s for the next slowest, and held up the rest of
      startup. The writes added in 0.2.0b5 waited for the recorder to store their rows, but the
      recorder thread does not work its queue until Home Assistant has started
      (`Recorder._wait_startup_or_shutdown`), and Home Assistant cannot finish starting until
      setup returns.
- [x] **Fix:** `statistics.py` does not wait for the recorder, and does not read rows back to
      check them, until `hass.state` is running (`_recorder_running`); after startup a wait
      gives up after 20 s (`_SETTLE_TIMEOUT`). Before startup a write's trail entry has
      `verified: None`. Tests: `test_startup_writes_do_not_wait_for_the_recorder` (hangs to its
      timeout without the fix) and `test_a_recorder_that_never_settles_cannot_stall_a_write_forever`.
- [x] **Verified live:** after the fix, setup took 7.1 s. On 0.2.0b7 startup is still quick and
      there are no Repairs notices after a poll, so the join check also ran clean on a real poll.

### Quality scale self-audit (16 Nov 2026)

Graded against the Home Assistant Integration Quality Scale (54 rules). This is a **custom**
integration, and the scale's pages are written for core ones and never mention custom
integrations, so it is a self-audit: Home Assistant does not enforce it for
`custom_components`, but hassfest does read it once the manifest has a `quality_scale` key. The
manifest first said `"bronze"` and hassfest accepted the key and the file in a Gitea run on
16 Nov 2026 (hassfest and ruff + pytest passed; HACS is skipped on Gitea); it now says `"silver"`
(see Silver below). The result is kept in `custom_components/citipower_myenergy/quality_scale.yaml` (all 54 rules, status and a
reason each), and `tests/test_quality_scale.py` checks its structure (every rule once, valid
statuses, a reason for every exemption, Bronze with no todo).
- [x] **Manifest tier:** `"quality_scale": "bronze"` is set (hassfest key order: domain and name
      first, then alphabetical, so it sits after `issue_tracker` and before `requirements`;
      accepted values are bronze, silver, gold, platinum, internal, legacy, custom, no_score), and
      `codeowners` is filled in, which Silver and above require. hassfest validates
      `quality_scale.yaml` once the key is set (every slug present and spelled exactly, a valid
      status, a comment on every exempt rule). To claim Silver, change the key to `"silver"` in
      the same change that gets `test-coverage` to done, and run hassfest first.
      `tests/test_quality_scale.py` fails if the manifest claims a tier the audit does not
      support.

- [x] **Audit of 0.2.0b7:** 29 done, 12 exempt, 13 todo. No tier earned yet (a tier needs every
      rule of it and the tiers below done or exempt).
- [x] **Bronze blockers closed in 0.2.0b8:** `common-modules` (`entity.py`; a test pins unique ids
      and entity ids), `config-flow-test-coverage` (`config_flow.py` 79% to 100% of lines and
      branches, every error path with a recovery), `docs-removal-instructions` (README section).
- [x] **`brands`:** original images (a 24-hour tariff clock with a bolt) ship in
      `custom_components/citipower_myenergy/brand/`, following gheydon/ha-yvw (`icon`, `icon@2x`,
      `logo`, `logo@2x`, plus `dark_logo` variants). Home Assistant reads that folder directly
      (2026.3 onwards). **Verified live on 2026.11.1:** `/api/brands/integration/<domain>/icon.png`
      serves it exactly as it serves ha-yvw's, and it showed once the browser cache was cleared.
      Not confirmed: that this satisfies the rule for a custom integration. No SVG source is in
      the repo (the images came from a script).
- [x] **Silver, done 16 Nov 2026:** `integration-owner` (`codeowners` is `["@markesss"]`) and
      `parallel-updates` (`PARALLEL_UPDATES = 0` in `sensor.py` because the sensors only read held
      data, `1` in `button.py` because a press polls the portal; a test pins both).
- [x] **Silver `test-coverage`, done 16 Nov 2026:** every module is over 95% with branch coverage
      on (total 98%, 789 tests; lowest module `session_store.py` at 95.8%). Re-measured on HEAD
      first: `api.py` 50%, `aura.py` 55%, `auth.py` 77%, `exceptions.py` 82%, `models.py` 81%,
      `repairs.py` 88%, `portal.py` 84%, `nem12.py` 90%, `tariff_flow.py` 94.9% (it printed 95%).
      New: a `serve` fixture (a local aiohttp server) in `tests/conftest.py`,
      `test_ha_aura.py`, `test_ha_api.py`, `test_ha_repairs.py`, `test_portal_errors.py`,
      `test_models_edges.py`, and extensions to `test_ha_auth.py` and `test_ha_options_flow.py`.
      24 mutations of a scratch copy were tried and 23 caught (the survivor was an equivalent
      mutant). No coverage exclusions. As before, no real wrong-password or wrong-code portal
      response exists, so those tests prove control flow only.
- [x] **Coverage in CI:** the `tests` job in `.github/workflows/validate.yml` runs pytest with
      coverage, prints a per-module table (`scripts/coverage_summary.py`, also added to the
      GitHub job summary), fails if any module is under `MIN_COVERAGE_PERCENT` (95), and uploads
      the HTML and XML reports on GitHub only (upload-artifact v4 is not supported by Gitea).
      The manifest tier is `"silver"`; hassfest accepts the key and file (see above).
- [x] **Fixed (16 Nov 2026), found by the coverage work: a failed repair dropped the notice.** The
      Repairs fix flow cleared the guard's recorded failures (and so the notice) before
      repricing, so a failed repricing aborted as `repair_failed` with the notice already gone
      until a later check found the break again. Fixed test first: three new tests in
      `tests/test_ha_repairs.py` failed on the old code (`assert {} == {'cost_import...'}`), the
      pin test that asserted the bug was removed, and two success-path guards were added. The
      flow now snapshots `guard.broken`, resets as before, and calls the new
      `ChainGuard.restore(saved)` if the repricing raises (it keeps anything the failed attempt
      reported and keeps the notice's statistic and hour). Rejected: no up-front reset (a failure
      for a statistic that is no longer produced would never clear, and nothing clears while the
      recorder is not running) and reset-only-after-success (it would wipe a fresh report).
      Mutation-checked: removing `restore` fails exactly those three tests.
- [ ] **Found by the coverage work, not fixed (need a code change):** (2)
      `MyEnergyAuraClient.async_invoke` has an `if held is None: raise` branch that looks
      unreachable; (3) `async_send_code` keeps the verification id and registration flag after
      raising on `isRegistration`, so a later `async_verify_code` would still be sent (harmless:
      the config flow stops there).
- [ ] **Gold:** `docs-supported-devices` (a section on supported networks and meters),
      `entity-translations` (the latest-day sensor sets its name in Python),
      `exception-translations` (no `exceptions` section in `strings.json`; the button and the
      coordinator raise plain-text errors), `icon-translations` (no `icons.json`),
      `entity-disabled-by-default` (do NOT disable the session sensor: it is how an expired
      session is seen; record the rule as exempt with that reason).
- [ ] **Platinum:** `strict-typing` only (22 errors in 7 files on a lenient `mypy --strict`; no mypy
      config or `py.typed`). `async-dependency` and `inject-websession` are exempt (no library;
      `tls.async_create_session` is required for the missing TLS intermediate), the second being
      a judgement call.
- Exempt, with reasons: `action-setup`, `docs-actions`, `docs-conditions`, `docs-triggers` (no
  actions, conditions or triggers), `dependency-transparency` (no external requirements),
  `discovery`, `discovery-update-info` (cloud account), `dynamic-devices`, `stale-devices` (one
  device per entry), `reconfiguration-flow` (debatable: credentials go through reauth, the NMI is
  the entry's identity).

## Backlog / ideas

- [ ] Near-real-time data via an in-home device (researched 16 Nov 2026, not started). Powercor
      and CitiPower let a customer link a Zigbee Smart Energy device to the meter by form (MAC
      address and install code; "usually within 5 business days"; no cost, model list or data
      stated). The meter is the Zigbee coordinator and the device joins it as a client, so ZHA or
      Zigbee2MQTT on an ordinary coordinator is not expected to work (install codes, Smart Energy
      key establishment; no reports of success). Reported working on Victorian meters (United
      Energy, not CitiPower or Powercor) with a Rainforest Eagle 3 (HA core *Rainforest Eagle*
      integration) and an Emporia Vue Utility with ESPHome. It would be a separate integration
      that does not use the portal. First step: phone Powercor (13 24 12) or CitiPower (13 12 80)
      about the meter model, the fee and the report interval. Mostly second-hand forum reports.
- [ ] United Energy support: same portal, different username suffix (brand `UE`).
- [ ] First-time MFA enrolment (`verifyMFARegister`, `isRegistration: true`).
- [ ] Run the first backfill as a background task so setup doesn't wait ~30 s.
- [ ] Brand assets (icon/logo) for the HACS default store.
