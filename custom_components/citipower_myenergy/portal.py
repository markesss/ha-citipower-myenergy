"""Pure parsing helpers for the myEnergy portal.

Nothing in here performs I/O, so it can be tested against captured responses
without Home Assistant or a network. The Aura helpers are adapted from
gheydon/ha-yvw (MIT), which talks to another Salesforce Experience Cloud portal
built the same way.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import unescape
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from .const import AURA_APP, MFA_METHOD_EMAIL, MFA_METHOD_SMS
from .exceptions import MyEnergyApiError, MyEnergyAuthError

# --- Aura -----------------------------------------------------------------------

# The framework descriptor sits URL-encoded in resource URLs, e.g.
# /s/sfsites/l/%7B...%7D/app.js. It nests braces, so it is sliced out by
# locating the trailing path rather than matched with a lazy regex.
_DESCRIPTOR_MARKER = "/sfsites/l/"
_DESCRIPTOR_TAILS = ("/app.js", "/resources.js", "/bootstrap.js")

# Signed-in pages name the cookie holding the CSRF token ("tokencookie"
# reversed). Matched as a JSON key so it cannot latch onto other references.
_TOKEN_COOKIE_RE = re.compile(r'"eikoocnekot"\s*:\s*"([^"]+)"')

# frontdoor.jsp and Visualforce postbacks redirect with script, not HTTP.
_CLIENT_REDIRECT_RES = (
    re.compile(r"""window\.location\.replace\(\s*['"]([^'"]+)['"]""", re.IGNORECASE),
    re.compile(r"""handleRedirect\(\s*['"]([^'"]+)['"]""", re.IGNORECASE),
    re.compile(r"""window\.location(?:\.href)?\s*=\s*['"]([^'"]+)['"]""", re.IGNORECASE),
    re.compile(
        r"""<meta[^>]+http-equiv=['"]refresh['"][^>]*"""
        r"""content=['"][^'"]*?\burl\s*=\s*([^'"]+)['"]""",
        re.IGNORECASE,
    ),
)


@dataclass(frozen=True, slots=True)
class AuraContext:
    """Everything an Aura request needs beyond the session cookies."""

    context: dict[str, Any]
    token: str

    def as_dict(self) -> dict[str, Any]:
        """Serialise for storage."""
        return {"context": self.context, "token": self.token}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> AuraContext | None:
        """Restore from storage, tolerating anything malformed."""
        if not isinstance(data, dict):
            return None
        context, token = data.get("context"), data.get("token")
        if not isinstance(context, dict) or not isinstance(token, str) or not token:
            return None
        return cls(context=context, token=token)


def describe_url(url: str | None) -> str:
    """Describe a URL for logs with every query value masked.

    frontdoor.jsp URLs carry a live session id in the query string.
    """
    if not url:
        return "<none>"
    parts = urlsplit(url)
    params = parse_qsl(parts.query, keep_blank_values=True)
    masked = "&".join(f"{name}=<{len(value)} chars>" for name, value in params)
    location = parts.path if not parts.netloc else f"{parts.netloc}{parts.path}"
    return f"{location}?{masked}" if masked else location


def find_client_redirect(html: str) -> str | None:
    """Return the target of a script or meta redirect, if the page has one."""
    for pattern in _CLIENT_REDIRECT_RES:
        match = pattern.search(html)
        if match:
            return unescape(match.group(1))
    return None


def parse_aura_body(text: str) -> dict[str, Any]:
    """Parse an Aura response, stripping its anti-hijacking wrappers."""
    body = text.strip()
    if body.startswith("while(1);"):
        body = body[len("while(1);") :]
    if body.startswith("*/"):
        body = body[2:]
    if body.endswith("/*ERROR*/"):
        body = body[: -len("/*ERROR*/")]
    try:
        return json.loads(body.strip())
    except ValueError as err:
        raise MyEnergyApiError("Could not parse the portal response") from err


def extract_descriptor(html: str) -> dict[str, Any]:
    """Pull the Aura framework descriptor out of a community page."""
    for tail in _DESCRIPTOR_TAILS:
        end = html.find(tail)
        while end != -1:
            start = html.rfind(_DESCRIPTOR_MARKER, 0, end)
            if start != -1:
                blob = html[start + len(_DESCRIPTOR_MARKER) : end]
                try:
                    descriptor = json.loads(unquote(blob))
                except ValueError:
                    descriptor = None
                if isinstance(descriptor, dict) and "fwuid" in descriptor:
                    return descriptor
            end = html.find(tail, end + 1)
    raise MyEnergyApiError("Could not find the Aura framework descriptor on the page")


def token_cookie_name(html: str) -> str | None:
    """Return the cookie a page says holds its CSRF token, if any."""
    match = _TOKEN_COOKIE_RE.search(html)
    return match.group(1) if match else None


def build_context(descriptor: dict[str, Any], app: str = AURA_APP) -> dict[str, Any]:
    """Build the aura.context payload from a page's framework descriptor."""
    return {
        "mode": descriptor.get("mode", "PROD"),
        "fwuid": descriptor["fwuid"],
        "app": descriptor.get("app", app),
        "loaded": descriptor.get("loaded", {}),
        "dn": [],
        "globals": {},
        "uad": True,
    }


def build_message(classname: str, method: str, params: dict[str, Any] | None) -> str:
    """Build the Aura ``message`` payload for one Apex call."""
    return json.dumps(
        {
            "actions": [
                {
                    "id": "1;a",
                    "descriptor": "aura://ApexActionController/ACTION$execute",
                    "callingDescriptor": "UNKNOWN",
                    "params": {
                        "namespace": "",
                        "classname": classname,
                        "method": method,
                        "params": params or {},
                        "cacheable": False,
                        "isContinuation": False,
                    },
                }
            ]
        }
    )


def extract_return_value(parsed: dict[str, Any]) -> Any:
    """Unwrap an Aura response, translating failures into exceptions."""
    event = parsed.get("event") or {}
    descriptor = event.get("descriptor", "")
    exception_message = parsed.get("exceptionMessage", "") or ""

    # A dead session and a rejected CSRF token both surface as invalidSession.
    if "invalidSession" in descriptor or "Guest user access is not allowed" in exception_message:
        raise MyEnergyAuthError("The portal session is no longer valid")

    actions = parsed.get("actions") or []
    if not actions:
        raise MyEnergyApiError(exception_message or "The portal returned no action result")

    action = actions[0]
    if action.get("state") != "SUCCESS":
        errors = action.get("error") or []
        detail = errors[0].get("message") if errors and isinstance(errors[0], dict) else None
        raise MyEnergyApiError(f"The portal rejected the request: {detail or action.get('state')}")

    return_value = action.get("returnValue")
    # ApexActionController double-wraps: returnValue.returnValue is the payload.
    if isinstance(return_value, dict) and "returnValue" in return_value:
        return return_value["returnValue"]
    return return_value


def unwrap_result(payload: Any, what: str) -> Any:
    """Unwrap the portal's own ``{"data": ..., "isSuccess": ...}`` envelope."""
    if not isinstance(payload, dict):
        raise MyEnergyApiError(f"Unexpected {what} response from the portal")
    if payload.get("isSuccess") is False:
        message = payload.get("errorMessage") or payload.get("message")
        data = payload.get("data")
        if not message and isinstance(data, dict):
            message = data.get("errorMessage")
        raise MyEnergyApiError(
            f"The portal could not return {what}: {message or 'no reason given'}"
        )
    return payload.get("data", payload)


def login_redirect(payload: Any) -> str | None:
    """Return the frontdoor URL from a communityLogin result, if it signed in."""
    if not isinstance(payload, dict) or payload.get("isSuccess") is False:
        return None
    data = payload.get("data")
    if isinstance(data, dict):
        url = data.get("loginUrl")
        return url if isinstance(url, str) and url else None
    return None


def login_error(payload: Any) -> str | None:
    """Return the portal's explanation of a refused sign-in, if it gave one."""
    if isinstance(payload, dict):
        for source in (payload, payload.get("data")):
            if isinstance(source, dict):
                for key in ("errorMessage", "message", "error"):
                    value = source.get(key)
                    if isinstance(value, str) and value:
                        return value
    if isinstance(payload, str) and payload:
        return payload
    return None


# --- Visualforce remoting (the MFA page) --------------------------------------

_PROVIDER_RE = re.compile(
    r"Visualforce\.remoting\.Manager\.add\(\s*new\s+\$VFRM\.RemotingProviderImpl\((\{.*?\})\)\s*\)",
    re.S,
)
_JS_VAR_RE = r"""\b{name}\s*=\s*["']([^"']*)["']"""
_FORM_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.IGNORECASE | re.S)
_INPUT_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s">]+))""")
# redirectToHome() submits the code form "as if" its command link was clicked,
# by naming the link's id as both a parameter name and value.
_JSFCLJS_RE = re.compile(
    r"""jsfcljs\(\s*document\.forms\[['"]([^'"]+)['"]\]\s*,\s*['"]([^'"]*)['"]"""
)
_VIEWSTATE_PREFIX = "com.salesforce.visualforce."


