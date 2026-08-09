# Smart Boiler Room Control API

Versioned REST API documentation for the Smart Boiler Room Control System.

**Base URL:** `/api/v1/`  
**Trailing slashes:** Disabled (`APPEND_SLASH = False`)  
**Default auth:** JWT Bearer token  
**Default permission:** Authenticated unless noted  

Interactive OpenAPI (when `DEBUG=True` or `ENABLE_API_DOCS=True`):

- Schema: `GET /api/schema/`
- Swagger UI: `GET /api/docs/`

---

## Authentication

| Client | Header |
|--------|--------|
| User (app / admin) | `Authorization: Bearer <access_jwt>` |
| Device (edge) | `Authorization: Device <access_token>` |

Roles referenced below: `SUPER_ADMIN`, `ADMIN`, `INSTALLER`, and other authenticated users with room/device scope.

---

## Auth & profile

### `POST /api/v1/auth/login`

Authenticate a user and issue JWT tokens.

- **Auth:** Public (`AllowAny`)
- **Throttle:** `login`
- **Body:**
  - `email` (required)
  - `password` (required)
- **Behavior:** Validates credentials, returns access + refresh tokens, writes audit event `auth.login`.

### `POST /api/v1/auth/refresh`

Rotate/refresh the access token (SimpleJWT).

- **Auth:** Public
- **Throttle:** `login`
- **Body:**
  - `refresh` (required)

### `POST /api/v1/auth/logout`

Log out and optionally blacklist the refresh token.

- **Auth:** Authenticated
- **Body:**
  - `refresh` (optional)
- **Behavior:** Blacklists refresh token when provided; writes audit event `auth.logout`.

### `GET /api/v1/me`

Return the current user’s profile and effective permissions.

- **Auth:** Authenticated

---

## Boiler rooms

All room endpoints require authentication and access to the target room (via role-scoped helpers such as `boiler_rooms_for_user` / `user_can_access_boiler_room`).

### `GET /api/v1/boiler-rooms`

List boiler rooms the caller can access.

### `POST /api/v1/boiler-rooms`

Create a boiler room (optionally with a new site).

- **Roles:** `ADMIN`, `SUPER_ADMIN`
- **Body:**
  - `name` (required)
  - `site_id` (optional)
  - `site_name` (optional)
  - `address` (optional)
  - `timezone` (optional)
  - `organization_id` (optional)
  - `customer_user_id` (optional)

### `GET /api/v1/boiler-rooms/<pk>`

Get boiler room detail.

- **Path:** `pk` — room ID

### `PATCH /api/v1/boiler-rooms/<pk>`

Update room name and/or status.

- **Roles (update):** `ADMIN`, `SUPER_ADMIN`, `INSTALLER`
- **Body:**
  - `name` (optional)
  - `status` (optional)

### `GET /api/v1/boiler-rooms/<pk>/configuration`

Get desired/applied configuration versions and documents for the room.

### `POST /api/v1/boiler-rooms/<pk>/configuration/versions`

Create a new configuration version.

- **Body:**
  - `document` (JSON object), **or** send the document as the raw JSON body

### `POST /api/v1/boiler-rooms/<pk>/configuration/<version>/publish`

Publish a configuration version to devices.

- **Path:** `pk`, `version`

### `GET /api/v1/boiler-rooms/<pk>/schedules`

List schedule versions for the room.

### `POST /api/v1/boiler-rooms/<pk>/schedules`

Create a schedule version.

- **Body:**
  - `weekly_rules` (default `[]`)
  - `exceptions` (default `[]`)
  - `publish` (default `true`)
  - `timezone` (optional)

### `GET /api/v1/boiler-rooms/<pk>/telemetry`

Return the latest telemetry envelopes for devices in the room (up to ~200).

### `GET /api/v1/boiler-rooms/<pk>/alerts`

List alerts for the room.

### `GET /api/v1/boiler-rooms/<pk>/reports/daily`

Daily telemetry/command summary.

- **Query:**
  - `date` — `YYYY-MM-DD` (default: today)

### `GET /api/v1/boiler-rooms/<pk>/reports/weekly`

Weekly report.

- **Query:**
  - `week_start` — `YYYY-MM-DD`

### `GET /api/v1/boiler-rooms/<pk>/reports/monthly`

Monthly report.

- **Query:**
  - `month` — `YYYY-MM`

---

## Devices

### `GET /api/v1/devices`

List devices visible to the caller (includes nested boilers/sensors where applicable).

### `POST /api/v1/devices/provision`

Provision a new device and return a provisioning code + device access token.

- **Roles:** `INSTALLER`, `ADMIN`, `SUPER_ADMIN` (`CanProvisionDevice`)
- **Body:**
  - `serial_number` (required)
  - `organization_id` (optional)
  - `hardware_version` (optional)
  - `firmware_version` (optional)

