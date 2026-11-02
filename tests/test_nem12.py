"""Tests for the NEM12 parser and the response models."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from custom_components.citipower_myenergy.const import MARKET_TZ
from custom_components.citipower_myenergy.models import (
    date_chunks,
    parse_account_info,
    parse_meter_data,
    parse_nmi_list,
)
from custom_components.citipower_myenergy.nem12 import (
    daily_total,
    merge_channels,
    parse_nem12,
)
from custom_components.citipower_myenergy.portal import extract_return_value


def _meter_data(fixture_json):
    return parse_meter_data(extract_return_value(fixture_json("meter_data.json")))


def test_meter_data_fixture(fixture_json):
    channels = _meter_data(fixture_json)
    assert list(channels) == ["E1"]
    e1 = channels["E1"]
    assert e1.nmi == "6102000000"
    assert e1.interval_minutes == 5
    assert e1.unit == "KWH"
    assert not e1.is_export
    assert sorted(e1.days) == [date(2026, 10, d) for d in range(27, 32)]
    assert len(e1.hours) == 5 * 24
    # Hours are in market time and aligned to the hour.
    first = min(e1.hours)
    assert first == datetime(2026, 10, 27, 0, 0, tzinfo=MARKET_TZ)
    assert all(hour.minute == 0 for hour in e1.hours)
    assert daily_total(e1, date(2026, 10, 31)) == 14.044
    assert e1.updated[date(2026, 10, 31)].date() == date(2026, 11, 1)


def test_hourly_buckets_sum_twelve_intervals():
    values = ["0.1"] * 12 + ["0.2"] * 12 + ["0"] * (288 - 24)
    text = "\n".join(
        [
            "100,NEM12,202611010000,X,Y",
            "200,6102000000,E1B1,E1,E1,,M1,KWH,5,",
            "300,20261101," + ",".join(values) + ",A,,,20261102010000,",
            "900",
        ]
    )
    e1 = parse_nem12(text)["E1"]
    midnight = datetime(2026, 11, 1, tzinfo=MARKET_TZ)
    assert e1.hours[midnight] == 1.2
    assert e1.hours[midnight + timedelta(hours=1)] == 2.4
    assert daily_total(e1, date(2026, 11, 1)) == 3.6


def test_export_channel_wh_and_30_minute_intervals():
    text = "\n".join(
        [
            "100,NEM12,202611010000,X,Y",
            "200,6102000000,E1B1,B1,B1,,M1,WH,30,",
            "300,20261101," + ",".join(["500"] * 48) + ",A,,,,",
            "900",
        ]
    )
    b1 = parse_nem12(text)["B1"]
    assert b1.is_export
    assert b1.hours[datetime(2026, 11, 1, 5, tzinfo=MARKET_TZ)] == 1.0
    assert daily_total(b1, date(2026, 11, 1)) == 24.0


def test_variable_quality_400_records():
    text = "\n".join(
        [
            "200,6102000000,E1,E1,E1,,M1,KWH,30,",
            "300,20261101," + ",".join(["1"] * 48) + ",V,,,,",
            "400,1,2,E52,,",
            "400,3,48,A,,",
            "900",
        ]
    )
    e1 = parse_nem12(text)["E1"]
    midnight = datetime(2026, 11, 1, tzinfo=MARKET_TZ)
    assert e1.quality[midnight] == "E"
    assert e1.quality[midnight + timedelta(hours=1)] == "A"


def test_merge_later_wins():
    def file(value: str) -> str:
        return "\n".join(
            [
                "200,6102000000,E1,E1,E1,,M1,KWH,30,",
                "300,20261101," + ",".join([value] * 48) + ",A,,,,",
            ]
        )

    merged = parse_nem12(file("1"))
    merge_channels(merged, parse_nem12(file("2")))
    assert daily_total(merged["E1"], date(2026, 11, 1)) == 96.0


def test_leading_spaces_and_blank_lines():
    text = (
        " 200,6102000000,E1,E1,E1,,M1,KWH,30,\r\n\r\n 300,20261101,"
        + ",".join(["1"] * 48)
        + ",A,,,,\r\n"
    )
    assert daily_total(parse_nem12(text)["E1"], date(2026, 11, 1)) == 48.0


def test_nmi_list(fixture_json):
    nmis = parse_nmi_list(extract_return_value(fixture_json("nmi_assignments.json")))
    assert len(nmis) == 1
    nmi = nmis[0]
    assert nmi.nmi == "61020000001"
    assert nmi.description == "Home"
    assert nmi.active
    assert nmi.assign_id and nmi.nmi_id
    assert "61020000001" in nmi.label


def test_account_info(fixture_json):
    info = parse_account_info(extract_return_value(fixture_json("account_info.json")))
    assert info.company == "Citipower"
    assert info.start == date(2025, 6, 18)
    assert info.end == date(2026, 10, 31)
    assert info.has_access


def test_date_chunks_cover_range_without_overlap():
    chunks = date_chunks(date(2025, 6, 18), date(2026, 10, 31), days=90)
    assert chunks[0][1] == date(2026, 10, 31)
    assert chunks[-1][0] == date(2025, 6, 18)
    covered = set()
    for start, end in chunks:
        assert (end - start).days < 90
        day = start
        while day <= end:
            assert day not in covered
            covered.add(day)
            day += timedelta(days=1)
    assert len(covered) == (date(2026, 10, 31) - date(2025, 6, 18)).days + 1


def test_single_day_chunk():
    assert date_chunks(date(2026, 1, 1), date(2026, 1, 1)) == [(date(2026, 1, 1), date(2026, 1, 1))]
