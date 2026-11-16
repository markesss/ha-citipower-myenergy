# Writing and backing up a tariff

The guided steps in *Configure → Electricity tariff and costs* cover flat and time-of-use
tariffs. The **Advanced: edit as YAML** step shows the whole tariff as YAML and takes it back, so
you can build one by hand, copy one between installs, keep a copy in a file, or use features the
guided steps leave out (per-day allowances, several rate versions).

Every example below is parsed by the test suite (`tests/test_tariff_docs.py`), so they stay valid.
They are examples of the *shape*: check the numbers against your own retailer's fact sheet.

## Creating one

1. Set up a tariff in the guided steps first, or open the advanced step with none set to get a
   flat-rate starting point.
2. Open **Advanced: edit as YAML**, replace the contents with your tariff, and save. The tariff is
   checked on save; an error says what is wrong.
3. Run **Recalculate cost history** so days already recorded are priced with it.

### Time of use

An example with three periods, one crossing midnight, priced on the Melbourne clock. The rates
are cents per kWh including GST, as printed on Victorian fact sheets.

```yaml
schema: 1
versions:
  - effective_from: "2026-09-30"
    timezone: Australia/Melbourne
    prices_include_gst: true
    supply_c_per_day: 92.40
    discount_pct: 3
    import_periods:
      - name: Peak
        windows: [{start: 16, end: 21}]
        tiers: [{up_to_kwh_per_day: null, c_per_kwh: 31.46}]
      - name: Solar Soak
        windows: [{start: 11, end: 16}]
        tiers: [{up_to_kwh_per_day: null, c_per_kwh: 13.20}]
      - name: Off Peak
        windows: [{start: 21, end: 11}]   # an end at or before the start runs past midnight
        tiers: [{up_to_kwh_per_day: null, c_per_kwh: 16.50}]
```

### A per-day allowance (tiers)

A tier applies up to a number of kWh **per day** within the period, then the next tier takes
over. The last tier has `up_to_kwh_per_day: null`. This is an illustrative plan, not a real one.
`catch_all: true` makes a period take every hour no other period claims, so it needs no windows.

```yaml
schema: 1
versions:
  - effective_from: "2026-07-01"
    timezone: Australia/Sydney
    supply_c_per_day: 110.00
    import_periods:
      - name: Free Hours
        windows: [{start: 11, end: 15}]
        tiers:
          - {up_to_kwh_per_day: 30, c_per_kwh: 0}
          - {up_to_kwh_per_day: null, c_per_kwh: 11.00}
      - name: Everything Else
        catch_all: true
        tiers: [{up_to_kwh_per_day: null, c_per_kwh: 30.00}]
```

### Weekdays only, and rate changes

`days` limits a window to some days, with Monday as 0 and Sunday as 6. To change rates from a
date, **add** a second entry to `versions` with a later `effective_from` and leave the old one in
place, so history keeps being priced at the rates in force then.

```yaml
schema: 1
versions:
  - effective_from: "2026-01-01"
    timezone: Australia/Brisbane
    supply_c_per_day: 100.00
    import_periods:
      - name: Weekday Peak
        windows: [{start: 15, end: 21, days: [0, 1, 2, 3, 4]}]
        tiers: [{up_to_kwh_per_day: null, c_per_kwh: 40.00}]
      - name: Other
        catch_all: true
        tiers: [{up_to_kwh_per_day: null, c_per_kwh: 22.00}]
  - effective_from: "2026-07-01"
    timezone: Australia/Brisbane
    supply_c_per_day: 105.00
    import_periods:
      - name: Weekday Peak
        windows: [{start: 15, end: 21, days: [0, 1, 2, 3, 4]}]
        tiers: [{up_to_kwh_per_day: null, c_per_kwh: 42.00}]
      - name: Other
        catch_all: true
        tiers: [{up_to_kwh_per_day: null, c_per_kwh: 23.00}]
```

## Field reference

| Key | Meaning |
|---|---|
| `schema` | Always `1`. |
| `versions` | One entry per set of rates, in any order; they are sorted by date. Dates must differ. |
| `effective_from` | `YYYY-MM-DD`. The first version also prices every earlier day. |
| `timezone` | The retailer's clock: `Australia/Melbourne`, `Sydney`, `Hobart`, `Canberra`, `Brisbane`, `Perth` or `GMT`. Left out means fixed AEST. Every version must use the same one. |
| `prices_include_gst` | `true` if your rates already include GST (the default). |
| `supply_c_per_day` | Daily supply charge in cents, before discount. |
| `discount_pct` | Pay-on-time style discount, taken off supply and usage but not export credit. |
| `import_periods` | The prices for energy you use. |
| `export_periods` | The same shape, for energy sent to the grid (only used if your meter has an export channel). |
| `name` | A label. It also becomes the period's statistic name when per-period statistics are on, so renaming a period starts new statistics. |
| `windows` | Whole hours: `start` 0-23 and `end` 0-24. An `end` at or before the `start` runs past midnight, `start` and `end` cannot be equal, and `0` to `24` is all day. Optional `days`. |
| `catch_all` | Takes every hour no other period claims. At most one per list, and it has no windows. |
| `tiers` | `c_per_kwh`, with `up_to_kwh_per_day` for an allowance (`null` for the last). |

Rules the checker enforces: every hour of the week belongs to exactly one period (a gap or an
overlap is an error, and the message names the hour), hours are whole numbers, period names
are unique, every period has at least one rate, tier thresholds increase, and only the last tier
is open-ended. Half-hour windows are not supported because the statistics are hourly.

## Backing up

- **Home Assistant backups include it.** The tariff is stored in the integration's options inside
  the config entry, so a normal Home Assistant backup and restore brings it back.
- **A copy you can keep.** Open **Advanced: edit as YAML**, copy the text into a file, and store it
  with your other notes. To restore on this or another install, set up the integration, open the
  same step and paste it in.
- **Costs are rebuilt, not backed up.** The cost statistics come from your stored readings and the
  tariff, so after restoring a tariff run **Recalculate cost history** and they are regenerated.
- **No secrets are in it.** The tariff holds prices and hours only. Sign-in details are stored
  separately and are not part of this YAML.
