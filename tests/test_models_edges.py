"""Edge cases for the response models and the NEM12 parser that the captures do not reach.

The captures (``tests/test_nem12.py``) are well-formed. These feed the parsers the damaged and
partial shapes a portal change or a bad reading could produce, and pin what each one does.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from custom_components.citipower_myenergy.const import MARKET_TZ
from custom_components.citipower_myenergy.exceptions import MyEnergyApiError
from custom_components.citipower_myenergy.models import (
    NmiInfo,
    date_chunks,
    parse_account_info,
    parse_meter_data,
    parse_nmi_list,
)
from custom_components.citipower_myenergy.nem12 import (
    Channel,
    daily_total,
    merge_channels,
    parse_nem12,
)

# --- NMI list ---------------------------------------------------------------------------------


def _assignments(*items: dict) -> dict:
    return {"data": {"nmiAssignmentsForEnergyUsage": list(items)}, "isSuccess": True}


def test_an_nmi_list_that_is_not_an_object_is_an_api_error() -> None:
    with pytest.raises(MyEnergyApiError, match="Unexpected NMI list"):
        parse_nmi_list({"data": ["unexpected"], "isSuccess": True})


def test_assignments_without_an_nmi_name_are_skipped() -> None:
    found = parse_nmi_list(
        _assignments(
            {"Id": "a1", "NMI__r": {"Id": "n1"}},  # no Name
            {"Id": "a2"},  # no NMI at all
            {"Id": "a3", "NMI__r": {"Name": "61020000001"}},
        )
    )

    assert [nmi.nmi for nmi in found] == ["61020000001"]


def test_an_assignment_with_only_the_required_fields_gets_defaults() -> None:
    (nmi,) = parse_nmi_list(_assignments({"NMI__c": "fallback-id", "NMI__r": {"Name": "N"}}))

    assert nmi == NmiInfo(
        assign_id="", nmi_id="fallback-id", nmi="N", description="", address="", active=True
    )


def test_an_inactive_assignment_is_marked_inactive() -> None:
    (nmi,) = parse_nmi_list(_assignments({"Status__c": "Inactive", "NMI__r": {"Name": "N"}}))
    assert nmi.active is False


def test_no_assignments_at_all_is_an_empty_list() -> None:
    assert parse_nmi_list({"data": {}, "isSuccess": True}) == []
    assert parse_nmi_list({"data": {"nmiAssignmentsForEnergyUsage": None}}) == []


@pytest.mark.parametrize(
    ("description", "address", "label"),
    [
        ("Home", "1 EXAMPLE STREET", "Home – 1 EXAMPLE STREET (N1)"),
        ("", "1 EXAMPLE STREET", "NMI – 1 EXAMPLE STREET (N1)"),
        ("Home", "", "Home – N1 (N1)"),
    ],
)
def test_the_label_falls_back_through_description_and_address(description, address, label) -> None:
    nmi = NmiInfo("a", "n", "N1", description, address, True)
    assert nmi.label == label


# --- account info -----------------------------------------------------------------------------


def test_account_info_that_is_not_an_object_is_an_api_error() -> None:
    with pytest.raises(MyEnergyApiError, match="Unexpected account details"):
        parse_account_info({"data": "oops", "isSuccess": True})


def test_account_info_that_carries_an_error_message_is_an_api_error() -> None:
    with pytest.raises(MyEnergyApiError, match="The portal reported: No access to this NMI"):
        parse_account_info({"data": {"errorMessage": "No access to this NMI"}, "isSuccess": True})


@pytest.mark.parametrize(
    "value", [None, 20260101, "", "2026-01", "not-a-date", "2026-13-45T00:00:00.000Z"]
)
def test_unreadable_dates_become_none_rather_than_errors(value) -> None:
    info = parse_account_info({"data": {"startDate": value, "endDate": value}})
    assert (info.start, info.end) == (None, None)


def test_account_info_defaults_when_the_portal_says_little() -> None:
    info = parse_account_info({"data": {"startDate": "2025-06-18T00:00:00.000Z"}})

    assert info.company == ""
    assert info.start == date(2025, 6, 18)
    assert info.end is None
    assert info.has_access is True  # no statement means access, as the browser behaves


def test_report_access_can_be_withheld() -> None:
    assert parse_account_info({"data": {"hasReportAccess": False}}).has_access is False


# --- meter data -------------------------------------------------------------------------------


def _meter(body, **extra) -> dict:
    return {"data": {"body": body, **extra}, "isSuccess": True}


@pytest.mark.parametrize("data", [{}, {"body": "text"}, {"body": None}, "text", [1]])
def test_meter_data_without_a_body_object_is_an_api_error(data) -> None:
    with pytest.raises(MyEnergyApiError, match="Unexpected meter data"):
        parse_meter_data({"data": data, "isSuccess": True})


@pytest.mark.parametrize("status", ["Success", "success", "Completed", "COMPLETE"])
def test_the_portals_success_spellings_are_accepted(status) -> None:
    assert parse_meter_data(_meter({"status": status, "nmiData": ""})) == {}


def test_a_missing_status_or_data_string_is_success_with_nothing_in_it() -> None:
    assert parse_meter_data(_meter({})) == {}
    assert parse_meter_data(_meter({"status": None, "nmiData": None})) == {}


def test_any_other_status_is_an_api_error_naming_it() -> None:
    with pytest.raises(MyEnergyApiError, match="ended with status Failed"):
        parse_meter_data(_meter({"status": "Failed", "nmiData": ""}))


# --- date chunks ------------------------------------------------------------------------------


def test_chunks_are_inclusive_newest_first_and_cover_the_range_exactly() -> None:
    chunks = date_chunks(date(2026, 1, 1), date(2026, 1, 10), days=4)

    assert chunks == [
        (date(2026, 1, 7), date(2026, 1, 10)),
        (date(2026, 1, 3), date(2026, 1, 6)),
        (date(2026, 1, 1), date(2026, 1, 2)),
    ]


def test_one_day_is_one_chunk_and_an_inverted_range_is_none() -> None:
    assert date_chunks(date(2026, 1, 1), date(2026, 1, 1)) == [(date(2026, 1, 1), date(2026, 1, 1))]
    assert date_chunks(date(2026, 1, 2), date(2026, 1, 1)) == []


def test_a_range_of_exactly_the_chunk_length_is_not_split() -> None:
    assert len(date_chunks(date(2026, 1, 1), date(2026, 3, 31))) == 1  # 90 days
    assert len(date_chunks(date(2026, 1, 1), date(2026, 4, 1))) == 2  # 91 days


# --- NEM12 ------------------------------------------------------------------------------------


def _header(interval: int = 30, unit: str = "KWH", suffix: str = "E1") -> str:
    return (
        "100,NEM12,202601010000,XXX1,\r\n"
        f"200,6102000000,{suffix},{suffix},{suffix},,SER1,{unit},{interval},\r\n"
    )


def _day(*tail: str, day: str = "20260101", values=None, per_day: int = 48) -> str:
    values = values if values is not None else ["1.0"] * per_day
    return ",".join(["300", day, *values, *tail]) + "\r\n"


def test_a_blank_or_non_numeric_interval_adds_nothing_but_keeps_the_hour() -> None:
    values = ["1.0", "", "x"] + ["0.0"] * 45  # hour 0: 1.0 and a blank; hour 1: "x" and 0.0
    channels = parse_nem12(_header() + _day("A", values=values))
    channel = channels["E1"]

    first = datetime(2026, 1, 1, 0, tzinfo=MARKET_TZ)
    assert channel.hours[first] == 1.0
    # Hour 1 holds the unreadable "x" and a 0.0: no energy, but the hour is kept.
    assert channel.hours[datetime(2026, 1, 1, 1, tzinfo=MARKET_TZ)] == 0.0
    assert channel.days == {date(2026, 1, 1)}


def test_a_300_record_with_a_short_tail_has_no_update_time() -> None:
    channel = parse_nem12(_header() + _day("A"))["E1"]
    assert channel.updated == {}


def test_a_300_record_with_an_update_time_records_it_and_a_bad_one_is_ignored() -> None:
    good = parse_nem12(_header() + _day("A", "", "", "20260102013000"))["E1"]
    assert good.updated == {date(2026, 1, 1): datetime(2026, 1, 2, 1, 30, tzinfo=MARKET_TZ)}

    bad = parse_nem12(_header() + _day("A", "", "", "not-a-time"))["E1"]
    assert bad.updated == {}
    assert bad.days == {date(2026, 1, 1)}  # the readings themselves still count


def test_a_malformed_400_record_is_ignored_and_a_good_one_still_applies() -> None:
    text = _header() + _day("V") + "400,abc,2,E,,\r\n400,1\r\n400,1,2,F,,\r\n"
    channel = parse_nem12(text)["E1"]

    # The good override (intervals 1-2, flag F) applies; the two broken ones did not stop it.
    assert channel.quality[datetime(2026, 1, 1, 0, tzinfo=MARKET_TZ)] == "F"
    assert channel.quality[datetime(2026, 1, 1, 1, tzinfo=MARKET_TZ)] == "V"


def test_unknown_record_types_between_data_are_skipped() -> None:
    text = _header() + _day("A") + "250,whatever\r\n" + _day("A", day="20260102") + "900\r\n"
    assert parse_nem12(text)["E1"].days == {date(2026, 1, 1), date(2026, 1, 2)}


def test_a_300_before_any_200_is_ignored() -> None:
    assert parse_nem12(_day("A")) == {}


def test_a_stream_that_reappears_updates_its_description_and_keeps_earlier_days() -> None:
    text = (
        _header(interval=30, unit="KWH")
        + _day("A")
        + "200,6102000000,E1,E1,E1,,SER2,WH,30,\r\n"
        + _day("A", day="20260102", values=["1000"] * 48)
    )
    channel = parse_nem12(text)["E1"]

    assert channel.meter_serial == "SER2"
    assert channel.unit == "WH"
    assert channel.days == {date(2026, 1, 1), date(2026, 1, 2)}
    # The second day is read in Wh: 1000 Wh per interval is 1 kWh.
    assert daily_total(channel, date(2026, 1, 2)) == 48.0


def test_a_short_200_record_takes_defaults_for_what_it_omits() -> None:
    channel = parse_nem12("200,6102000000,E1,E1,E1\r\n" + _day("A"))["E1"]
    assert (channel.meter_serial, channel.unit, channel.interval_minutes) == ("", "KWH", 30)

    # A repeat of a short 200 leaves what the stream already knew.
    again = parse_nem12(_header(unit="MWH") + "200,6102000000,E1,E1,E1\r\n")["E1"]
    assert (again.unit, again.meter_serial) == ("MWH", "SER1")


def test_a_day_with_no_flag_is_treated_as_actual() -> None:
    channel = parse_nem12(_header() + _day())["E1"]  # no quality field at all
    assert set(channel.quality.values()) == {"A"}


def test_daily_total_and_last_day_of_an_empty_channel() -> None:
    channel = Channel(nmi="N", suffix="E1", meter_serial="", unit="KWH", interval_minutes=30)

    assert channel.last_day is None
    assert daily_total(channel, date(2026, 1, 1)) is None


def test_merge_keeps_a_serial_when_the_newer_channel_has_none() -> None:
    older = parse_nem12(_header() + _day("A"))
    newer = parse_nem12("200,6102000000,E1,E1,E1\r\n" + _day("A", day="20260102"))

    merge_channels(older, newer)

    assert older["E1"].meter_serial == "SER1"
    assert older["E1"].days == {date(2026, 1, 1), date(2026, 1, 2)}
