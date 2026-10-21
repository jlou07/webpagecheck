# webpagecheck

Monitors web pages on a schedule and sends you an email when something changes.
A web interface lets you add pages and configure notifications. The optional
AI mode evaluates a condition written in plain language and returns a confidence
score.

## Azure quick start

The deployment script requires **Windows, PowerShell and Azure CLI**. You do
not need Docker locally: the script builds the image in Azure Container Registry.
The script uses Windows ACLs and cannot run unchanged on Linux, macOS, WSL or
Cloud Shell.

You need an Azure subscription and permission to create resources and assign
roles to the managed identity. Contributor alone does not grant
`Microsoft.Authorization/roleAssignments/write`. Resources incur charges:
Container Apps keeps at least one replica running, alongside ACR, Azure Files,
Log Analytics and, depending on your options, email and Azure OpenAI usage.

Clone the repository, open a PowerShell terminal in its directory, then run:

```powershell
az login
$subscriptionId = Read-Host 'Your Azure subscription ID'
az account set --subscription $subscriptionId
az account show --query "{Subscription:name,Id:id}" --output table
az extension add --name containerapp --upgrade

$resourceGroup = 'rg-webpagecheck-demo'
$namePrefix = 'wpcdemo'
$appName = "${namePrefix}-web"

.\deploy.ps1 -ResourceGroup $resourceGroup -NamePrefix $namePrefix `
    -DeployOpenAi:$false
```

This configuration enables email notifications but disables AI, so you can start
without a model quota. To enable AI on the first deployment, omit
`-DeployOpenAi:$false` after checking the model, its version and your subscription
quotas. Choose your options before running the script; do not run each variant
in sequence.

Open the HTTPS URL printed when deployment finishes. See the deployment section
below to retrieve the initial password in a private terminal. Set your
credentials under **Site login**, configure the sender address printed by the
script and add a recipient address you control.

Send one real test email, then disable sending while experimenting. Add
`https://example.com/` in **Keyword** mode with the keyword `Example Domain`.
**Scan** analyzes without sending an email or changing the watch state;
**Check now** updates the state and may send an alert. Simulated alerts are not
replayed when you enable sending again.

### Deployment status and validation

A **Deploy to Azure** button and a public prebuilt image are not available yet.
No redistribution license has been selected. A complete fresh deployment in a
separate subscription still needs validation. Local tests and Bicep compilation
do not prove regional service availability or email delivery.

GitHub Actions runs application, security and browser tests on Windows, compiles
Bicep and checks PowerShell syntax. It uses no Azure connection, provisions no
resources and publishes no image. Its GitHub token has read-only content access.

Local data, Python environments, secrets and deployment traces are excluded
from Git. The Docker build context includes only the required runtime files.

Keep the Chromium sandbox enabled by default. Diagnose host compatibility
before explicitly disabling it. The interface uses Basic Auth over HTTPS, not
Entra ID SSO with MFA. Audit the image, operating system and browser for current
CVEs before exposing a sensitive deployment.

## Monitoring modes

- **Change**: alerts when the text of the page changes (the first check is the reference).
- **Keyword**: alerts when a keyword appears on the page, and again when it disappears.
- **AI**: you describe the expected change (for example "Registrations for MVP Summit are open"). The Azure OpenAI model returns a probability from 0 to 100 %. An alert is sent once, when the probability crosses the threshold (70 % by default). Another alert is sent only after the probability drops below the threshold and rises above it again.

Keyword and AI modes read the page as rendered by Chromium (after JavaScript runs), so text injected by scripts is seen. If Chromium is unavailable, they fall back to the raw HTML. Change mode always hashes the raw HTML so that a browser failure never triggers a false change alert.

## Interface

- **Configuration** (top, collapsible with the chevron at its top right): email notifications (sender, recipients, test, sending on or off) and the site login. A coloured badge shows the sending mode: green when alerts are emailed, amber in test mode (alerts are only simulated), red when email is not configured.
- **Watched pages**: one row per page with its mode, interval, last check, clickable last-check capture, state and last alert (status, time and title). Click a thumbnail in **Last capture** to open the full-size image in a new tab. A capture is taken on every real check (scheduled or **Check now**), even when no alert is triggered or email is not configured. Until the next check, existing pages show **Capture unavailable**; new pages show **Not checked yet**. If a capture fails, the previous image is removed rather than shown as the latest check. Hover over an icon to see its label:
  - Check mark, **Check now**: runs a real check. It updates the state and sends an alert only when the condition changes (see Monitoring modes). When sending is disabled, the alert is simulated.
  - Magnifier, **Scan**: analysis only. It shows the page capture, the AI result (or the keyword or change result) and the email that would be sent. Nothing is sent and the alert state does not change.
  - Pencil, **Edit**; pause or play, **Pause monitoring** / **Resume monitoring**; trash, **Delete**.