### `GET /api/v1/devices/<device_id>`

Get device detail.

- **Path:** `device_id` — `public_id`, numeric PK, or serial
- **Auth:** User JWT (scoped) **or** device token (self only)

### `POST /api/v1/devices/<device_id>/pair`

Pair a device to a boiler room and create boilers/sensors.

- **Roles:** `INSTALLER`, `ADMIN`, `SUPER_ADMIN`
- **Body:**
  - `boiler_room_id` (required)
  - `customer_user_id` (optional)
  - `boiler_count` (optional, 1–16)
  - `sensors` (optional list of sensor dicts)

### `GET /api/v1/devices/<device_id>/diagnostics`

Diagnostics snapshot: presence, firmware, network, memory, active versions, capabilities.

- **Roles:** `INSTALLER`, `ADMIN`, `SUPER_ADMIN`

### `POST /api/v1/devices/<device_id>/telemetry`

Ingest a telemetry envelope from the device (HTTP 202). Deduplicated by `message_id`.

- **Auth:** Device token only
- **Body (typical):**
  - `schema_version`
  - `message_id`
  - `device_id`
  - `captured_at`
  - `sensor_readings`
  - `boiler_states`
  - `device_status`
  - optional: `sequence`, `active_schedule_version`, `active_config_version`, `errors`, `uptime_seconds`, `network`

### `POST /api/v1/devices/<device_id>/errors`

Ingest device error(s); creates/updates alerts.

- **Auth:** Device token only
- **Body:**
  - `errors` (list), **or** a single error object

### `POST /api/v1/devices/<device_id>/commands`

Queue a command for the device (HTTP 202).

- **Auth:** Authenticated user who can control the device
- **Headers:**
  - `Idempotency-Key` (optional)
- **Body:**
  - `name` (required)
  - `target` (optional)
  - `parameters` (optional)
  - `expires_in_seconds` (optional, 1–300)

---

## Commands

### `GET /api/v1/commands/<command_id>`

Get command status and related events.

- **Auth:** Authenticated; command’s device must be in the user’s device scope

---

## Schedules

### `PATCH /api/v1/schedules/<pk>`

Create a new schedule version by partially patching an existing one.

- **Path:** `pk` — schedule version ID
- **Auth:** Authenticated + access to the schedule’s boiler room
- **Body (all optional):**
  - `weekly_rules`
  - `exceptions`
  - `publish`
  - `timezone`

---

## Alerts

### `POST /api/v1/alerts/<pk>/acknowledge`

Acknowledge an alert.

- **Auth:** Authenticated; alert’s room must be accessible

### `POST /api/v1/alerts/<pk>/resolve`

Resolve an alert.

- **Auth:** Authenticated; alert’s room must be accessible

---

## Audit

### `GET /api/v1/audit-events`

List audit events (capped at ~200).

- **Query:**
  - `action` (optional)
  - `object_type` (optional)
- **Visibility:**
  - `SUPER_ADMIN` — all events
  - `ADMIN` — own actor events + `device.*`
  - others — own user events

---

## Reports index

### `GET /api/v1/reports`

Discovery payload listing report URL templates.

Actual report data endpoints live under:

- `/api/v1/boiler-rooms/<pk>/reports/daily`
- `/api/v1/boiler-rooms/<pk>/reports/weekly`
- `/api/v1/boiler-rooms/<pk>/reports/monthly`

---

## Admin & OpenAPI

| Method | Path | Description |
|--------|------|-------------|
| — | `/admin/` | Django admin UI |
| `GET` | `/api/schema/` | OpenAPI schema JSON (docs enabled) |
| `GET` | `/api/docs/` | Swagger UI (docs enabled) |

---

## WebSockets

Mounted via Django Channels (JWT / device auth middleware). All messages are JSON.

### Common envelope

```json
{
  "v": 1,
  "type": "<event.type>",
  "event_id": "evt-...",
  "sent_at": "2026-08-01T12:00:00Z",
  "payload": {}
}
```

Optional fields used on some server→device messages: `correlation_id`, `expires_at`.

---

### `WS /ws/v1/devices/<device_id>/`

Device realtime channel.

- **Auth:** `Authorization: Device <access_token>` or `?token=<access_token>`
- **Path:** `device_id` — device `public_id` or `serial_number` (must match the authenticated device)
- **Close codes:** `4401` unauthenticated, `4403` device_id mismatch

#### Device → server (send)

**`device.hello`** — send first after connect

```json
{
  "v": 1,
  "type": "device.hello",
  "event_id": "evt-device-001",
  "payload": {
    "firmware_version": "1.0.0",
    "hardware_version": "rev-a",
    "active_config_version": 1,
    "active_schedule_version": 1,
    "last_processed_command_id": null,
    "capabilities": {}
  }
}
```

