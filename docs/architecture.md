# AirStage2MQTT architecture

AirStage2MQTT is a local protocol bridge. It translates between retained MQTT state and commands
on one side and Fujitsu's AirStage LAN API on the other. There is no cloud client, cloud account,
or Internet API in the runtime path.

## System context

```mermaid
flowchart LR
    controller["MQTT clients<br/>Home Assistant, automations, CLI"]
    broker["MQTT broker<br/>Mosquitto"]

    subgraph container["AirStage2MQTT container"]
        config["Configuration loader<br/>YAML + environment + password file"]
        bridge["BridgeService<br/>MQTT session and readiness gate"]
        workers["One UnitWorker per A/C<br/>polling, locking, failure isolation"]
        adapters["PyairstageLocalUnit<br/>normalization and command validation"]
        discovery["DiscoveryManager<br/>Home Assistant schema"]
        manifest["ManifestManager<br/>retained-topic ownership"]
        data[("/data<br/>discovery index")]

        config --> bridge
        bridge --> workers
        workers --> adapters
        bridge --> discovery
        bridge --> manifest
        discovery <--> data
    end

    controller <--> broker
    bridge <--> broker
    discovery --> broker
    manifest --> broker
    adapters <-->|"local HTTP or HTTPS"| units["Fujitsu AirStage units<br/>reserved/static IPs"]

    internet["Fujitsu cloud<br/>(not used)"]
    internet ~~~ container
```

The cloud node is deliberately unconnected; the invisible Mermaid layout link only keeps it near
the system boundary. The service does not use the cloud. The container only needs network routes
to its MQTT broker and configured A/C addresses.

## Runtime responsibilities

| Component | Responsibility |
| --- | --- |
| `config.py` | Loads and validates MQTT, polling, discovery, bridge ownership, and per-unit settings. Environment variables override MQTT connection values; unit definitions remain in YAML. |
| `service.py` / `BridgeService` | Owns the MQTT connection, last will, subscriptions, readiness state, publish-on-change caches, and worker lifecycle. |
| `service.py` / `UnitWorker` | Serializes polling and commands for one unit, owns its reschedulable poll deadline and optimistic snapshot, tracks consecutive failures, and prevents one A/C from blocking another. |
| `airstage.py` | Uses `pyairstage.ApiLocal`, converts device responses into normalized state, tracks stable capabilities, validates and models accepted commands, and applies their device writes. |
| `discovery.py` | Builds one Home Assistant device-discovery document per A/C, suppresses identical updates, and explicitly removes disabled components. |
| `manifest.py` | Publishes the bridge-owned topic inventory and safely clears obsolete retained topics. |

The `aiohttp` session is shared, but each configured unit has its own adapter, worker, lock,
failure counter, current snapshot, and availability state. A slow or unreachable unit therefore
does not serialize polling for every other unit.

## MQTT topic layout and ownership

```mermaid
flowchart TB
    root["&lt;base_topic&gt;"]
    unit["&lt;unit_name&gt;"]
    state["retained JSON state"]
    set["set<br/>partial JSON command"]
    property["set/&lt;property&gt;<br/>single command"]
    get["get<br/>immediate refresh request"]
    availability["availability<br/>retained online/offline"]
    bridges["bridges/&lt;bridge_key&gt;"]
    bridge_state["state<br/>initializing/online/offline"]
    info["info<br/>retained sanitized metadata"]
    manifests["manifests/&lt;bridge_key&gt;<br/>retained ownership inventory"]
    discovery["&lt;discovery_prefix&gt;/device/<br/>airstage2mqtt_&lt;mac&gt;/config"]

    root --> unit
    unit --> state
    unit --> set
    unit --> property
    unit --> get
    unit --> availability
    root --> bridges
    bridges --> bridge_state
    bridges --> info
    root --> manifests
    root -. "separate Home Assistant namespace" .-> discovery
```

Only state, availability, bridge metadata, manifests, and discovery documents are retained.
Command and `/get` messages must not be retained; if a retained command is received, A2M ignores
and clears it rather than replaying it against the A/C.

