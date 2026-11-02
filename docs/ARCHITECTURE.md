# Architecture

## Module layers

```
                ┌──────────────────────────────────────────────────┐
 Home Assistant │ config_flow.py   coordinator.py   sensor.py      │
                │ __init__.py      statistics.py    diagnostics.py │
                │ session_store.py   tls.py                        │
                └───────────────┬──────────────────────────────────┘
                                │
 aiohttp        ┌───────────────▼──────────────────────────────────┐
                │ auth.py (MyEnergyLogin)    api.py (MyEnergyApi)  │
                │ aura.py (MyEnergyAuraClient, page/Aura helpers)  │
                └───────────────┬──────────────────────────────────┘
                                │
 pure Python    ┌───────────────▼──────────────────────────────────┐
                │ portal.py   nem12.py   models.py   exceptions.py │
                │ const.py                                         │
                └──────────────────────────────────────────────────┘
```

`tls.py` creates every HTTP session the integration uses (config flow and runtime).
The portal omits its TLS intermediate certificate, so the session's trust store is
certifi plus the DigiCert intermediate in `certs/`; see *TLS* in
`PORTAL_PROTOCOL.md`. Do not use `async_create_clientsession` for portal traffic.
Sessions share one connector (and so one SSL context) but have separate cookie jars.

The bottom layer has no I/O and no HA imports, and is fully unit-tested against
real (anonymised) portal responses.

## Sign-in (config flow)

```
user step ── email, password
   │  MyEnergyLogin.async_submit_credentials
   │    GET  /s/login/                       → Aura context (loginApp2, token "null")
   │    POST /s/sfsites/aura communityLogin  → frontdoor.jsp URL (one-time sid)
   │    GET  frontdoor.jsp → script redirect → /apex/MFA_Enabler
   │    parse_mfa_page(): remoting signatures, options, view state, final form
   ▼
method step ── email | sms   (skipped if only email is offered)
   │  async_send_code → POST /apexremote initiateMFA([method]) → verificationIdentifier
   ▼
code step ── 6 digits (or resend)
   │  async_verify_code → POST /apexremote verifyMFA([code, id, isRegistration, method])
   │                    → POST /MFA_Enabler (form + view state) → script redirect /s/
   │  async_finish      → GET /s/energy-usage → Aura context + CSRF token cookie
   ▼
NMI list (getEnergyUsageDetailWrapper) → nmi step if more than one
   │  retrieveAccountHolderInfo → company name
   ▼
entry data: nmi, nmi ids, address, description, company, cookies, signed_in_at
(no password, ever)
```

Reauth runs the same steps. It aborts with `wrong_account` if the login can't
see the entry's NMI.

## Runtime

```
async_setup_entry
  SessionStore → newest cookies + Aura context for this signed_in_at (else entry data)
  MyEnergyAuraClient(dedicated aiohttp session, cookies, context, on_change→store)
  MyEnergyCoordinator.first_refresh
  start keep-alive

every 4 h: _async_update_data
  retrieveAccountHolderInfo(nmi)        → start/end dates, company
  start = earliest (≤2 y, ≥ account.start) if any channel has no statistics
          else newest stored hour − 7 days
  getMeterData in ≤90-day chunks        → NEM12 → channels (E1, B1, …)
  async_insert_channel per channel      → external statistics (repair on change)
  save session (cookies, context) and known channel suffixes

every ~45 min (×0.85–1.0 jitter): keep-alive
  skipped if a request happened within 80% of the interval
  retrieveAccountHolderInfo(nmi); then read SessionTimeServlet (sr) for the sensor
  MyEnergyAuthError → session dead → stop loop → start reauth
  other errors      → retry in 2 min
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

## Session persistence

`session_store.py` keeps `{entry_id: {signed_in_at, cookies, context, suffixes}}`
in `.storage/citipower_myenergy.session`. Cookies and context are used only if
`signed_in_at` matches the entry's. A reauth writes a new `signed_in_at`, so a
dead session is never resumed. `__Host-ERIC_*` (the CSRF token cookie) and
analytics cookies aren't stored. The token is re-read from the jar after each
page load.
