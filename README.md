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

## Cost tracking

The portal has no prices (your retailer sets those), so you tell the integration your tariff and
it works out an **estimated** cost for every hour of your usage. These are estimates, not a bill.

**1. Enter your tariff.** *Settings → Devices & services → myEnergy → Configure → Electricity
tariff and costs → Set up a tariff.*

- **Flat rate:** one price for every hour.
- **Time of use:** add a period for each price, with the hours it applies. Hours are whole hours
  on a 24-hour clock, such as `16-21` (4pm to 9pm) or `9pm-11am`; an end at or before the start
  runs past midnight, and `0-24` is all day. Separate several windows with commas, and pick the
  days if it only applies on some. Every hour of the week has to belong to exactly one period;
  the review step shows the resolved day and tells you about any gap or overlap.
- Also enter the daily supply charge, whether your prices include GST, and any pay-on-time
  discount (taken off supply and usage charges only).
- Rates change, so add new rates **from a date** rather than editing old ones. Use *Recalculate
  cost history* to reprice days that are already recorded. Per-day allowances ("first 30 kWh a
  day free") are supported in the *Advanced: edit as YAML* step.

**Pick the clock your retailer's hours are on.** The *Tariff details* step has a *Pricing
clock* choice, and every step that takes hours repeats which clock you picked. Retailer fact
sheets only say "local time", so your bill is the authority.

| Choice | Clock |
|---|---|
| Melbourne, Sydney, Hobart, Canberra | AEST/AEDT, following daylight saving. On the two days a year the clocks change, a day has 23 or 25 hours. |
| Brisbane | AEST all year, no daylight saving. That is NEM (National Electricity Market) time, the clock your meter data uses. A tariff saved before this choice existed keeps using it until you change it. |
| Perth | AWST (UTC+8), no daylight saving. |
| GMT | UTC. |

GloBird, for example, prices its time-of-use periods on the wall clock with daylight saving, so on a
Victorian GloBird plan pick **Melbourne**, and check the first month's cost against a bill.
Adelaide, Darwin, Broken Hill, Eucla and Lord Howe are not offered: their clocks sit half an hour
off UTC and the statistics are hourly.

It is a pricing clock, not Home Assistant's own timezone. The Energy dashboard draws its days on
Home Assistant's timezone, so a different clock here can move a few cents across the edge of a day.
One clock applies to every set of rates you enter. Changing it moves every window and day edge, so
saving a changed clock offers to recalculate all recorded costs straight away.

**2. Use the cost in the Energy dashboard.** *Settings → Dashboards → Energy*, edit your grid
consumption source, and under cost choose **Use an entity tracking the total costs** and pick
**`<name> grid import cost`** (statistic `citipower_myenergy:<nmi>_e1_cost`). It already includes
the daily supply charge, so leave the dashboard's own daily cost adjustment at 0.

Nothing is written until you enter a tariff. Only whole finished days are priced, and only the
main import channel (`E1`) is costed; controlled load is not.

### Coloured bars: statistics per tariff period (optional)

The dashboard draws one source in one colour. To see each tariff period in its own colour, turn on
*Configure → Electricity tariff and costs → Statistics per tariff period*. Each import period then
also gets its own kWh and cost statistics (for example `…_e1_peak` and `…_e1_peak_cost`), and the
step lists them. Turning it on works out your whole stored history, which can take a minute.

Then, in the Energy dashboard:

1. Add one grid consumption source per period, each with that period's kWh statistic and, for
   cost, **Use an entity tracking the total costs** with the matching `_cost` statistic.
2. **Remove the combined grid source** (or turn off its cost). Using it as well counts your energy
   and cost twice.
3. **The daily supply charge is not applied.** The period costs leave it out, and the dashboard's
   own daily cost adjustment is not applied when you use time-of-use (per-period) billing meters
   like this. So the dashboard's grid cost is lower than your bill by the supply charge for each
   day (after any discount). The step shows that daily amount for reference, for example
   `AUD 0.89628`. Leave the dashboard's daily cost adjustment at 0 on every source.
4. Check one day: the dashboard's grid cost should equal the period costs, which is the combined
   cost statistic (`…_e1_cost`) minus that day's supply charge. To see the full cost including
   the supply charge, use the combined cost statistic instead of the per-period sources.

Renaming or removing a period stops its statistics being written but leaves the old ones in place;
clear them in *Developer Tools → Statistics* if you do not want them. Turning the option off keeps
everything already written.

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
