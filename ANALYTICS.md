# iPhone Use analytics

PostHog organization: `iPhone-use`. Project: `653022` (US Cloud).
Project time zone: `Asia/Shanghai`.
[Usage and reliability dashboard](https://us.posthog.com/project/653022/dashboard/2185876).

This follows Cowart's server-side delivery approach, with a Python standard-library
client in `server/analytics.py`. The write-only project token in
`server/posthog.json` ships with the plugin; a personal API key is never required
for capture. See the [PostHog capture API](https://posthog.com/docs/api/capture).

## Collection and controls

Analytics is enabled by default. It records a random installation UUID saved
privately in the plugin's state directory, a new random process/session UUID,
plugin version, platform, event timestamp and the bounded fields below.
`analytics_schema_version` is `2` from plugin version `0.3.13` onward. MCP
initialization, pings and tool/resource discovery do not create analytics events
or an analytics identity. Usage starts only when a tracked tool finishes or raises.
The installation UUID remains stable through upgrades that reuse the state directory.
It represents an installation, not a known person, phone or chat. PostHog person profiles and GeoIP enrichment are disabled. There is no automatic
frontend capture or phone recording. GA4 receives the same anonymous events
directly via Measurement Protocol; Google receives the network connection and
may derive coarse geography from the request IP. No IP override or phone location
is included. Advertising data use and ad personalization are explicitly denied.

Phone text, selectors, accessibility trees, screenshots, clipboard data,
UDIDs, signing teams, filesystem paths, MCP request IDs and exception
messages are never sent. Only known tools, actions, states and error codes are
accepted; unknown errors become `other`.

Explicit launch targets are collected as `target_app_bundle_id` (validated bundle
ID, at most 100 characters) and `target_app_name` (bundled public catalog name,
falling back to the ID). Device-provided App names, installed-App inventories,
foreground observations and App contents are never uploaded. This additive
instrumentation starts with client version `0.3.16` and does not backfill history.

Set either environment variable on the MCP server and restart it to disable
analytics completely, including local analytics identity creation:

```sh
IPHONE_USE_ANALYTICS=0
# Or:
DO_NOT_TRACK=1
```

For Codex, add `IPHONE_USE_ANALYTICS = "0"` under
`[mcp_servers.iphone_use.env]` in `~/.codex/config.toml`. For a portable MCP JSON
configuration, add `"env": {"IPHONE_USE_ANALYTICS": "0"}` to the server entry.

`IPHONE_USE_POSTHOG_PROJECT_TOKEN` overrides the bundled token. An explicitly
empty value disables PostHog only; GA4 delivery remains independent. For local development, `server/posthog.local.json`
may contain `{"projectToken":"your-write-only-project-token"}`; this file is
ignored by Git and excluded from both install and source packages. Precedence:
environment override, local config, bundled config. Capture always uses US Cloud.

`IPHONE_USE_ANALYTICS_ENV` accepts `production` (default), `development` or `test`.
The dashboard only counts `production`. `sh scripts/check.sh` disables telemetry
for tests and subprocess package checks; analytics unit tests inject fake senders.
Run standalone test commands with `IPHONE_USE_ANALYTICS=0` as well.

## Events

| Event | When | Bounded properties |
| --- | --- | --- |
| `iphone_use_session_started` | Once per MCP process, on the first tracked Runtime call's result | Common properties only |
| `iphone_use_tool_called` | Exactly once after a known tool finishes or raises, for MCP and direct Runtime/CLI calls | `tool_name`, `outcome`, `error_code`, `duration_ms`; `batch_size` for batches; `ready_state` for READY; `setup_action`, `setup_state` for setup; `screen_action` for preview controls and gestures |
| `iphone_use_app_launched` | Once per successfully returned explicit launch, including completed launch substeps in a batch | `target_app_bundle_id`, `target_app_name`, `launch_source` (`direct` / `batch`), `foreground_verified` (`yes` / `no`) |

`pua_screen_frame` polling and `pua_screen_action(action="viewport")` geometry
recovery are excluded entirely, including failures; neither starts a usage session.
Preview tap/drag and toolbar actions still count once. Batch substeps do not emit
additional tool-call events. A process's first tracked call emits one usage-start
event plus its tool event; later calls emit only their tool event, with dedicated
launch events as described below.

Launch calls additionally emit `iphone_use_app_launched`. Direct launch tool
events also carry the target fields for failure diagnostics. Only the dedicated
event is used for App distributions, preventing double counting. Repeated
successful launches count separately. A completed launch still counts when a
later batch step fails or yields; failed and unexecuted launch steps do not count.
An activation followed by a failed verification/observation is excluded because
the launch did not successfully return. Default launches are optimistic;
`foreground_verified=yes` requires a successful foreground check. These counts
measure explicit launch operations, not foreground time or task completion.
App launches do not add another `active_use=yes` event in GA4. The existing
schema version stays at `2` so legacy READY/setup dashboard filters still match.

Versions before `0.3.13` also emitted `iphone_use_ready_result`,
`iphone_use_setup_result` and `iphone_use_screen_action`. These dedicated events
are no longer emitted; their fields are merged into the tool event. READY/setup
charts combine each legacy result event OR the matching tool event with
`analytics_schema_version=2`. The schema filter excludes old tool events, preventing
double counts while old and new clients coexist. READY UV uses a single OR series
so an installation using both versions on one day still counts once.

`success` means the tool returned without an error or an explicit incomplete
result. It does not promise that a user task was completed or independently
verified. READY false, incomplete input/batches and queued/running setup jobs
are `pending`; raised errors and explicit failed results are `error`. Setup
events track submission/status calls, not autonomous worker completion.

Durations are in milliseconds, from Runtime entry through return/error. They
include the phone operation lock and execution, exclude MCP worker queue waiting,
response serialization and analytics delivery. Duration is capped at one hour,
batch size at 20. Arbitrary argument and result objects are never serialized.

## Dashboard definitions

The eight baseline PostHog trend insights cover daily active installations,
used MCP processes, tool usage, READY results, tool failure rate, average/P95 latency, error
categories and setup actions. The exact queries are preserved in
`scripts/posthog-dashboard.json` in the source package.

Active installations are distinct random installation IDs with a successful
task tool or READY call, excluding doctor, setup, metrics and preview controls.
Used MCP processes count distinct `session_id` values on tool events, not
conversations or idle initializations. This also reconstructs historical usage
without counting legacy idle startup events. Failure rate includes pending
calls in its denominator; a displayed zero on a day with no calls is not evidence
of reliability. The dashboard is empty until production events arrive. Test and
development events remain available in Activity but are excluded from these
charts. These are project-specific definitions, not team-approved Data Catalog
metrics.

The existing dashboard now has 20 insights. App insights 17–20 append launch-count
and distinct-installation tables (with both name and bundle ID), a launch-share
pie and daily launches by App. Total-value installation tables deduplicate over
the selected period, not by summing daily installations. Names come from the
bundled catalog; the ID disambiguates Apps with the same name. The share chart
uses launch counts, so frequent launches contribute more than one-off launches.
An installation may use several Apps, so per-App installation counts are not
additive. All four use only `iphone_use_app_launched` and `environment=production`.
See PostHog's [breakdown documentation](https://posthog.com/docs/product-analytics/trends/breakdowns).

## Delivery limits

PostHog queues bounded events in memory on a daemon worker, at most 256 waiting
events and 20 events per request. Each event has a random UUID; one retry for a
network failure, HTTP 429 or HTTP 5xx reuses the same payload and UUID. Ordinary
4xx errors are not retried. Requests have a three-second socket timeout, and
shutdown waits at most 1.5 seconds. No events are written to disk for replay.
Queue overflow, extended outages or an immediate process exit can drop events;
this is usage telemetry, not an audit trail. Analytics failures never change
phone results, replay phone operations or write to MCP stdout.

## GA4

GA4 account: `Twox Studio` (`268077887`). Independent property: **iPhone Use**
(`559245431`), reporting time zone `Asia/Shanghai`, currency USD. Web stream:
**iPhone Use MCP** (`16139365697`), measurement ID `G-N56TECV0VT`.
The stream URL identifies this repository; no JavaScript tag is installed on
GitHub or inside the preview widget. Enhanced measurement is disabled.

The dedicated Measurement Protocol capture secret is intentionally bundled in
`server/ga4.json`, following the project owner's publishing choice. It allows
writing events to this stream; it is not a Google account or reporting API key.
It can be rotated in the stream's Measurement Protocol API secrets settings.
No Cowart stream, secret, event or dashboard is reused or changed.

`IPHONE_USE_GA4_MEASUREMENT_ID` and `IPHONE_USE_GA4_API_SECRET` override the
corresponding bundled values. An explicitly empty value for either disables
GA4 only. Local overrides can go in `server/ga4.local.json` (same keys as the
bundled config); that file is ignored and excluded from both packages. The global
`IPHONE_USE_ANALYTICS=0` and `DO_NOT_TRACK=1` controls disable **both** providers.

GA4 receives the exact same logical events as PostHog. The random installation
UUID is deterministically encoded as a numeric dotted `client_id`; a positive
numeric `session_id` is derived from the random process UUID. Neither identifier
comes from a phone, Google account or chat. The same event UUID is supplied as
`event_id` for diagnostics, without assuming that GA4 deduplicates arbitrary
custom events. Production events never enable `debug_mode`.

Each provider has its own bounded queue (256 events), worker and three-second
network timeout; one provider being blocked, full or unconfigured cannot prevent
the other from sending. Each GA4 request contains at most 20 events. **GA4 requests
are attempted once**, including on timeouts and HTTP errors, to avoid double
counts after an ambiguous response. PostHog retains its one UUID-preserving retry.
Both share the 1.5-second shutdown budget. Dropped events are never replayed.

`engagement_time_msec` reflects bounded tool execution time, with a minimum of
1 ms; it measures automation activity, not human screen attention. Use custom
`duration_ms` for latency and installation counts for usage. GA4's native sessions
can apply its own inactivity rules; use the `iphone_use_session_started` event
count for actual-used MCP processes (subject to delivery loss).

Event-scoped dimensions register tool, outcome, error, READY/setup/screen state,
environment, plugin version, host platform, `active_use`, `target_app_bundle_id`,
`target_app_name`, `launch_source` and `foreground_verified`. `active_use` is a
derived yes/no flag on existing GA4 events: yes means a successful task or READY
call, excluding doctor, setup, metrics and both preview tools. It creates no
extra events and uses the same active-installation definition as PostHog.
`duration_ms` and `batch_size` are
custom metrics. All usage/reliability reports filter `environment=production`;
verification events use `test`. Newly registered custom definitions can take
24–48 hours to become available in reports and do not backfill historical data.
GA4 starts collecting with this integration; it does not restore events already
lost to the PostHog billing cap.


### Published GA4 dashboards

All three reports are published to all property users in **Reports → Custom dashboards**:

- [iPhone Use｜使用概览](https://analytics.google.com/analytics/web/#/a268077887p559245431/reports/builder/16139658463?params=_u..nav%3Dmaui&ruid=2155EAED-6065-415E-A58D-379B0208AC98&collectionId=16139662949)
- [iPhone Use｜稳定性与接入](https://analytics.google.com/analytics/web/#/a268077887p559245431/reports/builder/16139666921?params=_u..nav%3Dmaui&ruid=52AE58B4-1D21-4B73-9C7E-3D7C4948FC8C&collectionId=16139662949)
- [iPhone Use｜App 使用分布](https://analytics.google.com/analytics/web/#/a268077887p559245431/reports/builder/16181877443?params=_u..nav%3Dmaui&ruid=A3E90B07-F923-43D4-B5B0-D0947D74DD06&collectionId=16139662949)

The App report includes two data tables (name and bundle ID), each showing launch
event count and distinct installations (`totalUsers`), plus launch-share, daily
launches, direct/batch source and foreground-verification charts. Every card
inherits `environment=production` AND `eventName=iphone_use_app_launched`.
The new dimensions use event slots 12–15. No historical App breakdown is available
before client version `0.3.16`; newly registered dimensions can take 24–48 hours
to populate. GA4's App-name table may combine equal public catalog names; use the
bundle-ID table to distinguish them.

| Usage overview | Reliability and onboarding |
| --- | --- |
| Daily distinct installations with `active_use=yes` | Tool outcome shares, including pending |
| Daily `iphone_use_session_started` count | Error categories and counts |
| Tool call counts by tool | Daily failed calls by tool |
| Daily tool outcomes | READY state distribution |
| Daily READY installations by state | Setup call counts by action |
| Daily setup installations by action | Setup state distribution |
| Installations by plugin version | Average tool duration by tool |
| Preview control counts, excluding polling | Daily installations encountering errors |

Tool charts filter `iphone_use_tool_called`; READY and setup charts further
filter the corresponding tool. Error charts filter `outcome=error`. All charts
inherit the production environment filter. Counts describe installations and
calls, not identified people, chats or completed user tasks. An installation can
appear in multiple versions or state/action groups, so group totals need not be
unique installations across the whole report.

The calculated metric **Average tool duration ms** is
`SUM(duration_ms) / eventCount`, used only on tool-call events. `duration_ms`
alone is a sum, not an average. P95 latency remains available on the PostHog
dashboard; these GA4 native dashboard cards do not implement percentiles.
Report defaults use the last 28 complete days, excluding today; select a date
range including today when checking newly collected data. Realtime can show
test events before custom definitions and standard reports finish processing.

The report configurations, dimension slot mapping, calculated metric and view
links are preserved in `scripts/ga4-dashboard.json` in the source package.
See Google's [Measurement Protocol reference](https://developers.google.com/analytics/devguides/collection/protocol/ga4/reference)
for the event and session parameter contract.
