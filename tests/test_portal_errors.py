"""Failure and edge shapes for the portal parsers.

``test_portal.py`` covers the captured responses. These cover what the parsers do with pages and
payloads that are damaged, partial or of a different shape. None of these is a capture: they are
built to hit one branch each, and say so.
"""

from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from custom_components.citipower_myenergy.exceptions import (
    MyEnergyApiError,
    MyEnergyAuthError,
)
from custom_components.citipower_myenergy.portal import (
    build_remoting_call,
    describe_url,
    extract_descriptor,
    extract_return_value,
    login_error,
    login_redirect,
    parse_mfa_page,
    parse_remoting_result,
    unwrap_result,
)

# --- describe_url ----------------------------------------------------------------------------


@pytest.mark.parametrize("empty", [None, ""])
def test_describe_url_of_nothing_is_a_marker(empty) -> None:
    assert describe_url(empty) == "<none>"


def test_describe_url_of_a_relative_path_has_no_host_and_no_values() -> None:
    assert describe_url("/s/login/") == "/s/login/"
    assert describe_url("/x?token=hunter2&a=") == "/x?token=<7 chars>&a=<0 chars>"


# --- the Aura descriptor ----------------------------------------------------------------------

DESCRIPTOR = {"fwuid": "abc", "app": "siteforce:communityApp"}


def _script(blob: str, tail: str = "app.js") -> str:
    return f'<script src="/s/sfsites/l/{blob}/{tail}"></script>'


def test_the_descriptor_skips_undecodable_and_fwuid_less_blobs_and_takes_the_next() -> None:
    html = (
        _script(quote("{not json"))
        + _script(quote(json.dumps({"mode": "PROD"})))  # valid JSON, no fwuid
        + _script(quote(json.dumps(DESCRIPTOR)))
    )

    assert extract_descriptor(html) == DESCRIPTOR


def test_the_descriptor_tries_the_other_resource_names_when_app_js_has_none() -> None:
    html = _script(quote(json.dumps(DESCRIPTOR)), "bootstrap.js")
    assert extract_descriptor(html)["fwuid"] == "abc"


def test_a_resource_name_with_no_descriptor_path_before_it_is_ignored() -> None:
    with pytest.raises(MyEnergyApiError, match="Aura framework descriptor"):
        extract_descriptor("<script src='/static/vendor/app.js'></script>")


# --- unwrapping Aura results ------------------------------------------------------------------


def test_a_successful_action_whose_return_value_is_not_double_wrapped_is_returned_as_is() -> None:
    parsed = {"actions": [{"state": "SUCCESS", "returnValue": "plain"}]}
    assert extract_return_value(parsed) == "plain"
    parsed = {"actions": [{"state": "SUCCESS", "returnValue": {"other": 1}}]}
    assert extract_return_value(parsed) == {"other": 1}


def test_an_error_entry_that_is_not_a_dict_falls_back_to_the_state() -> None:
    parsed = {"actions": [{"state": "ERROR", "error": ["just a string"]}]}
    with pytest.raises(MyEnergyApiError, match="rejected the request: ERROR"):
        extract_return_value(parsed)


@pytest.mark.parametrize("payload", [None, "text", [1], 5])
def test_unwrap_result_rejects_anything_but_an_object(payload) -> None:
    with pytest.raises(MyEnergyApiError, match="Unexpected the thing response"):
        unwrap_result(payload, "the thing")


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"isSuccess": False, "errorMessage": "top"}, "top"),
        ({"isSuccess": False, "message": "second"}, "second"),
        ({"isSuccess": False, "data": {"errorMessage": "nested"}}, "nested"),
        ({"isSuccess": False, "data": "not a dict"}, "no reason given"),
        ({"isSuccess": False}, "no reason given"),
    ],
)
def test_unwrap_result_reports_the_portals_reason_wherever_it_put_it(payload, message) -> None:
    with pytest.raises(MyEnergyApiError, match=f"could not return the thing: {message}"):
        unwrap_result(payload, "the thing")


def test_unwrap_result_without_a_data_key_returns_the_payload_itself() -> None:
    payload = {"isSuccess": True, "other": 1}
    assert unwrap_result(payload, "x") is payload
    assert unwrap_result({"data": [1]}, "x") == [1]


# --- the login result -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "text",
        {"isSuccess": False, "data": {"loginUrl": "https://x.invalid/f"}},
        {"data": "not a dict"},
        {"data": {"loginUrl": ""}},
        {"data": {"loginUrl": 5}},
        {"data": {}},
    ],
)
def test_login_redirect_needs_a_non_empty_string_url_on_a_successful_result(payload) -> None:
    assert login_redirect(payload) is None


def test_login_redirect_returns_the_url() -> None:
    url = "https://x.invalid/secur/frontdoor.jsp?sid=abc"
    assert login_redirect({"isSuccess": True, "data": {"loginUrl": url}}) == url


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"errorMessage": "top"}, "top"),
        ({"message": "second"}, "second"),
        ({"error": "third"}, "third"),
        ({"data": {"message": "nested"}}, "nested"),
        ({"errorMessage": "", "message": "falls through"}, "falls through"),
        ({"errorMessage": 5, "data": {"error": "later"}}, "later"),  # a non-string is skipped
        ({"data": "not a dict"}, None),
        ({}, None),
        (None, None),
        ("A plain string", "A plain string"),
        ("", None),
        (5, None),
    ],
)
def test_login_error_finds_the_first_non_empty_string(payload, expected) -> None:
    assert login_error(payload) == expected


# --- the MFA page -----------------------------------------------------------------------------


