"""Constants for the CitiPower & Powercor myEnergy integration."""

from __future__ import annotations

from datetime import timedelta, timezone

DOMAIN = "citipower_myenergy"

# --- Portal -------------------------------------------------------------------
# myEnergy is a Salesforce Experience Cloud site shared by CitiPower, Powercor
# and United Energy. Customer pages live under /s/; the MFA step is a classic
# Visualforce page under /apex/.

BASE_URL = "https://myenergy.powercor.com.au"
COMMUNITY_PATH = "/s"
AURA_ENDPOINT = f"{BASE_URL}{COMMUNITY_PATH}/sfsites/aura"
LOGIN_PAGE = f"{BASE_URL}{COMMUNITY_PATH}/login/"
HOME_PAGE = f"{BASE_URL}{COMMUNITY_PATH}/"
USAGE_PAGE = f"{BASE_URL}{COMMUNITY_PATH}/energy-usage"
REMOTING_ENDPOINT = f"{BASE_URL}/apexremote"
SESSION_TIME_URL = f"{BASE_URL}/_nc_external/system/security/session/SessionTimeServlet"

AURA_APP = "siteforce:communityApp"
LOGIN_AURA_APP = "siteforce:loginApp2"

# The login form appends the network's brand to the email address before
# posting it: CitiPower/Powercor accounts are "<email>.VPN" (Victoria Power
# Networks). United Energy uses the same portal with its own suffix.
USERNAME_SUFFIX = ".VPN"

# --- Apex methods -------------------------------------------------------------

APEX_LOGIN_CLASS = "LightningLoginFormController"
APEX_LOGIN_METHOD = "communityLogin"

APEX_USAGE_PAGE_CLASS = "EnergyUsageDetailPageController"
APEX_NMI_LIST_METHOD = "getEnergyUsageDetailWrapper"

APEX_ENERGY_INFO_CLASS = "EnergyInformationController"
APEX_ACCOUNT_INFO_METHOD = "retrieveAccountHolderInfo"
APEX_METER_DATA_METHOD = "getMeterData"

METER_DATA_FORMAT = "Customer Access NEM12"

MFA_CONTROLLER = "MFAEnablerController"
MFA_INITIATE = "initiateMFA"
MFA_VERIFY = "verifyMFA"
MFA_METHOD_EMAIL = "email"
MFA_METHOD_SMS = "sms"
MFA_CODE_LENGTH = 6

# --- Data ---------------------------------------------------------------------

# NEM12 interval data is recorded in market time: AEST all year, no daylight
# saving. Using Australia/Melbourne here would shift every summer reading.
MARKET_TZ = timezone(timedelta(hours=10), "AEST")

# The portal itself requests at most ~3 months per call.
MAX_CHUNK_DAYS = 90
# How far back a first import reaches. The portal offers up to two years, and
# says exactly how far for each NMI; this is only an upper bound.
MAX_HISTORY_DAYS = 2 * 366
# Every poll re-reads this many days so substituted/estimated readings that
# are later replaced with actuals are corrected in the statistics.
REFRESH_WINDOW_DAYS = 7

# Readings land the morning after (about 01:00 AEST), so a few polls a day is
# plenty.
UPDATE_INTERVAL = timedelta(hours=4)

# --- Session ------------------------------------------------------------------

# The portal's session lasts two hours idle. Salesforce only extends a session
# when a request arrives in the second half of its lifetime, so a ping interval
# under half the timeout always keeps it alive.
DEFAULT_KEEPALIVE_MINUTES = 45
MIN_KEEPALIVE_MINUTES = 5
MAX_KEEPALIVE_MINUTES = 55
KEEPALIVE_RETRY = timedelta(minutes=2)

# --- Config entry keys --------------------------------------------------------
# The password is used once during sign-in and never stored: every new sign-in
# needs a fresh verification code anyway.

CONF_SID = "sid"
CONF_COOKIES = "cookies"
CONF_NMI = "nmi"
CONF_NMI_ID = "nmi_id"
CONF_NMI_ASSIGN_ID = "nmi_assign_id"
CONF_ADDRESS = "address"
CONF_DESCRIPTION = "description"
CONF_COMPANY = "company"
CONF_SIGNED_IN_AT = "signed_in_at"
CONF_KEEPALIVE_MINUTES = "keepalive_minutes"
CONF_MFA_METHOD = "mfa_method"
CONF_CODE = "code"
CONF_RESEND = "resend"

# Cookies not worth persisting: analytics identifiers, and the CSRF token
# cookie, which is re-read from each page load.
IGNORED_COOKIE_PREFIXES = ("_ga", "_gcl", "_fbp", "__Host-ERIC")

STORAGE_VERSION = 1
SESSION_STORAGE_KEY = f"{DOMAIN}.session"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-AU,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}
