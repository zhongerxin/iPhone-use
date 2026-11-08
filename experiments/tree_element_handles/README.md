# Snapshot Element Handles Experiment

Status: native build succeeded; three fresh-tree UUID clicks opened the intended
WeChat conversation on 2026-10-11. See [Results](RESULTS.md) for measured timings,
failed binding approaches and remaining limitations.

## Scope

`wda.patch` applies to Appium WebDriverAgent 16.14.0, commit
`d17782422d55ff1e5e0ceb74eb1fd509cc0c35b6`. Apply it only to an isolated clone.
It adds a session-bound GET `/wda/sourceWithHandles?max_nodes=200`:

- Acquire one standard application snapshot using existing depth/child settings.
- Traverse snapshot-local text, type, enabled and geometry; filter by viewport.
- Extract `native_uid` without invoking `XCUIElement.fb_uid`.
- Construct lazy descendant queries matching the snapshot's `fb_uid`, using
  `matchingPredicate:` followed by `element`, without evaluating the query.
- Register the snapshot's known UUID directly in WDA's existing session cache.
- Return `element_id` only for registered handles; default budget 200, maximum 500.
- Keep existing `/element/:uuid/click` and its staleness check unchanged.

`register_handles=false` keeps the same snapshot traversal and UID extraction
but skips native handle construction. Server timings separate snapshot,
filtering and registration. Runner markers bracket these stages for AX auditing.
Device logs show no additional AX requests during registration. Registration
does not prove every returned handle is usable; only the selected Cell has been
tested with live hittability checks and clicks.

This is an opt-in diagnostic endpoint, **not a published plugin feature**.
It is not connected to the public MCP tap schema, does not provide observation
generation invalidation and does not prove listed nodes are hittable. Do not
ship it before validating scrolling, background refresh and stale handles.

## Driver

`driver.py` imports the installed plugin Runtime, reuses the existing shared
state, session and operation lock, and uses a 60-second diagnostic HTTP timeout
only in its own process. It never changes the production timeout or setup
validation. Build and run with explicit paths:

```sh
python3 experiments/tree_element_handles/driver.py \
  --plugin-root /absolute/path/to/installed/iphone-use \
  --native-source /absolute/path/to/isolated/patched/WebDriverAgent \
  --output /absolute/private/output/directory --build
```

Omit `--build` for the interactive JSON-lines driver. First obtain setup status
and READY. The `switch` operation only stops a uniquely verified owned original
service, starts the experimental build at the same endpoint and preserves the
original configuration and build marker. Obtain diagnostic READY after the
switch. `restore` stops experiment children and starts the original cached
build. Exiting also attempts restoration once if an experiment is still active.
If startup fails, inspect the exact job/log rather than repeatedly restarting.

For each `trial`, choose `route="ordinary"`, `route="lookup-control"` or
`route="handles"` and a target
label substring identifying exactly one visible Cell. Both routes explicitly
check hittability and issue at most one UUID click, then capture a screenshot.
The handles route never performs `/elements` lookup. The ordinary route reads
the unmodified XML source and performs the existing selector lookup.
The lookup control uses the same compact-source endpoint with registration
disabled, then performs the existing selector lookup. This isolates lookup
cost from XML serialization differences. After an error, obtain and inspect a
fresh screenshot before any further navigation or target action.

Suggested successful-device protocol: warm both routes, run at least three
alternating pairs on the same visible Cell, inspect every result, return to the
list between trials, and retain failures. Separately test refresh/scroll/delay
staleness. Do not replay an uncertain click or automatically fall back to a
cached coordinate.

Raw observations, screenshots and logs remain private, outside the repository;
the driver does not print conversation content. Do not publish raw phone data.

## Verification

```sh
python3 -m unittest discover -s experiments/tree_element_handles -p test_driver.py -v
```

Ten tests cover target selection, isolated build overrides, request sequences,
no redundant lookup and refusal to click a non-hittable/nonunique target.
These are driver unit tests, not evidence of native handle correctness.
