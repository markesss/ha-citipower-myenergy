# Architecture

## Module layers

```mermaid
flowchart TB
    subgraph ha["Home Assistant"]
        direction LR
        cf["config_flow.py"]
        co["coordinator.py"]
        se["sensor.py"]
        ini["__init__.py"]
        st["statistics.py"]
        di["diagnostics.py"]
        ss["session_store.py"]
        tls["tls.py"]
        tf["tariff_flow.py"]
    end
    subgraph io["aiohttp"]
        direction LR
        auth["auth.py<br/>MyEnergyLogin"]
        api["api.py<br/>MyEnergyApi"]
        aura["aura.py<br/>MyEnergyAuraClient, page/Aura helpers"]
    end
    subgraph pure["Pure Python (no I/O, no HA imports)"]
        direction LR
        portal["portal.py"]
        nem12["nem12.py"]
        models["models.py"]
        exc["exceptions.py"]
        const["const.py"]
        tariff["tariff.py"]
        tform["tariff_form.py"]
    end
    ha --> io
    io --> pure
```

`tls.py` creates every HTTP session the integration uses (config flow and runtime).
The portal omits its TLS intermediate certificate, so the session's trust store is
certifi plus the DigiCert intermediate in `certs/`; see *TLS* in
`PORTAL_PROTOCOL.md`. Do not use `async_create_clientsession` for portal traffic.
Sessions share one connector (and so one SSL context) but have separate cookie jars.

The bottom layer has no I/O and no HA imports, and is fully unit-tested against
real (anonymised) portal responses.

## Sign-in (config flow)

```mermaid
flowchart TD
    user["<b>user step</b><br/>email, password"]
    login["MyEnergyLogin.async_submit_credentials<br/>1. GET /s/login/ → Aura context (loginApp2, token 'null')<br/>2. POST /s/sfsites/aura communityLogin → frontdoor.jsp URL (one-time sid)<br/>3. GET frontdoor.jsp → script redirect → /apex/MFA_Enabler<br/>4. parse_mfa_page(): remoting signatures, options, view state, final form"]
    method["<b>method step</b><br/>email | sms<br/>(skipped if only email is offered)"]
    send["async_send_code<br/>POST /apexremote initiateMFA([method]) → verificationIdentifier"]
    code["<b>code step</b><br/>6 digits (or resend)"]
    verify["async_verify_code<br/>1. POST /apexremote verifyMFA([code, id, isRegistration, method])<br/>2. POST /MFA_Enabler (form + view state) → script redirect /s/<br/>async_finish: GET /s/energy-usage → Aura context + CSRF token cookie"]
    nmi["<b>NMI list</b><br/>getEnergyUsageDetailWrapper → nmi step if more than one<br/>retrieveAccountHolderInfo → company name"]
    entry["<b>entry data</b><br/>nmi, nmi ids, address, description, company, cookies, signed_in_at<br/>(no password, ever)"]
    user --> login --> method
    method --> send --> code
    code --> verify --> nmi --> entry
```

Reauth runs the same steps. It aborts with `wrong_account` if the login can't
see the entry's NMI.

## Runtime

```mermaid
flowchart TD
    subgraph setup["async_setup_entry"]
        s1["SessionStore → newest cookies + Aura context<br/>for this signed_in_at (else entry data)"]
        s2["MyEnergyAuraClient<br/>(dedicated aiohttp session, cookies, context, on_change → store)"]
        s3["MyEnergyCoordinator.first_refresh"]
        s4["start keep-alive"]
        s1 --> s2 --> s3 --> s4
    end
    subgraph poll["Every 4 h: _async_update_data"]
        p1["retrieveAccountHolderInfo(nmi)<br/>→ start/end dates, company"]
        p2["start = earliest (≤ 2 y, ≥ account.start) if any channel has no statistics<br/>else newest stored hour − 7 days"]
        p3["getMeterData in ≤ 90-day chunks<br/>→ NEM12 → channels (E1, B1, …)"]
        p4["async_insert_channel per channel<br/>→ external statistics (repair on change)"]
        p5["save session (cookies, context)<br/>and known channel suffixes"]
        p1 --> p2 --> p3 --> p4 --> p5
    end
    subgraph keep["Every ~45 min (×0.85–1.0 jitter): keep-alive"]
        k1{"request within<br/>80% of interval?"}
        k2["retrieveAccountHolderInfo(nmi)<br/>then read SessionTimeServlet (sr) for the sensor"]
        k3["MyEnergyAuthError<br/>session dead → stop loop → start reauth"]
        k4["other errors<br/>retry in 2 min"]
        k1 -- yes --> skip["skip"]
        k1 -- no --> k2
        k2 -- auth error --> k3
        k2 -- other error --> k4
    end
    setup --> poll
    setup --> keep
```

## Statistics

- One external statistic per NEM12 channel: `citipower_myenergy:<nmi>_<suffix>`,
  kWh, `has_sum`, no mean. Name: `<description> grid import|grid export`.
- 5-minute intervals are summed into hours in **AEST** (market time), and whole
  hours are the same boundaries in UTC.
- On every write, the stored window is compared with the portal's. From the
  first hour that differs (or is missing mid-history), everything is rewritten
  with the running sum recomputed from the hour before. This fixes estimates
  that are later replaced by actual readings.
- Hours that haven't finished yet are never written.

## Cost tracking