**`device.heartbeat`** — send on the interval from `device.hello_ack` (default 30s)

```json
{
  "v": 1,
  "type": "device.heartbeat",
  "event_id": "evt-hb-001",
  "payload": {
    "uptime_seconds": 3600,
    "firmware_version": "1.0.0",
    "hardware_version": "rev-a",
    "network_type": "wifi",
    "rssi_dbm": -55,
    "free_memory_bytes": 120000
  }
}
```

**`command.ack`** — after receiving `command.execute`

```json
{
  "v": 1,
  "type": "command.ack",
  "event_id": "evt-ack-001",
  "payload": {
    "command_id": "cmd-...",
    "accepted": true,
    "stage": "received"
  }
}
```

- Reject: `"accepted": false` plus optional `"error": { "code": "..." }`
- `stage` may be a command status (`received`, `executing`, …)

**`command.result`** — when command finishes

```json
{
  "v": 1,
  "type": "command.result",
  "event_id": "evt-res-001",
  "payload": {
    "command_id": "cmd-...",
    "status": "executed",
    "reported_state": {
      "boiler_index": 1,
      "state": "on"
    }
  }
}
```

- Failure: `"status": "failed"` plus optional `"error": { ... }`
- Command statuses: `created`, `queued`, `sent`, `received`, `executing`, `executed`, `failed`, `expired`, `cancelled`

**`config.result`**

```json
{
  "v": 1,
  "type": "config.result",
  "event_id": "evt-cfg-001",
  "payload": {
    "config_version": 2,
    "status": "applied"
  }
}
```

- Failure: `"status": "failed"` plus `"validation_errors": [...]`

**`schedule.result`**

```json
{
  "v": 1,
  "type": "schedule.result",
  "event_id": "evt-sch-001",
  "payload": {
    "schedule_version": 2,
    "status": "applied"
  }
}
```

- Failure: `"status": "failed"` plus `"validation_errors": [...]`

**`device.error`**

```json
{
  "v": 1,
  "type": "device.error",
  "event_id": "evt-err-001",
  "payload": {
    "code": "sensor_fault",
    "message": "Inlet sensor timeout",
    "severity": "critical",
    "device_state": "fault"
  }
}
```

**`device.state`** — push live state to the app dashboard (`payload` is opaque JSON)

```json
{
  "v": 1,
  "type": "device.state",
  "event_id": "evt-st-001",
  "payload": {
    "boilers": [{ "index": 1, "state": "on" }]
  }
}
```

#### Server → device (receive)

**`device.hello_ack`**

```json
{
  "v": 1,
  "type": "device.hello_ack",
  "event_id": "evt-server-...",
  "correlation_id": "evt-device-001",
  "sent_at": "...",
  "payload": {
    "connection_id": "...",
    "server_time": "...",
    "desired_config_version": 2,
    "desired_schedule_version": 1,
    "heartbeat_interval_seconds": 30
  }
}
```

After hello, if versions or queued commands are out of sync, the server may also push `config.apply`, `schedule.apply`, and pending `command.execute`.

**`command.execute`**

```json
{
  "v": 1,
  "type": "command.execute",
  "event_id": "evt-...",
  "correlation_id": "cmd-...",
  "sent_at": "...",
  "expires_at": "...",
  "payload": {
    "command_id": "cmd-...",
    "name": "boiler.turn_on",
    "target": { "boiler_index": 1 },
    "parameters": {}
  }
}
```

Known command names: `boiler.turn_on`, `boiler.turn_off`, `boiler.set_mode`, `device.request_state`, `device.restart_service`.

**`config.apply`** — payload is the configuration document plus `config_version`

```json
{
  "v": 1,
  "type": "config.apply",
  "event_id": "evt-...",
  "correlation_id": "config-2",
  "sent_at": "...",
  "payload": {
    "config_version": 2
  }
}
```

**`schedule.apply`**

```json
{
  "v": 1,
  "type": "schedule.apply",
  "event_id": "evt-...",
  "correlation_id": "schedule-2",
  "sent_at": "...",
  "payload": {
    "schedule_version": 2,
    "timezone": "Asia/Tehran",
    "weekly_rules": [],
    "exceptions": []
  }
}
```

#### Typical device flow

1. Connect WS with device token
2. Send `device.hello`
3. Receive `device.hello_ack` (and possibly `config.apply` / `schedule.apply` / `command.execute`)
4. Loop: send `device.heartbeat`
5. On command: `command.ack` → execute → `command.result`
6. On config/schedule: apply → `config.result` / `schedule.result`

---

### `WS /ws/v1/app/`