@dataclass(frozen=True, slots=True)
class RemotingMethod:
    """The signing material for one @RemoteAction method on a page."""

    name: str
    ns: str
    ver: int
    csrf: str
    authorization: str | None


@dataclass(slots=True)
class MfaPage:
    """What the MFA_Enabler page offers and what posting back to it needs."""

    vid: str
    controller: str
    methods: dict[str, RemotingMethod]
    has_sms: bool
    sms_hint: str
    email_hint: str
    brand: str
    final_form_action: str
    final_form_fields: dict[str, str] = field(default_factory=dict)

    @property
    def options(self) -> list[str]:
        """Return the verification methods available to this account."""
        if self.has_sms and self.sms_hint:
            return [MFA_METHOD_SMS, MFA_METHOD_EMAIL]
        return [MFA_METHOD_EMAIL]


def is_mfa_page(html: str) -> bool:
    """Return whether a page is the MFA_Enabler verification page."""
    return "MFAEnablerController" in html and "RemotingProviderImpl" in html


def _attrs(tag: str) -> dict[str, str]:
    return {
        m.group(1): unescape(m.group(2) or m.group(3) or m.group(4) or "")
        for m in _ATTR_RE.finditer(tag)
    }


def _js_var(html: str, name: str) -> str | None:
    match = re.search(_JS_VAR_RE.format(name=re.escape(name)), html)
    return match.group(1) if match else None