The portal carries no prices, so the user describes their tariff (options flow:
`tariff_flow.py`, with `tariff_form.py` building the text and form handling) and it is stored
in `entry.options["tariff"]` as `Tariff.to_dict()`. `tariff.py` is pure: a `Tariff` is a list
of versions by effective date; each has a supply charge, discount, GST flag and independent
import and export `Period` lists (windows in whole hours, per-day tiers). Windows and days
are read on the tariff's **pricing clock**, a version's `timezone`: Melbourne, Sydney, Hobart,
Canberra, Brisbane, Perth or GMT (only zones with whole-hour UTC offsets, because statistics are
hourly on UTC boundaries). No `timezone` means fixed AEST, the NEM clock and what v0.2.0b1/b2
did. A tariff has one clock for all its versions. `cost_for_day` prices one whole local day of
hourly kWh (23, 24 or 25 hours); the supply charge goes on the day's first hour, GST and the
discount apply to import only. NEM12 meter data is always parsed on AEST (`const.MARKET_TZ`).

```mermaid
flowchart LR
    kwh["stored kWh<br/>(…_e1)"] --> day["whole finished local days<br/>tariff.cost_for_day"]
    day --> cost["…_e1_cost<br/>(currency, has_sum)<br/>includes the supply charge"]
    day --> byp["cost_for_day_by_period<br/>(opt-in)"]
    byp --> per["…_e1_&lt;slug&gt; kWh and<br/>…_e1_&lt;slug&gt;_cost per period<br/>(supply charge per the supply mode)"]
```

- Costs are written a **whole finished day** at a time and repaired with the same rule as kWh:
  if a stored day's cost differs from what the tariff and readings now give, everything from
  that day on is rewritten with the running total recalculated. A re-run writes nothing.
  `async_recalculate_costs` reprices older history from the stored kWh.
- **Changing the clock** moves every window and day edge, so every stored cost disagrees from the
  first affected day and is rewritten. The options flow asks first and runs a recalculation from
  before any stored reading, using the new tariff, then saves. A rewrite ends at the last complete
  day on the new clock; an hour just beyond it that was priced on the old clock keeps its price
  but is re-chained (`_async_keep_the_tail`) so no running total points backwards, and the next
  complete day replaces it.
- **Per-period statistics** are optional (`period_statistics` option). Each import period gets
  a kWh and a cost statistic, so the Energy dashboard can draw each as its own coloured grid
  source. The period set is the union of all versions' period names, keyed by `period_slugs`;
  every statistic has a row for every hour (zero outside its period). All of them are repaired
  together, so they can never disagree. Home Assistant's fixed daily cost adjustment is not
  applied to per-period (time-of-use) sources, so where the supply charge appears is the user's
  choice, the `supply_in_periods` option (`const.SUPPLY_MODES`), read with `tariff.supply_mode_of`
  (absent or unknown means `none`) and passed to `cost_for_day_by_period(..., supply_mode)`:
  `none` (default) leaves it out of the period costs; `spread` adds `daily_supply_dollars` divided
  by the hours the local day really has (23, 24 or 25) to every hour; `first_hour` adds all of it to
  the day's first hour, the hour the combined cost carries it on. With the last two the periods
  add up to the combined cost for a whole day. A single-period (flat) version always behaves as
  `none`. The mode only changes cost rows; the options flow asks for it (and offers a
  recalculation, `async_recalculate_costs(..., supply_mode=)`) only for a time-of-use tariff with
  the per-period option on. Users must not also use the combined cost source alongside the period
  sources. Renamed or removed periods leave their old statistics behind, unwritten.
- Turning the option on backfills the whole stored history on the next poll.

### Live tariff sensors

Three sensors (`sensor.py`) follow the tariff's clock, so automations can react to the period
and act before an expensive one: **Current tariff period** (an enum of the period names, with
rates and the next period as attributes), **Current tariff rate** (`<currency>/kWh`, GST
included, before the discount) and **Next tariff period starts** (a timestamp). They exist only
when a valid tariff is saved, and they are not polled and never touch the recorder.

`tariff.py` supplies the pure lookups: `period_at` (the period at an aware moment, on the
tariff's clock, using the version in force that local day), `rate_at` (that period's headline
and discounted rate) and `next_period_change`. The last walks hour boundaries for at most
`SCAN_HOURS` (8 days) until the period's name or rates differ, so adjacent hours of one period,
including a stretch that crosses midnight, are a single stretch, and a version that only
changes the supply charge is not a change. `TariffTimeline` in `sensor.py` holds the answer as
of the last boundary and one `async_track_point_in_utc_time` timer for the next; each fire
recomputes, reschedules and rewrites the entities. When nothing changes within the scan (a flat
tariff, or the next version is further away) it looks again after `SCAN_HOURS - 24` hours so a
later version is still seen about a day before it starts. Saving the tariff reloads the entry
(the options listener), which rebuilds the timeline; unloading cancels the timer.

## Session persistence

`session_store.py` keeps `{entry_id: {signed_in_at, cookies, context, suffixes}}`
in `.storage/citipower_myenergy.session`. Cookies and context are used only if
`signed_in_at` matches the entry's. A reauth writes a new `signed_in_at`, so a
dead session is never resumed. `__Host-ERIC_*` (the CSRF token cookie) and
analytics cookies aren't stored. The token is re-read from the jar after each
page load.