App/dashboard realtime channel.

- **Auth:** `Authorization: Bearer <access_jwt>` or `?token=<access_jwt>`
- **Behavior:** Joins all accessible `boiler_room_<id>` groups and `user_<id>`; primarily a push listener

#### App → server (send)

Only **`ping`** is handled:

```json
{ "type": "ping" }
```

#### Server → app (receive)

**`pong`**

```json
{
  "v": 1,
  "type": "pong",
  "event_id": "evt-...",
  "sent_at": "...",
  "payload": {}
}
```

**Push events**

| `type` | When |
|--------|------|
| `command.status_changed` | Command status updates |
| `sync.status_changed` | Config/schedule apply result (`kind`: `"config"`; schedule results currently do not broadcast) |
| `dashboard.state_changed` | Device sent `device.state` |
| `device.presence_changed` | Device online/offline |
| `alert.created` / `alert.updated` | Alerts open/update/resolve |

**`command.status_changed`**

```json
{
  "v": 1,
  "type": "command.status_changed",
  "event_id": "app-evt-...",
  "sent_at": "...",
  "payload": {
    "command_id": "cmd-...",
    "device_id": "dev-...",
    "status": "executed",
    "reported_state": { "boiler_index": 1, "state": "on" }
  }
}
```

**`sync.status_changed`**

```json
{
  "v": 1,
  "type": "sync.status_changed",
  "payload": {
    "kind": "config",
    "device_id": "dev-...",
    "version": 2,
    "status": "applied"
  }
}
```

**`dashboard.state_changed`**

```json
{
  "v": 1,
  "type": "dashboard.state_changed",
  "payload": {
    "device_id": "dev-...",
    "state": {}
  }
}
```

**`device.presence_changed`**

```json
{
  "v": 1,
  "type": "device.presence_changed",
  "payload": {
    "device_id": "dev-...",
    "presence": "offline",
    "last_seen_at": "..."
  }
}
```

**`alert.created` / `alert.updated`**

```json
{
  "v": 1,
  "type": "alert.updated",
  "payload": {
    "alert_id": 1,
    "type": "device_offline",
    "code": "device_offline",
    "severity": "warning",
    "status": "open",
    "message": "Device ... is offline.",
    "device_id": "dev-...",
    "boiler_room_id": 1
  }
}
```

---

## Endpoint index (quick reference)

| Method | Path |
|--------|------|
| POST | `/api/v1/auth/login` |
| POST | `/api/v1/auth/refresh` |
| POST | `/api/v1/auth/logout` |
| GET | `/api/v1/me` |
| GET, POST | `/api/v1/boiler-rooms` |
| GET, PATCH | `/api/v1/boiler-rooms/<pk>` |
| GET | `/api/v1/boiler-rooms/<pk>/configuration` |
| POST | `/api/v1/boiler-rooms/<pk>/configuration/versions` |
| POST | `/api/v1/boiler-rooms/<pk>/configuration/<version>/publish` |
| GET, POST | `/api/v1/boiler-rooms/<pk>/schedules` |
| GET | `/api/v1/boiler-rooms/<pk>/telemetry` |
| GET | `/api/v1/boiler-rooms/<pk>/alerts` |
| GET | `/api/v1/boiler-rooms/<pk>/reports/daily` |
| GET | `/api/v1/boiler-rooms/<pk>/reports/weekly` |
| GET | `/api/v1/boiler-rooms/<pk>/reports/monthly` |
| GET | `/api/v1/devices` |
| POST | `/api/v1/devices/provision` |
| GET | `/api/v1/devices/<device_id>` |
| POST | `/api/v1/devices/<device_id>/pair` |
| GET | `/api/v1/devices/<device_id>/diagnostics` |
| POST | `/api/v1/devices/<device_id>/telemetry` |
| POST | `/api/v1/devices/<device_id>/errors` |
| POST | `/api/v1/devices/<device_id>/commands` |
| GET | `/api/v1/commands/<command_id>` |
| PATCH | `/api/v1/schedules/<pk>` |
| POST | `/api/v1/alerts/<pk>/acknowledge` |
| POST | `/api/v1/alerts/<pk>/resolve` |
| GET | `/api/v1/audit-events` |
| GET | `/api/v1/reports` |
| WS | `/ws/v1/devices/<device_id>/` |
| WS | `/ws/v1/app/` |

---

## Notes

- Live routes are defined in `config/api_urls.py` (included from `config/urls.py` under `/api/v1/`).
- Per-app `urls.py` modules exist but are not mounted by the root URLconf.
- Apps without dedicated HTTP mounts (`access`, `boilers`, `sensors`, `configurations`, `telemetry`, `notifications`, `common`) provide models/services used by the endpoints above.
