# myEnergy portal protocol

Reverse-engineered from two browser HAR captures (Edge, 1 Nov 2026). There is
no documented API, and anything here can change when CitiPower/Powercor or
Salesforce ship a release. Values shown are placeholders.

Host: `https://myenergy.powercor.com.au`. Salesforce Experience Cloud site
(app `siteforce:communityApp`), shared with United Energy (brand `UE`).
CitiPower/Powercor brand is `VPN`.

## Aura requests

`POST /s/sfsites/aura?r=<n>&aura.ApexAction.execute=1`, form-encoded:

| field | value |
|---|---|
| `message` | `{"actions":[{"id":"1;a","descriptor":"aura://ApexActionController/ACTION$execute","callingDescriptor":"UNKNOWN","params":{"namespace":"","classname":C,"method":M,"params":{…},"cacheable":false,"isContinuation":false}}]}` |
| `aura.context` | `{"mode":"PROD","fwuid":…,"app":"siteforce:communityApp","loaded":{…},"dn":[],"globals":{},"uad":true}` |
| `aura.pageURI` | e.g. `/s/energy-usage` |
| `aura.token` | CSRF token (see below) |

- `fwuid` and `loaded` come from the URL-encoded JSON in a page's
  `/s/sfsites/l/{…}/resources.js` script URL. The `app.js` URL lacks `fwuid`,
  so pick the one that has it.
- CSRF token: signed-in pages include `"eikoocnekot":"__Host-ERIC_PROD…"`
  naming the cookie that holds the token. **The login page has no token and the
  browser sends the literal string `null`.**
- Response: `actions[0].returnValue.returnValue` holds the payload, usually
  `{"data": …, "isSuccess": true}`. A dead session comes back as
  `event.descriptor` containing `invalidSession`. Bodies may be wrapped in
  `while(1);` or `*/ … /*ERROR*/`.

## Sign-in

1. `GET /s/login/` (302 from `/s/login?…`): login app `siteforce:loginApp2`.
2. Aura `LightningLoginFormController.communityLogin`
   `{"username": "<email>.VPN", "password": "…", "startUrl": "/s/"}`,
   pageURI `/s/login/?ec=302&startURL=%2Fs%2F`.
   Success: `returnValue.returnValue = {"data":{"loginUrl":"https://…/secur/frontdoor.jsp?allp=1&appkp=1&apv=1&cshc=…&refURL=…&retURL=%2Fs%2F&sid=<SESSION>&untethered="},"isSuccess":true}`.
   **Failure shape not yet captured.**
   No reCAPTCHA token is sent, even though `api.js?render=<key>` loads on the page.
3. `GET frontdoor.jsp…`: HTML whose script does
   `window.location.replace("https://…/apex/MFA_Enabler?retURL=%2Fs%2F")`.
4. `GET /apex/MFA_Enabler?retURL=%2Fs%2F`: Visualforce page. Contains:
   - `Visualforce.remoting.Manager.add(new $VFRM.RemotingProviderImpl({"vf":{"vid":"066…",…},"actions":{"MFAEnablerController":{"ms":[{"name":"initiateMFA","len":1,"ns":"","ver":60.0,"csrf":"…","authorization":"…"},{"name":"verifyMFA","len":4,…},{"name":"verifyMFARegister","len":2,…}],"prm":1}},"service":"apexremote"}))`
   - JS vars: `brand = "VPN"`, `userMobile = "04** *** nnn"`, `hasSMSOption = "true"`.
   - Radio inputs `mfa-option-radio` = `email` | `sms`; label "Send an email to <email>".
   - Form `j_id0:j_id29` (action `/MFA_Enabler`) and a view-state span outside it
     with `com.salesforce.visualforce.ViewState`, `…ViewStateVersion`,
     `…ViewStateMAC`, `…ViewStateCSRF`.
   - `redirectToHome()` → `jsfcljs(document.forms['j_id0:j_id29'],'j_id0:j_id29:j_id31,j_id0:j_id29:j_id31','')`.
