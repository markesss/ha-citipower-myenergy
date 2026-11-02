"""Tests for the portal parsers, against responses captured from myEnergy."""

from __future__ import annotations

import pytest

from custom_components.citipower_myenergy.exceptions import (
    MyEnergyApiError,
    MyEnergyAuthError,
)
from custom_components.citipower_myenergy.portal import (
    AuraContext,
    build_context,
    build_remoting_call,
    describe_url,
    extract_descriptor,
    extract_return_value,
    find_client_redirect,
    is_mfa_page,
    login_error,
    login_redirect,
    parse_aura_body,
    parse_mfa_page,
    parse_remoting_result,
    session_seconds_remaining,
    token_cookie_name,
    unwrap_result,
)


def test_login_response_yields_frontdoor_url(fixture_text):
    result = extract_return_value(parse_aura_body(fixture_text("login_response.json")))
    url = login_redirect(result)
    assert url is not None
    assert "/secur/frontdoor.jsp" in url
    assert "sid=" in url


def test_failed_login_has_no_redirect():
    assert login_redirect({"isSuccess": False, "errorMessage": "Your login attempt failed"}) is None
    assert login_error({"isSuccess": False, "errorMessage": "Your login attempt failed"}) == (
        "Your login attempt failed"
    )
    assert login_redirect({"data": {}, "isSuccess": True}) is None


def test_describe_url_masks_session_id():
    described = describe_url("https://x.example/secur/frontdoor.jsp?sid=SECRET&retURL=%2Fs%2F")
    assert "SECRET" not in described
    assert "sid=<6 chars>" in described


def test_frontdoor_redirects_to_mfa(fixture_text):
    target = find_client_redirect(fixture_text("frontdoor.html"))
    assert target == "https://myenergy.powercor.com.au/apex/MFA_Enabler?retURL=%2Fs%2F"


def test_mfa_post_redirects_home(fixture_text):
    assert find_client_redirect(fixture_text("mfa_post.html")) == "/s/"


def test_mfa_page_is_recognised(fixture_text):
    assert is_mfa_page(fixture_text("mfa_enabler.html"))
    assert not is_mfa_page(fixture_text("community_page.html"))


def test_mfa_page_parse(fixture_text):
    page = parse_mfa_page(fixture_text("mfa_enabler.html"))
    assert page.controller == "MFAEnablerController"
    assert page.vid == "066OY0000004czZ"
    assert set(page.methods) == {"initiateMFA", "verifyMFA", "verifyMFARegister"}
    assert page.methods["verifyMFA"].ver == 60
    assert page.has_sms
    assert page.options == ["sms", "email"]
    assert page.email_hint == "user@example.com"
    assert page.brand == "VPN"
    assert page.final_form_action == "/MFA_Enabler"
    # Exactly the fields the browser posted.
    assert set(page.final_form_fields) == {
        "j_id0:j_id29",
        "j_id0:j_id29:j_id31",
        "com.salesforce.visualforce.ViewState",
        "com.salesforce.visualforce.ViewStateVersion",
        "com.salesforce.visualforce.ViewStateMAC",
        "com.salesforce.visualforce.ViewStateCSRF",
    }
    assert page.final_form_fields["j_id0:j_id29:j_id31"] == "j_id0:j_id29:j_id31"


def test_mfa_page_without_sms_offers_email_only(fixture_text):
    html = fixture_text("mfa_enabler.html").replace(
        'hasSMSOption = "true"', 'hasSMSOption = "false"'
    )
    assert parse_mfa_page(html).options == ["email"]


def test_remoting_call_shape(fixture_text):
    page = parse_mfa_page(fixture_text("mfa_enabler.html"))
    call = build_remoting_call(page, "verifyMFA", ["123456", "id", False, "sms"], 3)
    assert call["action"] == "MFAEnablerController"
    assert call["method"] == "verifyMFA"
    assert call["data"] == ["123456", "id", False, "sms"]
    assert call["type"] == "rpc"
    assert call["tid"] == 3
    assert set(call["ctx"]) == {"csrf", "vid", "ns", "ver", "authorization"}


def test_remoting_result(fixture_json):
    result = parse_remoting_result(fixture_json("initiate_mfa.json"), "initiateMFA")
    assert result["success"] is True
    assert result["verificationIdentifier"]
    assert parse_remoting_result(fixture_json("verify_mfa.json"), "verifyMFA") == {"success": True}


def test_remoting_exception():
    with pytest.raises(MyEnergyApiError):
        parse_remoting_result([{"type": "exception", "message": "boom"}], "verifyMFA")


def test_community_page_context(fixture_text):
    html = fixture_text("community_page.html")
    assert token_cookie_name(html).startswith("__Host-ERIC_")
    context = build_context(extract_descriptor(html))
    assert context["app"] == "siteforce:communityApp"
    assert context["fwuid"]
    assert context["loaded"]


def test_missing_descriptor():
    with pytest.raises(MyEnergyApiError):
        extract_descriptor("<html></html>")


def test_invalid_session_is_auth_error():
    with pytest.raises(MyEnergyAuthError):
        extract_return_value({"event": {"descriptor": "markup://aura:invalidSession"}})


def test_failed_action_is_api_error():
    with pytest.raises(MyEnergyApiError):
        extract_return_value({"actions": [{"state": "ERROR", "error": [{"message": "nope"}]}]})


def test_wrapped_error_body():
    parsed = parse_aura_body('*/{"event":{"descriptor":"markup://aura:invalidSession"}}/*ERROR*/')
    with pytest.raises(MyEnergyAuthError):
        extract_return_value(parsed)


def test_unwrap_result_failure():
    with pytest.raises(MyEnergyApiError, match="no access"):
        unwrap_result({"isSuccess": False, "errorMessage": "no access"}, "meter data")


def test_session_seconds(fixture_text):
    assert session_seconds_remaining(fixture_text("session_time.txt")) == 7172
    assert session_seconds_remaining(None) is None


def test_aura_context_round_trip():
    context = AuraContext(context={"fwuid": "x"}, token="t")
    assert AuraContext.from_dict(context.as_dict()) == context
    assert AuraContext.from_dict({"context": {}, "token": ""}) is None
    assert AuraContext.from_dict(None) is None