`bridge.key` scopes bridge state, metadata, and the manifest. This allows several A2M instances to
share a base topic, provided their bridge keys and unit names do not collide.

## Startup and readiness

The bridge uses its own availability as a readiness gate. Home Assistant discovery maps every
bridge state other than `online` to unavailable. Unit state and availability are established
behind that gate, and `online` is always the final startup publication.

```mermaid
sequenceDiagram
    autonumber
    participant A2M as BridgeService
    participant MQTT as MQTT broker
    participant IDX as Manifest and discovery index
    participant U1 as Unit worker 1
    participant UN as Unit worker N
    participant HA as Home Assistant

    A2M->>MQTT: CONNECT with retained offline last will
    A2M->>MQTT: Publish bridge state = initializing (retained)
    MQTT-->>HA: Bridge is unavailable during initialization
    A2M->>MQTT: Read previous ownership manifest
    A2M->>IDX: Load the local discovery index
    A2M->>MQTT: Subscribe to command, get, and HA birth topics
    A2M->>MQTT: Clear safely identified obsolete retained topics
    A2M->>MQTT: Publish current ownership manifest

    par Initial polls run independently
        A2M->>U1: Initial local poll with configured timeout/retries
        U1-->>A2M: Current snapshot or confirmed failure
    and
        A2M->>UN: Initial local poll with configured timeout/retries
        UN-->>A2M: Current snapshot or confirmed failure
    end

    loop For each reachable unit
        A2M->>MQTT: Publish retained current state
        A2M->>MQTT: Publish unit availability = online
    end
    loop For each unreachable unit
        A2M->>MQTT: Publish unit availability = offline
    end
    A2M->>MQTT: Publish retained discovery and bridge info
    A2M->>MQTT: Publish bridge state = online (retained, last)
    MQTT-->>HA: Expose each unit using its already-established status
    A2M->>A2M: Start periodic workers, message consumer, and health heartbeat
```

Initial polls run concurrently. Startup waits for all of them to resolve, but each unit's local API
request is bounded by its timeout and retry configuration. An unreachable unit can delay readiness
only for that bounded period and then starts as `offline`; it does not prevent the bridge or healthy
units from becoming operational.

## Polling and publish-on-change

```mermaid
flowchart TD
    wait["Poll deadline expires<br/>regular, reconciliation, or /get"] --> lock["Acquire unit lock"]
    lock --> request["Read unit through ApiLocal"]
    request --> result{"Valid response?"}
    result -- No --> failures["Increment this unit's failure count"]
    failures --> threshold{"Offline threshold reached<br/>after previously online?"}
    threshold -- Yes --> offline["Publish retained unit offline"]
    threshold -- No --> release["Release lock"]
    offline --> release
    result -- Yes --> normalize["Normalize state and retain stable capabilities"]
    normalize --> compare{"Semantic JSON state changed?"}
    compare -- Yes --> publish["Publish retained state"]
    compare -- No --> suppress["Suppress duplicate MQTT message"]
    publish --> available{"Unit was unavailable?"}
    suppress --> available
    available -- Yes --> online["Publish retained unit online"]
    available -- No --> metadata{"Discovery or bridge metadata changed?"}
    online --> metadata
    metadata -- Yes --> update["Publish changed retained metadata"]
    metadata -- No --> release
    update --> release
```

Without commands, polling continues at `polling.interval_seconds`; publish suppression does not
reduce how often A2M checks the hardware. A command replaces the pending regular deadline with a
short reconciliation deadline. After that poll finishes, the worker returns to the regular
interval. The first successful poll after each MQTT connection republishes state so a broker that
lost retained data is repaired. Later polls publish only semantic changes.

Capabilities only grow during a process lifetime. Across restarts, the persisted discovery record
preserves prior non-diagnostic components if a response is temporarily incomplete. Diagnostic
components are determined by configuration, never by whether one individual poll happened to
contain a value.

## Optimistic command and reconciliation flow

