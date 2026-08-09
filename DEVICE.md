# Device API & WebSockets

Firmware / edge integration guide for Smart Boiler Room controllers.

**REST base:** `/api/v1/`  
**WS base:** `/ws/v1/`  
**Trailing slashes:** disabled for REST (`APPEND_SLASH = False`); device WS path accepts an optional trailing slash.  
**Envelope:** all successful REST responses wrap payload in `{ "data": ..., "meta": { "request_id", "server_time" } }`.

Related files:

- Full platform API: [`API.md`](API.md)
- Example WS/REST payloads: [`websocket-mock.json`](websocket-mock.json)

---

## Lifecycle (high level)

```text
Admin provisions device
        ↓
  numeric username + password (+ provisioning_code once)
        ↓
Device → POST /auth/device/login → session token
        ↓
Installer pairs device to a boiler room
        ↓
Device opens WS /ws/v1/devices/<public_id>/
        ↓
device.hello → hello_ack (+ config.apply / schedule.apply / pending commands)
        ↓
Heartbeats + telemetry + command.ack / command.result
```

| Step | Who | Endpoint |
|------|-----|----------|
| Provision | ADMIN / SUPER_ADMIN | `POST /devices/provision` |
| Login | Device | `POST /auth/device/login` |
| Pair | INSTALLER / ADMIN / SUPER_ADMIN | `POST /devices/<id>/pair` |
| Live channel | Device | `WS /ws/v1/devices/<id>/` |
| Telemetry (HTTP) | Device | `POST /devices/<id>/telemetry` |
| Commands | App user | `POST /devices/<id>/commands` → WS `command.execute` |

`<id>` in paths may be `public_id`, database PK, or `serial_number` (unless noted).

---

## Authentication

| Client | Header |
|--------|--------|
| Human (apps / admin) | `Authorization: Bearer <jwt_access>` |
| Device (edge) | `Authorization: Device <session_token>` |

WebSocket auth (either form):

- Header: `Authorization: Device <token>` or `Authorization: Bearer <jwt>`
- Query: `?token=<session_token_or_jwt>`

### Device session

`POST /api/v1/auth/device/login` — public, throttle scope `login`.

**Request**

```json
{
  "username": "8123456789",
  "password": "12345678"
}
```

Both fields must be digits only.

**Response `data`**

```json
{
  "token": "<opaque-session-token>",
  "token_type": "Device",
  "expires_in": 86400,
  "device_id": "DEV-..."
}
```

- Token lifetime: `DEVICE_SESSION_LIFETIME_SECONDS` (default **86400**).
- Tokens are stored hashed server-side.
- `POST /devices/<id>/password` revokes **all** active sessions for that device.
- Use the same token for REST (`Authorization: Device …`) and the device WebSocket.

---

## Desired vs reported

| Field | Meaning | Source |
|--------|---------|--------|
| Unit `desired_state` / `desired_mode` | Cloud intent | App commands (`boiler.*` / `pump.*`) |
| Unit `reported_state` / `reported_mode` | Device truth | Telemetry `boiler_states` / `pump_states` (boiler command results may also update) |
| Room `desired_*_version` | Version cloud wants | Config/schedule publish |
| Device `active_*_version` | Version device reports running | Hello / telemetry |

On `device.hello`, if desired ≠ active, the server may push `config.apply` / `schedule.apply` and re-dispatch queued commands.

---

## REST endpoints

