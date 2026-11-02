# Testing

## Automated

```bash
pip install -r requirements-dev.txt
pytest -q
```

| file | needs HA | covers |
|---|---|---|
| `tests/test_portal.py` | no | Aura/remoting parsing, MFA page, redirects, URL masking |
| `tests/test_nem12.py` | no | NEM12 parsing, hourly roll-up, units, 400 records, models, chunking |
| `tests/test_ha_config_flow.py` | yes | full flow, bad password, bad code + retry, resend, NMI choice, reauth |
| `tests/test_ha_statistics.py` | yes | first import, append-only, repair on change, separate export stat |
| `tests/test_ha_tls.py` | yes | SSL context trusts the shipped intermediate and still verifies; sessions share a connector, not cookies |

Without `pytest-homeassistant-custom-component` installed, `test_ha_*` are skipped
(see `tests/conftest.py`).

## Fixtures

`tests/fixtures/` holds real portal responses with personal data replaced:

| fixture | from |
|---|---|
| `login_response.json` | `communityLogin` success (`sid` → `REDACTED_SID`) |
| `frontdoor.html` | `frontdoor.jsp` script redirect |
| `mfa_enabler.html` | MFA page (csrf/authorization/view state → `REDACTED`, mobile → `04** *** 000`) |
| `initiate_mfa.json`, `verify_mfa.json` | remoting results |
| `mfa_post.html` | final form post response |
| `community_page.html` | signed-in page trimmed to its descriptor tags and `eikoocnekot` |
| `nmi_assignments.json` | NMI list (contact/user blocks removed) |
| `account_info.json` | account details |
| `meter_data.json` | 5 days of NEM12 |
| `session_time.txt` | `SessionTimeServlet` |

Substitutions: NMI `61026…` → `61020000001`, email → `user@example.com`,
address → `1 EXAMPLE STREET MELBOURNE VIC 3000`, Salesforce user/contact ids → zeros.

## Capturing new portal traffic

You'll need this when a live test fails or the portal changes.

1. Browser devtools → Network → tick **Preserve log**, start from a signed-out tab.
2. Do the action, then **Export HAR**. The HAR contains your **plaintext
   password**, a live session id, your name, mobile, address and NMI.
3. Keep the HAR outside the repo. Extract only the response you need with a
   script, scrub it (see the substitutions above, plus any `sid=`, `csrf`,
   `authorization`, ViewState values), and save it as a fixture.
4. Before committing, grep the repo for your password, NMI, email, surname,
   street and mobile number. Then delete the HAR.

## Live test procedure

See `PLAN.md` Phases 3–5 for the checklist. Setup:

```yaml
# configuration.yaml
logger:
  default: warning
  logs:
    custom_components.citipower_myenergy: debug
```

If the config flow says "Could not reach myEnergy", look for the warning
`Could not reach myEnergy: …` in the HA log. It carries the transport error. A
`CERTIFICATE_VERIFY_FAILED` means the shipped intermediate no longer matches the portal's
chain (see *TLS* in `PORTAL_PROTOCOL.md`). Reading the log without UI access: the core log is at
`/api/hassio/core/logs` on a Supervisor install.

Things to watch in the log during sign-in:

- `Sign-in hop N landed on …/apex/MFA_Enabler`: the password worked.
- `MFA form post landed on …`: the code was accepted.
- `Following a client-side redirect to /s/`.
- `Importing myEnergy history for … from … to …` then `Adding N hours to citipower_myenergy:…_e1`.
- Keep-alive: `Keep-alive ok; portal reports Ns left`.

Useful HA checks:

- **Developer tools → Statistics**: search `citipower_myenergy`.
- **Settings → Devices → myEnergy …**: the Session sensor's attributes show
  `seconds_remaining`, `last_keepalive`, `signed_in_at`.
- **Download diagnostics** on the entry (redacted).

To compare with the portal, open myEnergy → Energy usage → daily view for the
same date. The *Latest day grid import* sensor should equal that day's total.