```mermaid
sequenceDiagram
    participant Client as MQTT client
    participant MQTT as MQTT broker
    participant Bridge as BridgeService
    participant Worker as UnitWorker
    participant Adapter as PyairstageLocalUnit
    participant AC as AirStage local API

    Client->>MQTT: Publish /set or /set/property
    MQTT->>Bridge: Deliver non-retained command
    Bridge->>Bridge: Decode JSON and select unit
    Bridge->>Worker: Queue command task
    Worker->>Worker: Cancel pending poll deadline
    Worker->>Worker: Acquire per-unit lock
    opt No prior snapshot exists
        Worker->>Adapter: Obtain baseline snapshot
        Adapter->>AC: Read current state
        AC-->>Adapter: Device state
        Adapter-->>Worker: Normalized baseline
    end
    Worker->>Adapter: Accept command against current snapshot
    Adapter-->>Worker: Canonical command + optimistic snapshot
    Worker->>Worker: Install optimistic snapshot
    Worker->>Bridge: Notify state listener before device write
    Bridge->>MQTT: Publish retained optimistic state if changed
    Worker->>Adapter: Apply accepted command
    Adapter->>AC: Write operations in safe order
    AC-->>Adapter: Write response
    Adapter-->>Worker: Write completed or failed
    Worker->>Worker: Schedule reconciliation in finally
    Worker->>Worker: Release lock
    Note over Worker: Wait command_refresh_delay_seconds
    Worker->>Adapter: Reconciliation poll
    Adapter->>AC: Read current state
    AC-->>Adapter: Actual device state
    Adapter-->>Worker: Normalized actual snapshot
    Worker->>Bridge: Notify state listener
    alt Actual snapshot equals optimistic snapshot
        Bridge->>Bridge: Suppress duplicate MQTT state
    else Actual snapshot differs
        Bridge->>MQTT: Publish corrected retained state
    end
    Worker->>Worker: Resume regular poll interval
```

The same lock covers normal polling and command execution, so they cannot interleave requests to
one unit. A pending poll is cancelled as soon as a command task starts; an already-running poll is
allowed to finish before the command acquires the lock. Multi-property commands are first
validated and normalized as one operation. The modeled snapshot preserves uncommanded readings
and capabilities while resolving values such as `TOGGLE`, enum aliases, Boolean controls,
temperature rounding, and power/mode side effects.

Listeners see that modeled snapshot before any device write. The write is then ordered safely—for
example, power-on can precede mode and temperature changes, while a requested power-off is applied
last. Whether the write succeeds or fails, the worker schedules an authoritative read after
`polling.command_refresh_delay_seconds` (two seconds by default). An identical full snapshot is
suppressed by the normal publish-on-change cache; any difference, including an unrelated sensor
change, is published.

Each new accepted command replaces the pending reconciliation deadline, so a burst of commands
settles with one poll after the final write. An explicit `/get` has priority over that delay and
polls as soon as the unit lock becomes available. After reconciliation completes, the next poll is
scheduled using `polling.interval_seconds`.

Invalid, unsupported, and read-only properties are rejected before listeners are notified or the
device is written. If no prior snapshot exists—for example, for a unit that was unreachable at
startup—the worker must first obtain a baseline so it can preserve a complete state document and
validate capabilities. A failed baseline read rejects the command without inventing state.

## Availability, disconnects, and restarts

```mermaid
stateDiagram-v2
    [*] --> Disconnected
    Disconnected --> Initializing: MQTT connected
    Initializing --> Online: all initial unit polls resolved
    Online --> Disconnected: MQTT connection lost
    Online --> Stopping: graceful shutdown requested
    Stopping --> Disconnected: bridge offline published
    Disconnected --> Initializing: reconnect backoff expires

    state Initializing {
        [*] --> UnitsUnknown
        UnitsUnknown --> UnitsEstablished: each unit online or offline
        UnitsEstablished --> [*]
    }
```