### Index

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/auth/device/login` | Public | Issue device session token |
| GET | `/devices` | User JWT | List devices in caller scope |
| POST | `/devices/provision` | ADMIN / SUPER_ADMIN | Create device + credentials |
| GET | `/devices/<id>` | User JWT or Device token (self) | Detail |
| PATCH | `/devices/<id>` | INSTALLER / ADMIN / SUPER_ADMIN | Update metadata |
| POST | `/devices/<id>/password` | INSTALLER / ADMIN / SUPER_ADMIN | Reset password; revoke sessions |
| POST | `/devices/<id>/pair` | INSTALLER / ADMIN / SUPER_ADMIN | Attach to boiler room |
| GET | `/devices/<id>/diagnostics` | INSTALLER / ADMIN / SUPER_ADMIN | Presence / network / versions |
| POST | `/devices/<id>/telemetry` | Device token | Ingest telemetry (202) |
| POST | `/devices/<id>/errors` | Device token | Ingest errors → alerts |
| POST | `/devices/<id>/commands` | User JWT (control) | Queue command (202) |
| GET | `/commands/<command_id>` | User JWT | Command status + events |

Installer assignment (admin-only, not called by firmware):

| Method | Path |
|--------|------|
| GET / POST | `/assignments/devices` |
| PATCH / DELETE | `/assignments/devices/<assignment_id>` |

---

### `POST /devices/provision`

**Roles:** ADMIN / SUPER_ADMIN  

**Body**

```json
{
  "serial_number": "XYZ-123",
  "hardware_version": "CTRL-V2",
  "firmware_version": "1.0.0"
}
```

`hardware_version` / `firmware_version` optional.

**Returns once** (in `data`): normal device object plus:

- `provisioning_code`
- `device_password` (plain, shown once)
- `device_username` (numeric, also on every subsequent device payload)

---

### `GET /devices` / `GET /devices/<id>`

Device object fields include:

`id`, `public_id`, `device_username`, `serial_number`, `boiler_room`, `status`, `presence`, `device_status`, firmware/hardware versions, presence timestamps, network (`network_type`, `rssi_dbm`), `active_config_version`, `active_schedule_version`, `capabilities`, `paired_at`, nested `boiler_units[]`, `pump_units[]`, `sensors[]`.

Device token may only fetch **itself**.

---

### `PATCH /devices/<id>`

**Body (any subset):** `serial_number`, `hardware_version`, `firmware_version`, `status`, `device_status`.

Does not change room assignment or credentials.

---

### `POST /devices/<id>/password`

**Body:** `{ "password": "4321" }` — digits only, min length 4.

**Response:** `{ "password_changed": true, "sessions_revoked": true }`

---

### `POST /devices/<id>/pair`

**Roles:** INSTALLER / ADMIN / SUPER_ADMIN  
Installers must already be assigned to both the device and the target room.

**Body**

```json
{
  "boiler_room_id": 1,
  "customer_user_id": 4,
  "boiler_count": 2,
  "pump_count": 2,
  "sensors": [
    { "sensor_id": "inlet-1", "type": "inlet_temperature", "sequence": 1 }
  ]
}
```

| Field | Notes |
|-------|--------|
| `boiler_room_id` | Required |
| `customer_user_id` | Optional; **ADMIN / SUPER_ADMIN only** |
| `boiler_count` | 1–16, default 1 |
| `pump_count` | 0–16, default 0 |
| `sensors` | Optional list |

Creates/links room-owned boiler & pump units (with this device as controller where applicable) and sensors.

---

### `GET /devices/<id>/diagnostics`

Returns presence, firmware/hardware, last seen/heartbeat, uptime, network, free memory, active versions, `last_processed_command_id`, `capabilities`.

---

### `POST /devices/<id>/telemetry`

**Auth:** Device token only. Path `device_id` must match the authenticated device (`public_id`, serial, or PK). Payload `device_id` must also match.

**Status:** `202 Accepted`  
Deduped by `message_id`.

```json
{
  "schema_version": 1,
  "message_id": "msg-1001",
  "device_id": "DEV-...",
  "captured_at": "2026-08-02T12:00:00Z",
  "sequence": 1001,
  "device_status": "normal",
  "active_config_version": 1,
  "active_schedule_version": 1,
  "uptime_seconds": 3600,
  "network": { "type": "wifi", "rssi_dbm": -55 },
  "sensor_readings": [
    {
      "sensor_id": "inlet-1",
      "type": "inlet_temperature",
      "value": 42.5,
      "unit": "C",
      "status": "ok"
    }
  ],
  "boiler_states": [
    { "boiler_index": 1, "state": "on", "mode": "automatic" }
  ],
  "pump_states": [
    { "pump_index": 1, "state": "on", "mode": "automatic" }
  ],
  "errors": []
}
```

`boiler_states` / `pump_states` update unit `reported_state` / `reported_mode`.

**Response**

```json
{
  "accepted": true,
  "message_id": "msg-1001",
  "duplicate": false,
  "server_time": "2026-08-02T12:00:01Z"
}
```

---

### `POST /devices/<id>/errors`

**Auth:** Device token only.

Body: `{ "errors": [ ... ] }` or a single error object. Each entry becomes an alert.

---

### `POST /devices/<id>/commands`

**Auth:** User JWT with control permission on the device.  
Optional header: `Idempotency-Key`.

**Status:** `202 Accepted`

```json
{
  "name": "pump.turn_on",
  "target": { "pump_index": 1 },
  "parameters": {},
  "expires_in_seconds": 60
}
```

`expires_in_seconds`: 1–300 (optional; server default applies if omitted).

**Response**

```json
{
  "command_id": "cmd-...",
  "status": "queued",
  "created_at": "...",
  "expires_at": "..."
}
```

If the device is online on WS, the server pushes `command.execute` immediately (and again after hello reconciliation for still-queued/sent commands).

#### Supported command names

| name | target | parameters |
|------|--------|------------|
| `boiler.turn_on` | `{ "boiler_index": N }` | — |
| `boiler.turn_off` | `{ "boiler_index": N }` | — |
| `boiler.set_mode` | `{ "boiler_index": N }` | `{ "mode": "automatic" \| "manual" }` |
| `pump.turn_on` | `{ "pump_index": N }` | — |
| `pump.turn_off` | `{ "pump_index": N }` | — |
| `pump.set_mode` | `{ "pump_index": N }` | `{ "mode": "automatic" \| "manual" }` |
| `device.request_state` | `{}` | — |
| `device.restart_service` | `{}` | optional `{ "service": "..." }` |
| `alarm.acknowledge_local` | `{}` | optional alarm fields |

Boiler/pump commands set unit **desired** state/mode before dispatch. Unknown index → `400`.

Poll status: `GET /api/v1/commands/<command_id>`.

---

## WebSockets

Run an ASGI server locally (not `runserver` alone) for WS:

```bash
daphne -b 0.0.0.0 -p 8000 config.asgi:application
```

Production example: `wss://br.mayanext.com/ws/v1/...`