5. Remoting `POST /apexremote`, JSON, headers `X-Requested-With: XMLHttpRequest`,
   `X-User-Agent: Visualforce-Remoting`, `Referer: …/apex/MFA_Enabler?retURL=%2Fs%2F`:
   ```json
   {"action":"MFAEnablerController","method":"initiateMFA","data":["sms"],
    "type":"rpc","tid":2,
    "ctx":{"csrf":"…","vid":"066…","ns":"","ver":60,"authorization":"…"}}
   ```
   → `[{"statusCode":200,"type":"rpc","tid":2,"action":"MFAEnablerController","method":"initiateMFA","result":{"isRegistration":false,"success":true,"verificationIdentifier":"…"}}]`
6. `verifyMFA` data: `["<6 digits>", "<verificationIdentifier>", false, "sms"]`
   → `result: {"success": true}`. On failure the page shows `result.message`
   (**failure shape not yet captured**).
7. `POST /MFA_Enabler` form-encoded:
   `j_id0:j_id29=j_id0:j_id29`, `j_id0:j_id29:j_id31=j_id0:j_id29:j_id31`, plus the
   four view-state fields → HTML that redirects to `/s/` via
   `SfdcApp.projectOneNavigator.handleRedirect('/s/')` or `window.location.replace('/s/')`.
8. `GET /s/`: signed-in community page with descriptor and `eikoocnekot`.

## Data calls (signed in)

| Class.method | params | returns (`data`) |
|---|---|---|
| `EnergyUsageDetailPageController.getEnergyUsageDetailWrapper` | `{"nmiAssignId": "a1B…", "noOfLimit": 50}` (`nmiAssignId` optional) | `nmiAssignmentsForEnergyUsage[]`: `Id`, `Description__c`, `Status__c`, `NMI__r{Name (11-digit NMI), Id, Display_Address__c, Customer_Type__c}` |
| `EnergyInformationController.retrieveAccountHolderInfo` | `{"nmi": "<11-digit>"}` | `companyName` ("Citipower"), `startDate`/`endDate` (`YYYY-MM-DDT00:00:00.000Z`), `hasReportAccess`, `errorMessage` |
| `EnergyInformationController.getMeterData` | `{"nmi", "formatType": "Customer Access NEM12", "startDate": "YYYY-MM-DD", "endDate", "company": "Citipower", "jobId": n}` | `body{formatType, jobId, nmi, status:"Success", nmiData: "<NEM12 text>"}` |
| `EnergyUsageDetailPageController.getNMIEventNotificationData` | `{"nmiId": "a1C…"}` | outage notices (unused) |

- The browser requests ~92-day windows. We use ≤90. Each call takes 1–3 s.
- `formatType: "MDPP Interval Summary"` returns monthly totals as CSV
  (`nmi,meter,kWh,quality,dd/mm/yyyy,dd/mm/yyyy,total,…`). It's useful for
  cross-checking.

## NEM12 as served

```
100,NEM12,202611012230,XXX1,
200,6102000000,E1,E1,E1,,<meter>,KWH,5,
300,20261031,<288 values>,A,,,20261101010010,
900
```

- The 200 record's NMI is the 10-digit form (no checksum digit).
- 5-minute intervals (288/day). Quality flag after the values (`A` actual). The
  update timestamp is the 4th field after the quality flag.
- Times are AEST market time.

## Session

- `GET /_nc_external/system/security/session/SessionTimeServlet?buster=<ms>` →
  `while(1);\n{"sp":7142,"sr":7172}`. `sr` is taken to be seconds remaining.
- Aura `SessionTimeoutWarn.getSessionTime` → `forceLogout: true`,
  logout URL `/secur/logout.jsp?…`.
- Idle timeout ≈ 2 h. Salesforce extends a session on a request made in its
  second half, so keep-alive pings must come at intervals under 60 min.