The MQTT connection declares a retained `offline` last will. Mosquitto publishes it if the process,
container, or network connection disappears unexpectedly. On a graceful shutdown, A2M publishes
bridge `offline` itself before publishing per-unit offline transitions.

After MQTT failure, A2M reconnects with bounded exponential backoff and repeats the complete
initialization gate. In-memory publish caches are reset on each new MQTT session so retained state,
availability, discovery, and bridge information can repopulate a restarted broker.

Home Assistant uses both bridge and unit availability with `availability_mode: all`. Consequently:

- All A2M entities are unavailable while the bridge is initializing, disconnected, or stopped.
- Once the bridge is online, each A/C independently follows its unit availability topic.
- A runtime unit is marked offline only after `polling.offline_after_failures` consecutive failures.
- A later successful poll publishes current state before transitioning that unit back online.

## Home Assistant discovery lifecycle

```mermaid
flowchart LR
    config["Unit config<br/>friendly name + diagnostics"]
    snapshot["Current snapshot<br/>stable capabilities"]
    build["Build one device-discovery document"]
    prior["Prior discovery document<br/>in /data"]
    diff{"Document changed?"}
    removed{"Diagnostic component removed?"}
    tombstone["Publish intermediate config<br/>removed component = platform only"]
    final["Publish final retained config"]
    skip["Publish nothing"]
    save["Persist topic, device ID,<br/>and final document in /data"]

    config --> build
    snapshot --> build
    prior --> diff
    build --> diff
    diff -- No --> skip
    diff -- Yes --> removed
    removed -- Yes --> tombstone --> final
    removed -- No --> final
    final --> save
```

One retained Home Assistant device-discovery topic represents each physical A/C and contains its
climate entity, supported sensors and switches, configured diagnostics, and a bridge-version
diagnostic sourced from the retained bridge information. Identical discovery is suppressed during
a connection. It is offered again after an A2M MQTT reconnect so broker data can be repaired; an
unchanged Home Assistant birth message does not cause repeated discovery churn.

The configurable diagnostics—`error_code`, `demand`, and `power_consumption`—are disabled by
default. Enabling one adds a diagnostic entity even if the current reading is absent; the entity
then remains stable and reports an unknown value until the unit supplies data. Disabling one uses
[Home Assistant's explicit component-removal update](https://www.home-assistant.io/integrations/mqtt/#device-discovery-payload),
followed by the final document without that component.

## Persistence boundaries

```mermaid
flowchart TB
    command["Accepted MQTT command"]
    ac[("A/C unit<br/>authoritative actual state")]
    runtime["A2M memory<br/>modeled current snapshots<br/>and dedup caches"]
    broker[("Mosquitto retained store<br/>current state, availability, discovery, manifest")]
    data[("A2M /data<br/>discovery ownership and schema index")]
    recorder[("Home Assistant recorder<br/>optional history")]

    command -->|optimistic model| runtime
    runtime -->|device write| ac
    ac -->|authoritative reconciliation poll| runtime
    runtime -->|changed current values| broker
    runtime -->|discovery index only| data
    broker --> recorder
```

A2M deliberately does not persist readings or command history in `/data`. In memory and MQTT, an
accepted command is treated as the current modeled state until a later device poll confirms or
corrects it. The A/C remains the authoritative source for reconciliation; Mosquitto retains the
latest published model, and Home Assistant's recorder owns historical data. Mosquitto persistence
is therefore needed if retained MQTT messages must survive a broker restart.

Persisting `/data` is still recommended. It is not needed for live control, but it allows A2M to
remember discovery ownership and schema across container replacement, including clean component
removal and cleanup after topic or unit configuration changes.

## Container and security boundary

The production container runs as a non-root user with a read-only root filesystem, dropped Linux
capabilities, `no-new-privileges`, and a small writable `/tmp` used for the health heartbeat. Its
only persistent write is the discovery index under `/data`. Configuration is mounted read-only,
and the MQTT password can be supplied through a separately mounted secret file.

No deployment-specific MQTT credentials, A/C readings, MAC addresses, unit IPs, or
network-specific registry values are built into the image or committed examples.