def _mfa_html(
    provider: str | None = None,
    submit: str | None = "jsfcljs(document.forms['codeForm'],'codeForm:go,codeForm:go','')",
    form: str | None = (
        '<form id="codeForm" action="/MFA_Enabler?x=1">'
        '<input type="hidden" name="codeForm" value="codeForm">'
        '<input type="text" name="visible" value="ignored">'
        "<input type=hidden name=\"keep\" value='a&amp;b'>"
        "</form>"
    ),
) -> str:
    if provider is None:
        provider = json.dumps(
            {
                "vf": {"vid": "vid1"},
                "actions": {
                    "Ctl": {
                        "ms": [
                            {"name": "initiateMFA", "csrf": "c1", "ver": 61.0},
                            {"name": "verifyMFA", "csrf": "c2", "authorization": "auth2"},
                        ]
                    }
                },
            }
        )
    parts = ["<html>"]
    if provider:
        parts.append(
            "Visualforce.remoting.Manager.add(new $VFRM.RemotingProviderImpl(" + provider + "));"
        )
    if submit:
        parts.append(f"<script>function redirectToHome() {{ {submit}; }}</script>")
    if form:
        parts.append(form)
    parts.append("</html>")
    return "\n".join(parts)


def test_a_minimal_mfa_page_is_read_field_by_field() -> None:
    page = parse_mfa_page(_mfa_html())

    assert (page.vid, page.controller) == ("vid1", "Ctl")
    assert page.methods["initiateMFA"].ver == 61  # a float version is truncated
    assert page.methods["verifyMFA"].ver == 60  # and the default applies when omitted
    assert page.methods["initiateMFA"].authorization is None
    assert page.methods["verifyMFA"].authorization == "auth2"
    assert page.final_form_action == "/MFA_Enabler?x=1"
    # Hidden inputs only; the clicked link's name and value are added; entities are decoded.
    assert page.final_form_fields == {
        "codeForm": "codeForm",
        "keep": "a&b",
        "codeForm:go": "codeForm:go",
    }
    # No SMS script variables and no email sentence on this page.
    assert (page.has_sms, page.sms_hint, page.email_hint, page.brand) == (False, "", "", "")
    assert page.options == ["email"]


def test_a_form_without_an_action_posts_to_the_default_path() -> None:
    page = parse_mfa_page(_mfa_html(form='<form name="codeForm"></form>'))
    assert page.final_form_action == "/MFA_Enabler"


@pytest.mark.parametrize(
    ("html", "message"),
    [
        (_mfa_html(provider=""), "did not include its remoting setup"),
        (_mfa_html(provider="{not json}"), "Could not read the verification page's remoting"),
        (_mfa_html(provider='{"vf": {}, "actions": {}}'), "remoting setup was incomplete"),
        (
            _mfa_html(provider='{"vf": {"vid": "v"}, "actions": {}}'),
            "remoting setup was incomplete",
        ),
        (_mfa_html(submit=None), "Could not find how the verification page submits"),
        (_mfa_html(form='<form id="other"></form>'), "has no form named codeForm"),
        (_mfa_html(form=None), "has no form named codeForm"),
    ],
)
def test_a_damaged_mfa_page_is_an_api_error_that_says_what_is_missing(html, message) -> None:
    with pytest.raises(MyEnergyApiError, match=message):
        parse_mfa_page(html)


# --- remoting calls ---------------------------------------------------------------------------


def test_a_remoting_call_includes_authorization_only_when_the_page_gave_one() -> None:
    page = parse_mfa_page(_mfa_html())

    without = build_remoting_call(page, "initiateMFA", ["sms"], 2)
    assert "authorization" not in without["ctx"]
    assert without["ctx"] == {"csrf": "c1", "vid": "vid1", "ns": "", "ver": 61}

    with_auth = build_remoting_call(page, "verifyMFA", ["1", "id", False, "sms"], 3)
    assert with_auth["ctx"]["authorization"] == "auth2"
    assert (with_auth["action"], with_auth["tid"], with_auth["type"]) == ("Ctl", 3, "rpc")


def test_a_method_the_page_does_not_offer_cannot_be_called() -> None:
    page = parse_mfa_page(_mfa_html())
    with pytest.raises(MyEnergyApiError, match="does not offer resendMFA"):
        build_remoting_call(page, "resendMFA", [], 2)


@pytest.mark.parametrize(
    ("payload", "error", "match"),
    [
        ([{"type": "exception", "message": "Session invalid"}], MyEnergyAuthError, "invalid"),
        (
            [{"statusCode": 500, "message": "Remote failure"}],
            MyEnergyApiError,
            "initiateMFA failed: Remote failure",
        ),
        ([{"type": "exception"}], MyEnergyApiError, "the portal reported an error"),
        ([{"type": "exception", "message": "Invalid input"}], MyEnergyApiError, "Invalid input"),
        ([], MyEnergyApiError, "Unexpected response to initiateMFA"),
        (["not a dict", 5], MyEnergyApiError, "Unexpected response"),
        ([{"result": "a string"}], MyEnergyApiError, "Unexpected response"),
    ],
)
def test_remoting_failures_map_onto_the_exception_hierarchy(payload, error, match) -> None:
    with pytest.raises(error, match=match):
        parse_remoting_result(payload, "initiateMFA")


def test_remoting_results_tolerate_a_bare_object_and_skip_junk_entries() -> None:
    assert parse_remoting_result({"result": {"success": True}}, "m") == {"success": True}
    assert parse_remoting_result(["junk", {"result": {"a": 1}}], "m") == {"a": 1}
    # A call that returns nothing is an empty result, which the callers read as "not successful".
    assert parse_remoting_result([{"result": None}], "m") == {}
