# AirStage2MQTT

AirStage2MQTT is a local-only bridge between Fujitsu AirStage Wi-Fi air conditioners and
MQTT. It polls each configured unit over the LAN, publishes normalized JSON state, accepts
Zigbee2MQTT-style commands, and can optionally publish Home Assistant MQTT discovery.

Fujitsu cloud accounts and APIs are not used. Home Assistant is optional: any MQTT client can
monitor or control the units.

For component boundaries, MQTT ownership, persistence, and Mermaid process diagrams covering
startup, polling, commands, reconnects, and Home Assistant discovery, see the
[architecture guide](docs/architecture.md).

## Requirements

- A Linux Docker host, or Python 3.12 or newer for direct execution.
- An MQTT 3.1.1/5 broker such as Mosquitto.
- Fujitsu AirStage units supported by the local API in `pyairstage` 3.2.2.
- A DHCP reservation or static IPv4 address for every unit.
- The unit MAC address. Its separators are removed to form the Fujitsu device ID.

The container must be able to route to the MQTT broker and every configured A/C address.
Automatic IP discovery is not currently supported.

## Quick start with Docker Compose

1. Create local configuration from the committed examples:

   ```sh
   cp .env.example .env
   cp config.example.yaml config.yaml
   ```

2. Edit `.env` with the MQTT connection and edit `config.yaml` with the real A/C addresses and
   MAC addresses. Generate `bridge.key` once with `openssl rand -hex 16`; keep this value stable
   across upgrades and container replacements. Both files are ignored by Git.

3. Build and start the service:

   ```sh
   docker compose up -d --build
   ```

4. Follow the logs and inspect health:

   ```sh
   docker compose logs -f bridge
   docker inspect --format '{{json .State.Health}}' airstage2mqtt
   ```

To stop the bridge without deleting its Home Assistant discovery index:

```sh
docker compose down
```

## Configuration

### Environment variables

Docker Compose loads `.env`. Environment variables override the corresponding MQTT connection
and credential values in YAML. The MQTT base topic and unit list are always read from YAML.

| Variable | Default | Description |
| --- | --- | --- |
| `A2M_IMAGE` | `airstage2mqtt:local` | Image built or run by Compose |
| `A2M_CONFIG` | `/config/config.yaml` | Configuration path inside the container |
| `A2M_DATA_DIR` | `/data` | Discovery fallback-index directory |
| `A2M_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |
| `A2M_MQTT_HOST` | required | MQTT hostname or address |
| `A2M_MQTT_PORT` | `1883` | MQTT TCP port |
| `A2M_MQTT_USERNAME` | unset | MQTT username |
| `A2M_MQTT_PASSWORD` | unset | MQTT password; convenient but less private than a file |
| `A2M_MQTT_PASSWORD_FILE` | unset | File containing the MQTT password; takes precedence |
| `A2M_MQTT_TLS` | `false` | Enable MQTT TLS |
| `A2M_MQTT_TLS_CA_FILE` | system CAs | Optional CA bundle for MQTT TLS |
| `A2M_MQTT_TLS_INSECURE` | `false` | Disable MQTT certificate verification; discouraged |

For file-based credentials, create `secrets/mqtt_password`, uncomment the password-file volume
in `compose.yml`, remove `A2M_MQTT_PASSWORD`, and set:

```dotenv
A2M_MQTT_PASSWORD_FILE=/run/secrets/mqtt_password
```

Do not commit `.env`, `config.yaml`, or files under `secrets/`.

### YAML configuration

```yaml
bridge:
  key: 0123456789abcdef0123456789abcdef

mqtt:
  base_topic: airstage2mqtt

polling:
  interval_seconds: 10
  command_refresh_delay_seconds: 2
  timeout_seconds: 20
  retries: 5
  offline_after_failures: 2
  reconnect_min_seconds: 1
  reconnect_max_seconds: 30

homeassistant:
  enabled: true
  discovery_prefix: homeassistant