def parse_mfa_page(html: str) -> MfaPage:
    """Read the remoting signatures, options and final form from MFA_Enabler."""
    match = _PROVIDER_RE.search(html)
    if not match:
        raise MyEnergyApiError("The verification page did not include its remoting setup")
    try:
        provider = json.loads(match.group(1))
    except ValueError as err:
        raise MyEnergyApiError("Could not read the verification page's remoting setup") from err

    vid = (provider.get("vf") or {}).get("vid")
    actions = provider.get("actions") or {}
    if not vid or not actions:
        raise MyEnergyApiError("The verification page's remoting setup was incomplete")
    controller, spec = next(iter(actions.items()))
    methods: dict[str, RemotingMethod] = {}
    for entry in spec.get("ms") or []:
        methods[entry["name"]] = RemotingMethod(
            name=entry["name"],
            ns=entry.get("ns", ""),
            ver=int(float(entry.get("ver", 60))),
            csrf=entry.get("csrf", ""),
            authorization=entry.get("authorization"),
        )

    # The page script fills the hints in from these variables.
    has_sms = (_js_var(html, "hasSMSOption") or "").lower() == "true"
    sms_hint = _js_var(html, "userMobile") or ""
    brand = _js_var(html, "brand") or ""
    email_match = re.search(r"Send an email to\s+([^<]+?)\s*</", html)
    email_hint = unescape(email_match.group(1)).strip() if email_match else ""

    # The view state lives outside any form (in an "ajax-view-state" span) and
    # is copied into the form by vfPrepareForms() before submitting.
    view_state = {
        attrs["name"]: attrs.get("value", "")
        for attrs in (_attrs(tag) for tag in _INPUT_RE.findall(html))
        if attrs.get("name", "").startswith(_VIEWSTATE_PREFIX)
    }

    submit = _JSFCLJS_RE.search(html)
    if not submit:
        raise MyEnergyApiError("Could not find how the verification page submits")
    form_id = submit.group(1)
    clicked = [part for part in submit.group(2).split(",") if part]

    action = "/MFA_Enabler"
    fields: dict[str, str] = {}
    for form in _FORM_RE.finditer(html):
        form_attrs = _attrs(form.group(1))
        if form_attrs.get("id") != form_id and form_attrs.get("name") != form_id:
            continue
        action = form_attrs.get("action") or action
        for tag in _INPUT_RE.findall(form.group(2)):
            attrs = _attrs(tag)
            if attrs.get("type", "").lower() == "hidden" and attrs.get("name"):
                fields[attrs["name"]] = attrs.get("value", "")
        break
    else:
        raise MyEnergyApiError(f"The verification page has no form named {form_id}")

    # jsfcljs takes "name,value,name,value...".
    for name, value in zip(clicked[0::2], clicked[1::2], strict=False):
        fields[name] = value
    fields.update(view_state)

    return MfaPage(
        vid=vid,
        controller=controller,
        methods=methods,
        has_sms=has_sms,
        sms_hint=sms_hint,
        email_hint=email_hint,
        brand=brand,
        final_form_action=action,
        final_form_fields=fields,
    )


