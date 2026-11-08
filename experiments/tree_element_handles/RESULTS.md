# Device Results: 2026-10-11

## Outcome

The prototype returns registered WDA UUIDs with the initial compact tree.
Three fresh observations followed by direct UUID clicks opened the intended
WeChat conversation, confirmed from screenshots. No `/elements` request was
used in these trials. Three coordinate clicks opened the same conversation.
No messages were typed or sent.

This is an isolated native WDA experiment on `codex/tree-element-handles`, not
a production MCP feature or a published plugin release. Existing worktree
changes were preserved. The pinned original WDA checkout remains unmodified.

## Successful Measurements

Both approaches used the same experimental WDA runner, device, session settings
and visible target Cell. Coordinates used the center of its observed rectangle.
The UUID route additionally checked live hittability before clicking.

| Trial | Tree HTTP (s) | Registration (ms) | Hittable (s) | Native Click (s) | Outcome |
| --- | ---: | ---: | ---: | ---: | --- |
| UUID 1 | 11.1448 | 1.174 | 2.0625 | 4.2932 | Intended chat opened |
| UUID 2 | 11.1901 | 1.095 | 2.1906 | 4.3112 | Intended chat opened |
| UUID 3 | 6.0972 | 1.072 | 2.1001 | 4.2556 | Intended chat opened |
| Coordinate 1 | 11.2692 | Disabled | Not requested | 18.7669 | Intended chat opened |
| Coordinate 2 | 11.1914 | Disabled | Not requested | 18.7558 | Intended chat opened |
| Coordinate 3 | 18.4839 | Disabled | Not requested | 14.3148 | Intended chat opened |

Native clicks are endpoint timings only: `/element/:uuid/click` versus
`/wda/tap`. Tree acquisition, model time and post-click screenshots are excluded.
Coordinate 3 also paid 11.5785 seconds for a separate pre-tap `window/size`
request; that cost is not included in its 14.3148-second native click.

- Median native click: UUID 4.2932 s, coordinate 18.7558 s; 77.1% less time.
- Median UUID hittable + click: 6.3557 s; 66.1% less than coordinate native tap.
- Each observation output 200 nodes and registered 147 unique UUIDs when enabled.
- Registration added zero AX snapshot requests in the audited device logs.
- UUID native click requested 2 AX snapshots; coordinate native tap requested 5.
- UUID hittability was a separate 1-AX check. Initial tree was another 1 AX.

These are small, nonrandomized, same-page samples. The coordinate series followed
the UUID/control series; background chat updates and snapshot latency varied.
Do not interpret these percentages as a general or end-to-end speed guarantee.

## Why It Helps

The successful implementation constructs a lazy descendant query with
`fb_uid == capturedUUID`, without evaluating it during registration, then stores
the known UUID in WDA's existing session element cache. A later live use resolves
that query. The existing native click staleness check is unchanged.

The initial tree already contains AX identities, so registration does not need
another selector lookup or an `XCUIElement.fb_uid` call per node. UUID clicks
also avoid the application-frame read used to convert coordinate taps.
Coordinate logs show this frame read plus additional AX work during synthesis.

## Failed Approaches And Controls

The first private-helper prototype registered handles quickly but live use
failed with stale-reference errors before any click. Application-only query
scope was wrong. Expanding that helper's query to descendants changed the
binding input but still failed. These results are not successful benchmarks.
The final patch uses the lazy UID predicate, not the private binding helper.

The lookup control read the same compact source with registration disabled,
then used ordinary `/elements` lookup. Both controls failed before clicking:

| Control | Tree HTTP (s) | Lookup HTTP (s) | Outcome |
| --- | ---: | ---: | --- |
| 1 | 11.4125 | 37.2403 | Stale reference during cache-ID identity binding |
| 2 | 16.4681 | 36.9595 | Same failure, no click |

Each failed lookup requested four AX snapshots: the broad query, then a
cache-ID resolution with two identity-binding retries. They cannot establish
a successful ordinary-lookup speed ratio. Earlier independent ordinary lookup
on the same visible target succeeded in 12.474 seconds.

## Remaining Work

Complete application snapshots still took 6.015-18.421 seconds across the final
trials. The endpoint obtains the complete snapshot before filtering; this
experiment does not solve expensive full-tree acquisition or guarantee reads
fit a production 15-second HTTP timeout.

Session settings remained depth 50, child limit 2147483647, idle timeout 0 and
animation cool-off 0. Snapshot traversal did not request native visibility.
The driver alone used a diagnostic 60-second HTTP timeout; production settings
were not changed.

Only one selected Cell was verified, not all 147 registered handles. Scroll,
long delay, navigation, background refresh, cache eviction and different apps
need validation before release. Flat per-node viewport filtering can retain
misleading child rectangles from offscreen rows; it is not hittability proof.
Production integration also needs observation/app invalidation and public tap
schema support. UUIDs must not be treated as permanent IDs.

Ten driver unit tests pass; the native build and patch-application check pass.
Raw trees, screenshots and logs contain private phone data and remain outside
the repository in permission-restricted experiment directories.

## Cleanup

Experimental runner and forward processes were stopped. The original cached
WDA was restarted and a new shared session passed READY. The diagnostic driver
was closed; the phone was left on the WeChat conversation list. No release was
published and the production configuration/build marker was preserved.
