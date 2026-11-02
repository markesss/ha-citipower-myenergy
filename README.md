# CitiPower & Powercor myEnergy for Home Assistant

Imports your smart meter's interval data from the CitiPower/Powercor
[myEnergy](https://myenergy.powercor.com.au) portal into Home Assistant's
long-term statistics, so it shows on the **Energy dashboard**, including the
up to two years of history the portal already holds.

- 5-minute meter readings, rolled up into hourly statistics
- Grid import (`E1`), plus grid export (`B1`) and any other channels your meter reports
- Full history import on first setup, then a rolling re-check of the last week
  so estimated readings that are later replaced with actual ones are corrected
- Signs in the way the portal does: password, then a code by **email or SMS**
- The password is used once to sign in and is **never stored**

> The portal publishes each day's readings the following morning (about 1am),
> so this is yesterday's data, not live data. For real-time readings, see
> CitiPower/Powercor's Zigbee in-home display program.

## Install

1. HACS → ⋮ → **Custom repositories** → add this repository as an *Integration*.
2. Install **CitiPower & Powercor myEnergy**, then restart Home Assistant.
3. **Settings → Devices & services → Add integration → CitiPower & Powercor myEnergy**.
4. Enter your myEnergy email and password, choose email or SMS, and enter the
   6-digit code.

The first import of history takes about half a minute.

## Energy dashboard

**Settings → Dashboards → Energy → Electricity grid → Add consumption** and pick
**`<name> grid import`** (statistic `citipower_myenergy:<nmi>_e1`). If you have
solar, add **`<name> grid export`** (`citipower_myenergy:<nmi>_b1`) under
*Return to grid*.

## Sessions and re-signing in

myEnergy requires a verification code at every sign-in, so the integration
keeps one session alive rather than signing in again. Sessions end after two
hours idle, so it makes a small request every 45 minutes (configurable under
*Configure*, 5–55 min). Salesforce only extends a session when a request
arrives in the second half of its lifetime, so keep the interval under 60.

If the session does end (the portal logs it out, Home Assistant is off for
more than two hours, or you sign out everywhere), the **Session** sensor
shows *Expired* and Home Assistant asks you to sign in again. Nothing is lost:
the next import fills in every hour missed.

## Entities

| Entity | What it shows |
|---|---|
| Latest data | When the newest reading ends |
| Latest day grid import / export | Total for the latest complete day |
| Session (diagnostic) | `active` or `expired`, with the portal's reported time remaining |

## Troubleshooting

```yaml
logger:
  logs:
    custom_components.citipower_myenergy: debug
```

Debug logs never include the password, verification codes or session ids
(URLs are logged with every query value masked). Diagnostics downloads redact
the NMI, address and cookies.

## How it works

myEnergy is a Salesforce Experience Cloud site. Sign-in is
`LightningLoginFormController.communityLogin` (the email gets a `.VPN` suffix,
as the portal's own login form adds), then `frontdoor.jsp`, then the
`MFA_Enabler` Visualforce page: `MFAEnablerController.initiateMFA` sends the
code and `verifyMFA` checks it. Data comes from
`EnergyInformationController.getMeterData` in *Customer Access NEM12* format,
which is parsed locally. NEM12 times are market time (AEST, no daylight
saving), so summer hours land correctly.

The session handling is adapted from
[gheydon/ha-yvw](https://github.com/gheydon/ha-yvw) (MIT), which solves the same
problem for Yarra Valley Water's portal.

## Limitations

- This is not an official API. Portal changes can break it.
- United Energy uses the same portal with a different username suffix; it is
  not supported yet.
- Accounts that have never completed MFA setup in a browser need to sign in once
  at myenergy.powercor.com.au first.

## Development

See [`CLAUDE.md`](CLAUDE.md), [`PLAN.md`](PLAN.md) and [`docs/`](docs/) for architecture, the
portal protocol and the test plan.


```bash
pip install -r requirements-dev.txt
pytest -q
```

The parser tests run against anonymised responses captured from the portal
(`tests/fixtures`). Without Home Assistant installed, the `test_ha_*` tests are
skipped and the parser tests still run.