def build_remoting_call(page: MfaPage, method: str, data: list[Any], tid: int) -> dict[str, Any]:
    """Build the JSON body for a Visualforce remoting call."""
    signature = page.methods.get(method)
    if signature is None:
        raise MyEnergyApiError(f"The verification page does not offer {method}")
    ctx: dict[str, Any] = {
        "csrf": signature.csrf,
        "vid": page.vid,
        "ns": signature.ns,
        "ver": signature.ver,
    }
    if signature.authorization:
        ctx["authorization"] = signature.authorization
    return {
        "action": page.controller,
        "method": method,
        "data": data,
        "type": "rpc",
        "tid": tid,
        "ctx": ctx,
    }


def parse_remoting_result(payload: Any, method: str) -> dict[str, Any]:
    """Return the result of a remoting call, raising on transport-level errors."""
    entries = payload if isinstance(payload, list) else [payload]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "exception" or entry.get("statusCode", 200) != 200:
            message = entry.get("message") or "the portal reported an error"
            if "session" in str(message).lower() and "invalid" in str(message).lower():
                raise MyEnergyAuthError(message)
            raise MyEnergyApiError(f"{method} failed: {message}")
        result = entry.get("result")
        if isinstance(result, dict):
            return result
        if result is None:
            return {}
    raise MyEnergyApiError(f"Unexpected response to {method}")


# --- Session clock -----------------------------------------------------------

_SESSION_TIME_RE = re.compile(r'"sr"\s*:\s*(\d+)')


def session_seconds_remaining(text: str | None) -> int | None:
    """Read the seconds remaining from SessionTimeServlet's response."""
    if not text:
        return None
    match = _SESSION_TIME_RE.search(text)
    return int(match.group(1)) if match else None