units:
  - name: living_room
    friendly_name: Living Room Air Conditioner
    mac: "E8:FB:1C:00:00:00"
    ip: "192.168.1.40"
    use_https: false
    turn_on_before_set_temperature: false
    diagnostics:
      enabled: []

  - name: bedroom
    friendly_name: Main Bedroom Air Conditioner
    mac: "E8:FB:1C:00:00:01"
    ip: "192.168.1.41"
    use_https: false
    turn_on_before_set_temperature: true
    diagnostics:
      enabled:
        - error_code
```

`bridge.key` is a required, stable ownership identifier containing 8-64 letters, digits,
underscores, or hyphens. It is not a password. Each bridge sharing a base topic must have a
different key; changing it leaves the old manifest inaccessible to the new instance. Unless
`mqtt.client_id` is set explicitly, the key also makes the MQTT client ID unique.

`name` is the topic-safe identifier and may contain letters, digits, `_`, and `-`. It cannot
contain spaces or use the reserved names `bridge`, `bridges`, or `manifests`. `friendly_name` is
optional, may contain spaces, and is used for Home Assistant and bridge metadata; when omitted it
is derived from `name`. Unit names, MAC addresses, and IP addresses must be unique. Unit names
must also be unique between bridge instances that share the same base topic.
`turn_on_before_set_temperature` controls whether a temperature command
automatically powers on an off unit or is rejected. `mqtt.base_topic` controls the shared
operational MQTT topic prefix; it must not contain MQTT wildcards.

`polling.command_refresh_delay_seconds` controls how long AirStage2MQTT waits after a command
write before polling the unit to reconcile its actual state. Each new command restarts this short
delay; after reconciliation, polling returns to `polling.interval_seconds`.

`diagnostics.enabled` is an optional per-unit list containing `error_code`, `demand`, and/or
`power_consumption`. If `diagnostics`, `enabled`, or the list itself is omitted or empty, all
three are disabled. Disabled diagnostics are omitted from both MQTT state and Home Assistant
discovery. Unknown names are configuration errors. Enabled diagnostics are discovered as active
diagnostic entities; a blank or Fujitsu `65535` unsupported value is omitted from state and
logged once instead of being published as a reading.

The following connection settings may alternatively be placed under the `mqtt:` mapping: `host`,
`port`, `username`, `password`, `password_file`, `tls`, `tls_ca_file`, and `tls_insecure`.
Environment variables take precedence for those settings. `base_topic`, `client_id`, and `qos`
are YAML-only settings.

`use_https` controls the local A/C connection, not MQTT. `pyairstage` disables certificate
validation for local HTTPS because the unit certificate does not match its IP address. Keep the
A/C network trusted and isolated.

## MQTT interface

For a unit named `living_room` and the default base topic:

| Topic | Direction | Retained | Purpose |
| --- | --- | --- | --- |
| `airstage2mqtt/living_room` | bridge → broker | yes | Complete normalized state |
| `airstage2mqtt/living_room/set` | client → bridge | no | Partial JSON command |
| `airstage2mqtt/living_room/set/<property>` | client → bridge | no | Single-property command |
| `airstage2mqtt/living_room/get` | client → bridge | no | Request an immediate refresh |
| `airstage2mqtt/living_room/availability` | bridge → broker | yes | `online` or `offline` |
| `airstage2mqtt/bridges/<key>/state` | bridge → broker | yes | `initializing`, `online`, or last-will `offline` |
| `airstage2mqtt/bridges/<key>/info` | bridge → broker | yes | Version and sanitized device metadata |
| `airstage2mqtt/manifests/<key>` | bridge → broker | yes | Topics owned by this bridge instance |

Subscribe to everything:

```sh
mosquitto_sub -h MQTT_HOST -u USERNAME -P PASSWORD -v -t 'airstage2mqtt/#'
```

Set several properties. AirStage2MQTT immediately publishes the accepted, modeled state before
writing it to the unit:

```sh
mosquitto_pub -h MQTT_HOST -u USERNAME -P PASSWORD \
  -t 'airstage2mqtt/living_room/set' \
  -m '{"state":"ON","mode":"heat","target_temperature":21.5,"fan_mode":"auto"}'
