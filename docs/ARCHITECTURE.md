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

## Session persistence

`session_store.py` keeps `{entry_id: {signed_in_at, cookies, context, suffixes}}`
in `.storage/citipower_myenergy.session`. Cookies and context are used only if
`signed_in_at` matches the entry's. A reauth writes a new `signed_in_at`, so a
dead session is never resumed. `__Host-ERIC_*` (the CSRF token cookie) and
analytics cookies aren't stored. The token is re-read from the jar after each
page load.
