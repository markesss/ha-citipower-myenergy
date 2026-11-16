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
  day free") are supported in the *Advanced: edit as YAML* step. Worked YAML examples, the field
  reference and how to back up a tariff are in [`docs/TARIFF_YAML.md`](docs/TARIFF_YAML.md).

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
3. **Choose where the daily supply charge goes** (below). The dashboard's own daily cost
   adjustment is not applied to time-of-use (per-period) sources like these, so leave it at 0 on
   every source, and let the period costs carry the supply charge if you want it shown.
4. Check one day: with the supply charge in the period costs, the dashboard's grid cost should
   equal that day's combined cost statistic (`…_e1_cost`), and your bill. Without it, the grid
   cost is lower than your bill by the supply charge for the day (after any discount).

**Supply charge in period costs.** When you turn the per-period statistics on with a
time-of-use tariff, *Configure → Electricity tariff and costs → Statistics per tariff period*
asks where the daily supply charge goes. The step shows the daily amount, for example
`AUD 0.89628`. There is no separate menu item for it: it is the screen that follows pressing
**Submit** on *Statistics per tariff period* with the switch on. To change the choice later, open
that same screen, leave the switch on, and submit.

| Choice | What the period costs hold | Use it when |
|---|---|---|
| **Left out** (default) | Usage times rate only. Add up the periods and you are short of your bill by one supply charge a day. | You only want to compare the periods, and use the combined cost for the bill figure. |
| **Spread equally over every hour of the day** | Usage plus a share of the supply charge: one 24th each hour (a 23rd or 25th on the days the clocks change). The periods add up to the combined cost, and the hourly chart stays smooth. | You want the dashboard total to match your bill. |
| **All on the first hour of the day** | Usage, plus the whole supply charge on the day's first hour, in whichever period covers midnight. The periods add up to the combined cost, with a step at that hour. | You want the total to match, and a charge that lands once a day. |

With either of the last two, a period's cost divided by its kWh reads above its tariff rate,
because it includes part of the supply charge. Do not read the dashboard as a rate check. The
combined `…_e1_cost` statistic is the same in all three, so the cost for the whole day is never
double counted if you use it on its own. A flat rate has one period, so it has no choice to
make and stays on *Left out*; the dashboard applies its own daily adjustment to it as usual.
Changing the choice offers to rewrite the recorded period costs straight away (ticked by
default); older days keep their old costs until you do, or until you run *Recalculate cost
history*.

Renaming or removing a period stops its statistics being written but leaves the old ones in place;
clear them in *Developer Tools → Statistics* if you do not want them. Turning the option off keeps
everything already written.

### Sensors for automations

Once a tariff is saved, three sensors follow its clock, so an automation can react when a period
starts or act shortly before an expensive one. They are not polled: each updates exactly when the
period changes, on the tariff's pricing clock (so daylight saving is handled).

| Sensor | State | Attributes |
|---|---|---|
| Current tariff period | The period's name, such as `Peak` | `rate_c_per_kwh`, `rate_after_discount_c_per_kwh`, `tiered`, `period_slug`, `next_period`, `next_rate_c_per_kwh`, `next_period_starts` |
| Current tariff rate | The rate in `<currency>/kWh`, such as `0.165` | `period`, `rate_after_discount` |
| Next tariff period starts | A timestamp | `next_period` |

The rate includes GST and is **before** any pay-on-time discount (the discounted figure is an
attribute). A period with a per-day allowance shows its first rate and `tiered: true`. "Next" means
the next *different* period, so an Off Peak that runs from 9pm to 11am is one stretch, not two. A
flat tariff never changes, so its next-period sensor is unknown.

These give the rate and period, not a live cost: the portal's readings arrive a day late, so cost
statistics are for yesterday and earlier.

Entity ids come from your device name; yours may differ from the `myenergy_home` used here.

```yaml
automation:
  # When Peak starts
  - alias: Pause the pool pump at Peak
    triggers:
      - trigger: state
        entity_id: sensor.myenergy_home_current_tariff_period
        to: "Peak"
    actions:
      - action: switch.turn_off
        target: {entity_id: switch.pool_pump}

  # Ten minutes before Peak starts
  - alias: Pre-cool before Peak
    triggers:
      - trigger: time
        at: sensor.myenergy_home_next_tariff_period_starts
        offset: "-00:10:00"
    conditions:
      - condition: state
        entity_id: sensor.myenergy_home_current_tariff_period
        attribute: next_period
        state: "Peak"
    actions:
      - action: climate.set_temperature
        target: {entity_id: climate.living_room}
        data: {temperature: 21}
```

If you want minutes until the next period (as a template sensor of your own):

```yaml
template:
  - sensor:
      - name: "Minutes until next tariff period"
        unit_of_measurement: min
        state: >
          {{ ((states('sensor.myenergy_home_next_tariff_period_starts') | as_datetime - now())
             .total_seconds() / 60) | round(0) }}
```

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
| Current tariff period / rate, Next tariff period starts | Only when a tariff is set up. See [Sensors for automations](#sensors-for-automations) |

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