- **Scan** card: the result of the last scan, shown below the table.
- **Alerts (preview)** (bottom, collapsible with the chevron at its top right): the latest alerts with their status (`sent`, `failed`, `simulated`, `not sent`), content and capture.
- The interface uses a lightweight CSS gradient instead of a background image and follows the system's light or dark appearance. On narrow screens, the watched-pages table scrolls horizontally.

Only the latest check capture is kept per page, in the data folder's `checks` subfolder. It survives restarts and is deleted when the page is deleted or its check state is reset by editing its target or monitoring condition. Captures use the same sign-in protection as the interface. A **Scan** does not replace this capture or change the last-check state.

## Configuration

**Email notifications**

- **Sender address**: the Azure Communication Services sender address, shown at the end of the deployment, or `DoNotReply@your-domain` once a custom domain is linked (see below).
- **Recipient addresses**: one or more addresses, separated by commas.
- **Extra email addresses** (per page): these receive that page's alerts in addition to the main recipients.
- **Send test notification**: sends a test email to every recipient.
- **Disable sending (test mode)**: stops sending. Alerts are simulated: they appear in "Alerts (preview)" with their content and capture, and nothing is sent. Click again to enable sending.

**Site login**

- Sets the username and password (at least 8 characters) needed to open the interface. The browser asks for them when it next needs to sign in.
- Until a login is saved, the `UI_PASSWORD` value is used, with any username.
- To recover a forgotten login, remove the `login` entry from `config.json` in the data folder. On Azure, that folder is the `webpagecheck-data` file share. The `UI_PASSWORD` value applies again.

## Email limits

Email is sent with Azure Communication Services. The default Azure domain (`*.azurecomm.net`) allows 5 emails per minute and 10 per hour per subscription, and these limits cannot be raised. A custom verified domain allows higher limits through a quota request. When the limit is reached, the interface shows a clear message. Keep test mode on while you experiment.

Alert emails start with `URGENT: ` in the subject and are sent with high priority. They include a capture of the page when one can be taken. The capture covers the whole page, up to 4000 px high.

## Run locally

```powershell
$env:UI_PASSWORD = "choose-a-password"
$env:DATA_DIR = "data"
python -m pip install --require-hashes -r requirements.txt
python -m playwright install chromium
python app.py
```

Open http://localhost:8080. Without Chromium, checks and alerts still work, but captures are unavailable.

Run the application tests with `python -m unittest test_app`. To also exercise the
dashboard in Chromium (thumbnail links, refresh, scan isolation, image errors and
mobile/dark layouts):

```powershell
$env:WPC_BROWSER_TESTS = "1"
python -m unittest test_app test_dashboard
```

## Network security

The HTTP collector requests English content, and Chromium uses the `en-US`
locale. Watch names, keywords and AI conditions remain user-supplied and may use
any language. Existing legacy alert status values are still recognized for
data migration.

Only public Internet HTTP/HTTPS destinations are supported. Loopback, private,
link-local, reserved and multicast addresses are rejected, including IPv6 and
alternative IPv4 representations. URL credentials and scoped IPv6 addresses are
not accepted. Azure's platform-only address `168.63.129.16` is also blocked.
Existing watches are checked again when they run.

Both collectors use an authenticated, temporary loopback proxy. It resolves and
checks every outgoing destination, then connects to the checked numeric address
without a second DNS lookup. Mixed public/private DNS answers are rejected.
Redirections and Chromium subresources pass through the same policy; environment
proxy settings and `NO_PROXY` cannot bypass it. HTTPS certificate checks remain
enabled. Chromium's sandbox is enabled by default; service workers, downloads, non-proxied
WebRTC UDP and QUIC are disabled. The loopback proxy requires a random
credential that is never forwarded to monitored sites.

This is application-level egress protection, not a replacement for container and
network isolation. Keep Chromium and the base image patched. The container host
must support Chromium sandboxing; if it does not, captures fail explicitly in
the logs and alerts continue without a capture.

On a host that does not support Chromium sandboxing,
an operator can explicitly set `CHROMIUM_SANDBOX=false` after accepting the
reduced browser isolation. This restores the previous capture behavior; the
public-only proxy, TLS checks, non-root user and all other network restrictions
remain in force. There is no automatic fallback: a failed sandbox launch never
silently switches to this mode. A compromised browser would still share the
application's container and secrets, so prefer an isolated capture service for
stronger protection. Invalid setting values prevent application startup.

Dependencies are fully pinned with hashes in `requirements.txt`; edit
`requirements.in` and regenerate the lock with:

```powershell
uv pip compile --universal --python-version 3.12 --generate-hashes requirements.in --output-file requirements.txt
```

Install with `python -m pip install --require-hashes -r requirements.txt`. Audit
the built container separately, including its OS packages and bundled Chromium.

Environment variables:

