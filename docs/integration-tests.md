# Disposable backend integration test

`tests/integration/live_roundtrip.py` is a standalone, destructive opt-in test,
not part of the mocked pytest suite. It registers a new user, creates an API
key and writes inventory. Run it **only against a new throwaway local backend**;
never use an existing database, a production snapshot or real credentials.
The loopback URL and explicit disposable marker are safety checks, not proof
that the target is disposable. The operator must ensure that independently.
The script disables proxy use and redirects, and never accepts remote hosts.

The fixture targets the extended Homebox entities API, including location
inheritance and `syncChildEntityLocations`. An arbitrary upstream image is not
a substitute. The following example builds an appropriate local backend source
checkout. It needs that checkout's required Go version and cached dependencies;
it doesn't download or install dependencies. Inspect the source revision and
dirty state before using it. All paths below must be disposable scratch paths.

```sh
# From homebox-mcp; BACKEND points to the intended backend source checkout.
: "${TMPDIR:?set TMPDIR to a private disposable scratch directory}"
: "${BACKEND:?set BACKEND to the verified backend source checkout}"
git -C "$BACKEND" rev-parse HEAD
git -C "$BACKEND" status --short
(cd "$BACKEND/backend" && GOPROXY=off go build -o "$TMPDIR/homebox-api" ./app/api)
mkdir -p "$TMPDIR/homebox-live"
PORT=$(python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
(cd "$TMPDIR/homebox-live" && exec env -i PATH="$PATH" HOME="$TMPDIR" \
  HBOX_WEB_HOST=127.0.0.1 HBOX_WEB_PORT="$PORT" \
  HBOX_AUTH_API_KEY_PEPPER=disposable-test-pepper-000000000000000000 \
  HBOX_OPTIONS_CHECK_GITHUB_RELEASE=false HBOX_OPTIONS_ALLOW_ANALYTICS=false \
  "$TMPDIR/homebox-api") >"$TMPDIR/homebox-live.log" 2>&1 &
PID=$!
# Confirm the log reports a successful migration and the intended listener;
# stop immediately if it does not. Port selection and bind are not atomic.
# Then, using an already provisioned project Python environment:
HBOX_INTEGRATION_URL="http://127.0.0.1:$PORT" HBOX_INTEGRATION_DISPOSABLE=yes \
  python tests/integration/live_roundtrip.py
kill "$PID"
wait "$PID" 2>/dev/null || true
```

Use a fresh scratch directory on each run and stop the process even if the test
fails. The test does not delete the registered user or inventory; discard the
entire temporary backend storage after stopping it. The default backend data path
is relative to its working directory. The fixture must not use the project
`.env` for authentication: it supplies its own temporary API key.