```

Set one property without a JSON object:

```sh
mosquitto_pub -h MQTT_HOST -t 'airstage2mqtt/living_room/set/economy' -m 'ON'
```

Request a refresh:

```sh
mosquitto_pub -h MQTT_HOST -t 'airstage2mqtt/living_room/get' -n
```

After a command, the pending scheduled poll is replaced by a reconciliation poll after
`command_refresh_delay_seconds`. If the unit reports the modeled state, the unchanged MQTT message
is suppressed; if it reports something different, the retained state is corrected. The regular
polling interval resumes after reconciliation. An explicit `/get` requests the next poll as soon
as any active write finishes.

Retained unit JSON and bridge information are otherwise published only when their normalized
content changes. Suppressing unchanged MQTT messages does not reduce hardware monitoring.
Availability is likewise published on transitions rather than on every poll.

### Properties

Writable properties are published only when supported by the unit:

| Property | Accepted values |
| --- | --- |
| `state` | `ON`, `OFF`, `TOGGLE` |
| `mode` | `off`, `auto`, `cool`, `dry`, `fan_only`, `heat` |
| `target_temperature` | Celsius number in the active mode's supported range, in 0.5 °C steps |
| `fan_mode` | `auto`, `quiet`, `low`, `medium`, `high` |
| `swing_mode` | `vertical_swing` or a published vertical position |
| `economy` | `ON` or `OFF` |
| `powerful` | `ON` or `OFF` |
| `outdoor_low_noise` | `ON` or `OFF` |
| `energy_save_fan` | `ON` or `OFF` |
| `minimum_heat` | `ON` or `OFF` |
| `indoor_led` | `ON` or `OFF` |
| `human_detection_auto_save` | `ON` or `OFF` |

Read-only state may also include `current_temperature`, `outdoor_temperature`,
`human_detection`, `filter_sign_reset`, and `model`. When enabled for that unit, it may also
include `power_consumption`, `demand`, and `error_code`.
Unknown, read-only, or unsupported commands are rejected and logged without affecting other
units. Medium-low and medium-high fan states can be reported by some units but cannot be written
through `pyairstage`.

The three configurable diagnostics expose raw, model-dependent local API fields:

- `error_code` is the unit's raw fault code; `0` normally means no reported fault.
- `demand` is a raw demand/capacity-control value and should not be interpreted as a reliable
  compressor-running indicator.
- `power_consumption` is not available on every model, and AirStage2MQTT deliberately assigns no
  power/energy unit or Home Assistant device class until its meaning can be validated by model.

## Home Assistant

Set `homeassistant.enabled: true` and ensure Home Assistant's MQTT integration uses the configured
discovery prefix (normally `homeassistant`). After the first successful device poll,
AirStage2MQTT publishes one retained MQTT device-discovery document containing:

- A primary climate entity.
- Indoor and outdoor temperature sensors when supported.
- Capability-dependent control switches and explicitly enabled diagnostic sensors.
- Bridge and per-unit availability.

Discovery is retained and published once after every bridge MQTT connection. Home Assistant birth
messages are also handled, but an identical discovery document is not republished repeatedly
during the same MQTT session. A transiently missing value cannot remove a previously established
capability from discovery. When a configured diagnostic is disabled, AirStage2MQTT uses Home
Assistant's explicit component-removal update before publishing the final device document.

At startup, the bridge publishes `initializing`, polls all units concurrently, and publishes each
unit's current state and availability. Only after every unit has either responded or exhausted
its configured local API retries does the bridge publish `online`. Home Assistant treats every
bridge state other than `online` as unavailable, so it cannot briefly expose stale per-unit
availability while startup is still in progress. A genuinely unreachable unit does not prevent
reachable units or the bridge from starting.

### Ownership manifest

Each instance publishes a retained manifest at `<base_topic>/manifests/<bridge.key>`. On startup,
the bridge reads its previous manifest, compares the previously owned operational and Home
Assistant discovery topics with the current configuration, clears obsolete retained messages,
and publishes the replacement manifest. Manifest entries are validated before deletion so one
instance cannot clear another instance's manifest or arbitrary broker topics.

MQTT has no portable topic-list operation, so the manifest provides a deterministic inventory.
If broker persistence is disabled, the manifest and stale retained messages disappear together.
If broker persistence is enabled, the manifest allows cleanup after units are renamed or removed
and after the Home Assistant discovery prefix changes. Retained messages on command and `/get`
topics are ignored and cleared rather than executed after a restart.

### `/data` persistence

The `/data` volume contains one small fallback file, `homeassistant-discovery.json`. It records
discovery topics, normalized device IDs, and the last discovery document. This lets the bridge
remove stale discovery when a broker manifest is missing or `mqtt.base_topic` changes, preserve a
stable entity schema across transient partial device responses, and explicitly remove diagnostics
disabled in configuration. It contains no MQTT credentials, A/C state history, readings, or
command history.

Persisting `/data` remains recommended when Home Assistant discovery is enabled, although the
broker manifest handles normal cleanup for a stable base topic. If discovery will always remain
disabled, persistence is not functionally necessary. The supplied Compose file uses the named
`bridge-data` volume; `docker compose down` preserves it, while `docker compose down -v` deletes
it.

Set `homeassistant.enabled: false` to use only the generic MQTT interface.

## Direct Python development

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.lock
python -m pytest
python -m ruff check src tests
python -m mypy src
A2M_CONFIG=./config.yaml A2M_DATA_DIR=./data \
  A2M_MQTT_HOST=localhost python -m airstage2mqtt
```

