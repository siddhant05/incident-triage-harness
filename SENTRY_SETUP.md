# Live Sentry Integration

This guide wires a real Sentry organization to the Incident Triage Harness running at
`https://incident-triage-harness.onrender.com`.

The harness exposes `POST /webhook/sentry?agent=<heuristic|gemini|claude>`. When the
`SENTRY_CLIENT_SECRET` env var is set on Render, every incoming request is
HMAC-SHA256 verified against the `Sentry-Hook-Signature` header. Requests with bad
signatures are rejected with `401`. Requests for resource types the harness does not
handle (anything other than `event_alert`, `issue`, `error`) are acked with `200
{"ignored": true, "resource": "..."}` so Sentry stops retrying.

## 1. Create the Internal Integration in Sentry

1. Sentry → **Settings** → **Custom Integrations** (under "Developer Settings") →
   **Create New Integration** → **Internal Integration**.
2. **Name**: `Incident Triage Harness`
3. **Webhook URL**:
   ```
   https://incident-triage-harness.onrender.com/webhook/sentry?agent=gemini
   ```
   (Swap `gemini` for `heuristic` or `claude` to change which agent runs.)
4. **Permissions**:
   - `Issue & Event`: **Read**
   - `Project`: **Read**
5. **Webhooks**: enable
   - `issue`
   - `error`
6. **Save Changes**.
7. Open the new integration → copy the **Client Secret**.

## 2. Set the secret on Render

1. Render dashboard → `incident-triage-harness` service → **Environment**.
2. Add env var:
   - Key: `SENTRY_CLIENT_SECRET`
   - Value: *(the client secret from step 1)*
3. **Save Changes** → wait for the service to redeploy.

Once set, unsigned or wrong-signature requests will return `401`. Until it's set the
harness logs a single warning and accepts any payload (useful for the demo curl flow).

## 3. Verify with Sentry's "Send Webhook" button

On the integration page Sentry has a **Send Webhook** test button. Click it and
check the Render logs — you should see a `200` and a new run via
`GET /runs`.

## 4. Verify with a real alert

Free / Team Sentry plans only fire `issue` webhooks roughly every ~10 minutes per
issue, so don't expect each new event to hit the harness. Trigger an alert from an
instrumented project and wait. To check delivery without waiting, use the curl example
below.

## 5. Curl with a valid HMAC (for demos)

The trick is that the body must be **byte-identical** to what you sign. Save the
payload to a file first, then read it back as the body:

```bash
SECRET='paste-your-client-secret-here'
PAYLOAD_FILE=demo/sample_payload.json

SIG=$(openssl dgst -sha256 -hmac "$SECRET" -hex < "$PAYLOAD_FILE" \
        | awk '{print $NF}')

curl -X POST 'https://incident-triage-harness.onrender.com/webhook/sentry?agent=gemini' \
  -H 'content-type: application/json' \
  -H "Sentry-Hook-Resource: event_alert" \
  -H "Sentry-Hook-Signature: $SIG" \
  --data-binary @"$PAYLOAD_FILE"
```

If `SENTRY_CLIENT_SECRET` is unset on the server (dev), you can skip both headers and
just `--data-binary @demo/sample_payload.json` — the existing demo flow still works.

## 6. Sample `event_alert` payload shape

Sentry wraps the event under `data.event` and the matched rule under
`data.triggered_rule`:

```json
{
  "action": "triggered",
  "installation": { "uuid": "..." },
  "data": {
    "event": {
      "event_id": "abc123...",
      "project": 1,
      "release": "frontend@22.10.0",
      "title": "TypeError: Cannot read property 'x' of undefined",
      "message": "TypeError: Cannot read property 'x' of undefined",
      "tags": [
        ["level", "error"],
        ["service", "users-api"],
        ["environment", "prod"]
      ],
      "exception": {
        "values": [{
          "type": "TypeError",
          "value": "Cannot read property 'x' of undefined",
          "stacktrace": {
            "frames": [
              { "filename": "src/routes/sessions.py", "function": "create_session", "lineno": 42 }
            ]
          }
        }]
      }
    },
    "triggered_rule": "High priority errors"
  },
  "actor": { "type": "application", "id": "sentry", "name": "Sentry" }
}
```

The harness `parse_sentry_payload` already reads from `payload["data"]["event"]`, so
no payload massaging is needed.

## 7. Troubleshooting

- `401 invalid signature` → `SENTRY_CLIENT_SECRET` on Render does not match the
  integration's Client Secret, or the curl body was modified after signing (e.g.
  a shell here-string re-encoded a newline). Always sign and POST the same bytes.
- Webhook fires but `GET /runs` doesn't show a new run → check
  `Sentry-Hook-Resource` header value; anything not in `{event_alert, issue, error}`
  is intentionally ignored with `200 {"ignored": true}`.
- No webhook arriving at all → in Sentry, the integration's "Webhook Log" tab shows
  delivery attempts and response codes.