All messages are JSON envelopes:

```json
{
  "v": 1,
  "type": "<event.type>",
  "event_id": "evt-...",
  "sent_at": "2026-08-02T12:00:00Z",
  "payload": {}
}
```

Optional fields seen on some messages: `correlation_id`, `expires_at`.

---

### Device channel — `WS /ws/v1/devices/<device_id>/`

**URL examples**

```text
ws://127.0.0.1:8000/ws/v1/devices/DEV-DEMO/?token=<DEVICE_SESSION_TOKEN>
wss://br.mayanext.com/ws/v1/devices/DEV-DEMO/?token=<DEVICE_SESSION_TOKEN>
```

- **Auth:** Device session token (header or `?token=`).
- **Path:** must be the device’s `public_id` or `serial_number` (must match authenticated device).
- **Close codes:** `4401` unauthenticated / invalid token · `4403` path `device_id` mismatch.
- On connect: presence → online. On disconnect: presence → offline (+ presence evaluation).
- Each inbound message re-verifies the session token; revoked password closes with `4401`.

#### Device → server

| type | Purpose | Typical payload |
|------|---------|-----------------|
| `device.hello` | First message after connect | firmware/hardware, `active_config_version`, `active_schedule_version`, `last_processed_command_id`, `capabilities` |
| `device.heartbeat` | Keepalive | `uptime_seconds`, versions, `network_type`, `rssi_dbm`, `free_memory_bytes` |
| `command.ack` | Accept/reject execute | `command_id`, `accepted`, `stage` |
| `command.result` | Final outcome | `command_id`, `status`, optional `reported_state` / `error` |
| `config.result` | Config apply outcome | `config_version`, `status`: `applied` \| `failed` |
| `schedule.result` | Schedule apply outcome | `schedule_version`, `status`: `applied` \| `failed` |
| `device.error` | Raise alert | error fields |
| `device.state` | Live dashboard push | arbitrary state object → apps as `dashboard.state_changed` |

**Hello example**

```json
{
  "v": 1,
  "type": "device.hello",
  "event_id": "evt-hello-1",
  "payload": {
    "firmware_version": "1.2.5",
    "hardware_version": "CTRL-V2",
    "active_config_version": 0,
    "active_schedule_version": 0,
    "last_processed_command_id": null,
    "capabilities": {
      "max_boilers": 16,
      "max_pumps": 16,
      "supports_schedules": true
    }
  }
}
```

**Heartbeat interval:** from `device.hello_ack.payload.heartbeat_interval_seconds` (env `DEVICE_HEARTBEAT_INTERVAL_SECONDS`, default **30**). Offline threshold: `DEVICE_OFFLINE_THRESHOLD_SECONDS` (default **120**).

#### Server → device