| Variable | Purpose |
|---|---|
| `UI_PASSWORD` | Required. Interface password, used until a login is saved in the app. |
| `DATA_DIR` | Folder for `config.json`, latest-check captures and alert captures (default `data`). |
| `PORT` | Listening port (default `8080`). |
| `CHROMIUM_SANDBOX` | `true` (default) or `false`. Explicit browser compatibility option; disabling reduces browser isolation but does not disable anti-SSRF. |
| `ACS_CONNECTION_STRING` | Optional. Azure Communication Services connection string for email. |
| `AZURE_OPENAI_ENDPOINT` | Optional. Enables AI mode. |
| `AZURE_OPENAI_DEPLOYMENT` | Model deployment name (required with the endpoint). |
| `AZURE_OPENAI_API_KEY` | Optional. Otherwise Entra ID authentication (`DefaultAzureCredential`) is used. |

## Example: Example Domain

The **Add page** form is pre-filled with this example:

- Name: `Example Domain`
- URL: `https://example.com/`
- Mode: AI
- Condition: `The page says this domain is for documentation examples`
- Threshold: 70 %
- Interval: 60 minutes

## Deploy to Azure Container Apps

Prerequisite: Azure CLI signed in (`az login`) on the right subscription.

```powershell
.\deploy.ps1 -ResourceGroup rg-webpagecheck
```

The script:

1. creates the resource group;
2. deploys `infra/main.bicep`: Azure Container Registry, managed identity, Azure Files storage mounted on `/data`, Container Apps environment, Log Analytics, Azure OpenAI and Azure Communication Services Email (each can be skipped with `-DeployOpenAi:$false` / `-DeployEmail:$false`);
3. builds the image with `az acr build`;
4. deploys the Container App and prints its URL and the sender email address.

The password is never printed automatically or included in Azure CLI process
arguments. It is passed through a temporary JSON parameter file restricted to
the current Windows account, removed even on failure. If you did not pass
`-UiPassword`, recover the generated value explicitly in a private operator
terminal (never in shared CI logs):

```powershell
az containerapp secret list --resource-group $resourceGroup --name $appName --show-values --query "[?name=='ui-password'].value | [0]" --output tsv
```

The variables above come from the quick start. The `--show-values` option
explicitly displays the initial password: do not record, screenshot or transcribe
its output. This deployment script uses Windows ACLs to protect the temporary file.
Running it again without `-UiPassword` generates a new initial password. A login
saved in the application still takes precedence over that initial password.

Useful options: `-Location`, `-OpenAiLocation` (default `swedencentral`), `-DeployOpenAi:$false`, `-DeployEmail:$false`, `-CustomEmailDomain`.

If the host blocks Chromium's sandbox and you explicitly accept the risk, add
`-ChromiumSandbox:$false` to the deployment command (Bicep parameter
`chromiumSandbox=false`). Keep this option on subsequent deployments to retain
compatibility. It does not change the container's user or host security policy.

To send from your own domain, add and verify it in the Email Communication Service (SPF and DKIM records), then link it to the Communication Services resource. Azure refuses sends from an unlinked domain (`DomainNotLinked`). Pass the domain with `-CustomEmailDomain page.example.com`, and pass it on every run: otherwise the deployment resets the links to the Azure domain only.

Notes:

- The app runs with **one replica** (`minReplicas = maxReplicas = 1`) because storage is a single JSON file.
- The default AI model is `gpt-4.1-mini` (`openAiModelName` and `openAiModelVersion` in the Bicep file). Check that it is available in the chosen region.
- The UI password and the ACS connection string are stored as Container App secrets.

## Files

- `app.py`: Flask API, login check, entry point (waitress).
- `monitor.py`: page fetching, text extraction, check engine, scan, notifications.
- `public_network.py`: shared public-only egress proxy, DNS/address checks and redirect policy.
- `store.py`: JSON persistence (atomic writes), validation, login hashing.
- `ai.py`: Azure OpenAI evaluator.
- `static/dashboard.html`, `static/dashboard.js`: web interface.
- `infra/main.bicep`: Azure infrastructure.
- `deploy.ps1`: deployment script.
- `requirements.in`, `requirements.txt`: direct requirements/security floors and hashed universal lock.
- `test_app.py`, `test_security.py`: application and security regression tests.

## Regression tests

```powershell
python -m unittest test_app test_security test_dashboard
```

The security tests cover public/private URL validation, alternative IP forms,
mixed DNS answers, connection-time DNS rebinding, address pinning, redirections,
proxy authentication, `NO_PROXY` bypass attempts, TLS certificate validation, response limits, API errors,
Chromium configuration and Windows deployment secret handling. Test origins and
private destinations are local fixtures; public transport is simulated. No Azure
deployment, model invocation or email is performed.

To also run real Chromium against these fixtures:

```powershell
$env:WPC_BROWSER_TESTS = "1"
python -m unittest test_app test_security test_dashboard
```

These tests verify that private images, frames, fetches, WebSockets and redirected
pages never reach the local fixture. The browser tests require the installed
Playwright Chromium and a host supporting its sandbox. Windows-only deployment
tests mock Azure CLI and check output redaction, file ACLs and cleanup on success
and failure. Audit the image before deploying; these tests do not prove the
security of a production host.