The Windows virtual-environment activation command is `.venv\Scripts\Activate.ps1`.

## Jenkins image publishing

The root `Jenkinsfile` follows the build/promotion flow used by the CIDR Allow List Updater. It
runs daily and on other job triggers, executes unit and restricted-container tests against local
Mosquitto and AirStage fixtures, publishes an agent-native image, records its digest, promotes
the SHA and `latest` tags, and finally creates an annotated Git tag.

The Jenkins controller or job must supply:

- `CONTAINER_REGISTRY_READ`
- `CONTAINER_REGISTRY_PUSH`
- `CI_GIT_USER_NAME`
- `CI_GIT_USER_EMAIL`
- `GIT_PUSH_CREDENTIALS_ID`

Registry values use `host[:port]` without a scheme. Publishing is anonymous; the Docker agent
must already trust and be allowed to push to that endpoint. Git tag credentials are bound only
during the tag push. The tag destination is derived from the checkout's `origin`; a GitHub HTTPS
checkout URL is rewritten to SSH for that operation. Successful builds produce immutable
`v0.1.<BUILD_NUMBER>` image and Git tags, plus moving `sha-<commit>` and `latest` image tags.

## Upgrades and troubleshooting

```sh
docker compose pull
docker compose up -d --build
docker compose logs --tail=200 bridge
```

- **Configuration error:** startup exits with status 2 and names the invalid field.
- **Unit remains offline:** verify its reservation, MAC, IP, HTTP/HTTPS selection, VLAN routing,
  and any Wi-Fi client isolation.
- **MQTT remains offline:** verify broker routing, credentials, TLS CA, and topic ACLs.
- **Home Assistant entity missing:** inspect the retained discovery topic and confirm the first
  device poll succeeded.
- **Container unhealthy:** check MQTT connectivity first; health represents a live MQTT session,
  not whether every A/C is online.

## Licence

AirStage2MQTT is available under the MIT licence. `pyairstage` and other dependencies retain
their respective licences.