| type | Purpose |
|------|---------|
| `device.hello_ack` | `connection_id`, `server_time`, `desired_config_version`, `desired_schedule_version`, `heartbeat_interval_seconds` |
| `command.execute` | `{ command_id, name, target, parameters }` |
| `config.apply` | Config document + `config_version` |
| `schedule.apply` | `{ schedule_version, timezone, weekly_rules, exceptions }` |

After hello, the server may reconcile: push missing config/schedule and re-dispatch non-expired commands in `queued` / `sent`.

**Hello ack example**

```json
{
  "v": 1,
  "type": "device.hello_ack",
  "event_id": "evt-server-...",
  "correlation_id": "evt-hello-1",
  "sent_at": "...",
  "payload": {
    "connection_id": "a1b2c3d4e5f6",
    "server_time": "2026-08-02T12:00:00Z",
    "desired_config_version": 1,
    "desired_schedule_version": 1,
    "heartbeat_interval_seconds": 30
  }
}
```

**Command execute example**

```json
{
  "v": 1,
  "type": "command.execute",
  "event_id": "evt-cmd-...",
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

**Device ack / result**

```json
{
  "v": 1,
  "type": "command.ack",
  "event_id": "evt-ack-1",
  "payload": {
    "command_id": "cmd-...",
    "accepted": true,
    "stage": "received"
  }
}
```

```json
{
  "v": 1,
  "type": "command.result",
  "event_id": "evt-res-1",
  "payload": {
    "command_id": "cmd-...",
    "status": "executed",
    "reported_state": { "boiler_index": 1, "state": "on" }
  }
}
```

**Config / schedule result**

```json
{ "v": 1, "type": "config.result", "payload": { "config_version": 1, "status": "applied" } }
```

```json
{ "v": 1, "type": "schedule.result", "payload": { "schedule_version": 1, "status": "applied" } }
```

---

### App channel — `WS /ws/v1/app/`

Not used by firmware; apps listen for live updates.

- **Auth:** user JWT (`Bearer` or `?token=`).
- Joins `boiler_room_<id>` for accessible rooms + `user_<id>`.
- **Send:** `{ "type": "ping" }` → `{ "type": "pong", ... }`.
- **Receive:** `command.status_changed`, `sync.status_changed`, `dashboard.state_changed`, `device.presence_changed`, `alert.opened`, `alert.updated`.

---

## End-to-end examples

### Turn on pump 1

1. App: `POST /devices/<id>/commands` with `pump.turn_on` → sets `desired_state=on`.
2. Device WS receives `command.execute`.
3. Device sends `command.ack`, then `command.result`.
4. App WS receives `command.status_changed`.
5. Device later posts telemetry `pump_states` so `reported_state` matches.

### Apply published schedule

1. Admin/app publishes schedule on the boiler room.
2. Device (after hello or live) receives `schedule.apply`.
3. Device replies `schedule.result` with `status: "applied"`.

---

## Demo credentials (`seed_demo`)

Human logins (numeric):

| Role | Username | Password |
|------|----------|----------|
| Super Admin | `9001` | `9001` |
| Admin | `2001` | `2001` |
| Installer | `1001` | `1001` |
| User | `3001` | `3001` |

Seed also creates a demo device (serial `XYZ-123`). After seed, use the printed `public_id` / `device_username` / password, or reset via admin/installer password API, then `POST /auth/device/login`.

---

## Env knobs (device-related)

| Variable | Default | Meaning |
|----------|---------|---------|
| `DEVICE_SESSION_LIFETIME_SECONDS` | `86400` | Device session token TTL |
| `DEVICE_HEARTBEAT_INTERVAL_SECONDS` | `30` | Suggested heartbeat period in `hello_ack` |
| `DEVICE_OFFLINE_THRESHOLD_SECONDS` | `120` | Presence / offline evaluation |
| `COMMAND_DEFAULT_EXPIRY_SECONDS` | `15` | Default command expiry when omitted |
| `TELEMETRY_DEFAULT_INTERVAL_SECONDS` | `60` | Typical config telemetry interval |

---

## Quick test checklist

```bash
# 1) Device login
curl -s -X POST https://br.mayanext.com/api/v1/auth/device/login \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"<device_username>\",\"password\":\"<password>\"}"

# 2) Self detail
curl -s https://br.mayanext.com/api/v1/devices/<public_id> \
  -H "Authorization: Device <token>"

# 3) WebSocket (use a WS client); first message = device.hello
# wss://br.mayanext.com/ws/v1/devices/<public_id>/?token=<token>
```

More payload samples: [`websocket-mock.json`](websocket-mock.json).
