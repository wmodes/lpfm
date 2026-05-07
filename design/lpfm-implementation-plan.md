# LPFM Station Infrastructure Implementation Plan
*Raspberry Pi 3B+ Audio Pipeline and Transmitter Control*

# 1. Purpose

This project builds the infrastructure for a Low-Power FM (LPFM) radio station running on a Raspberry Pi 3B+. The station acquires a live audio stream over wifi, routes it to a USB audio dongle connected to an FM transmitter, and controls the transmitter's power via a wifi-connected relay. The system is designed to be reliable, unattended, and self-healing — automatically recovering from stream dropouts or process failures without manual intervention.

The implementation is intentionally generalizable. While designed for a specific station setup, the architecture and configuration patterns should be reusable by anyone building a similar low-power broadcast system.

Logging is handled entirely through systemd and journalctl, which are standard on Debian-based systems and require no additional infrastructure.

---

# 2. Goals and Scope

## 2.1. Goals

1. **Acquire a live audio stream** reliably over wifi, with buffering to smooth over brief network hiccups.
2. **Route audio to the USB dongle** configured as the system audio output device feeding the FM transmitter.
3. **Control the transmitter via a wifi power relay** — switching it on and off cleanly under software control.
4. **Apply scheduling and heuristics** to determine when the transmitter should be active (time-of-day rules, stream health as a gate).
5. **Interlock transmitter power with stream health** — never activate the transmitter without confirmed good audio.
6. **Monitor the stream and recover from failure** — detect dropouts, restart the stream fetcher, and fall back to local audio if needed.
7. **Manage all processes via systemd** — auto-start on boot, automatic restarts on crash, journalctl logging with rotation.
8. **Drive everything from a single config file** — stream URL, schedule, relay address, fallback settings, and tuning parameters all in one place.
9. **Allow remote access via SSH** for monitoring, control, and troubleshooting.

## 2.2. Why These Goals Matter

An unattended broadcast system has to be self-sustaining. Without stream monitoring and automatic recovery, a brief network dropout leaves the transmitter radiating silence. Without the interlock, a crashed stream process leaves the transmitter on but carrying nothing. Without systemd management, a power cycle requires manual intervention to restart.

The single config file and remote SSH access ensure that operational changes (adjusting schedule, swapping stream URL, tuning buffer sizes) can be made without touching the code.

## 2.3. Scope

This implementation covers:

- **Stream acquisition:** fetching and buffering a remote audio stream.
- **Audio routing:** configuring the USB audio dongle as the system output and piping stream audio to it.
- **Relay control:** sending on/off commands to a wifi power relay over the local network.
- **Scheduler:** time-based rules determining active broadcast windows.
- **Heuristics engine:** stream health check as a gate on transmitter activation.
- **Watchdog:** detecting stream failure and triggering recovery or fallback.
- **Fallback:** playing local audio files when the stream is unavailable.
- **Systemd services:** unit files for each component, with dependency ordering and restart policies.
- **Configuration:** a single config file for all tunable parameters.
- **Remote access:** SSH enabled, no additional tooling required.

## 2.4. Out of Scope

- Audio level normalization or dynamic range compression.
- Silence detection or dead-air protection.
- Transmitter hardware warm-up/cool-down timing.
- A web UI or API (SSH is sufficient for remote control).
- FCC compliance verification (the operator's responsibility).

---

# 3. Development Standards

## 3.1. Language

Python 3.x throughout. Chosen for its strong Raspberry Pi ecosystem, readable syntax, and ease of writing platform stubs for macOS development.

## 3.2. Code Style

- **Readable over clever** — code should be immediately understandable; prioritize clarity over concision.
- **Well-commented** — explain the *why*, not the what. Comments address intent, constraints, and non-obvious decisions.
- **OOP throughout** — classes for all meaningful components (stream fetcher, relay controller, scheduler, watchdog, etc.), as reasonable.
- **Parallel identifiers** — class names, method names, config keys, and log messages all use the same conceptual vocabulary developed for the project (e.g., if the concept is "broadcast window," that term appears consistently everywhere — not "time slot" in one place and "active period" in another).
- **No hardcoded values** — every tunable parameter lives in the config file. Code contains no magic numbers, URLs, paths, or timing values.

## 3.3. Documentation

Python equivalent of JSDoc is **Google-style docstrings**, used consistently:

- **File headers** — every module begins with a module-level docstring: purpose, author, and a brief description of its role in the system.
- **Class docstrings** — describe what the class represents and its responsibilities.
- **Method/function docstrings** — describe purpose, args (with types), return value, and any raised exceptions.

Example pattern:
```python
def connect(self, url: str, retries: int = 3) -> bool:
    """Establish a connection to the audio stream.

    Args:
        url: The stream URL to connect to.
        retries: Number of reconnection attempts before signaling failure.

    Returns:
        True if connection succeeded, False otherwise.

    Raises:
        StreamConfigError: If the URL is malformed or unsupported.
    """
```

## 3.4. Architecture Principles

- **Separation of concerns** — each module has one job. No module reaches into another's internals.
- **Dependency injection over globals** — components receive their config and dependencies at construction time.
- **Platform abstraction** — all Pi-specific calls (audio device, relay control, systemd notify) go through an interface layer so macOS stubs can be swapped in transparently during development and testing.

## 3.5. Directory Structure

```
lpfm/
├── config/
│   └── config.toml          # all runtime parameters; no defaults in code
├── lpfm/
│   ├── __init__.py
│   ├── stream_fetcher.py    # stream acquisition and buffering
│   ├── audio_router.py      # audio device configuration and routing
│   ├── relay_controller.py  # wifi relay on/off commands
│   ├── scheduler.py         # broadcast window logic
│   ├── watchdog.py          # stream health monitoring and recovery
│   ├── fallback_player.py   # local audio fallback
│   └── config_loader.py     # config parsing and validation
├── platform/
│   ├── __init__.py
│   ├── raspi.py             # real Pi hardware interfaces
│   └── macos_stub.py        # development stubs for macOS
├── tests/
│   └── ...
├── systemd/
│   └── *.service            # unit files for deployment
├── requirements.txt
└── main.py                  # entry point; wires components together
```

## 3.6. Configuration

A single `config/config.toml` file holds all parameters. No defaults are buried in code — if a value isn't in config, the system raises a clear error at startup. Config is loaded once and passed to all components at construction time.

## 3.7. Dependency Management

Dependencies tracked in `requirements.txt`. A `requirements-dev.txt` covers development-only packages (testing, linting). Target environment is Python 3.9+ to match current Raspberry Pi OS availability.

---

# 4. Hardware and Environment Baseline

## 4.1. Hardware

| Component | Role |
|---|---|
| Raspberry Pi 3B+ | Main controller and audio router |
| USB audio dongle | Analog audio output to FM transmitter input |
| WiFi power relay | Remotely switches transmitter power via HTTP or MQTT |
| FM transmitter | Accepts analog audio input; powered by relay |
| Network (wifi) | Carries the incoming stream and relay control traffic |

## 4.2. Software Environment

- **Target OS:** Raspberry Pi OS (Debian-based), current stable release.
- **Init system:** systemd — all services managed as units.
- **Logging:** journalctl with automatic log rotation via systemd's journal size limits.
- **Audio subsystem:** ALSA or PulseAudio; USB dongle configured as default output device.
- **Development environment:** macOS, with stubs standing in for Pi-specific hardware controls (relay, audio device) and faux systemd wrappers for local testing.

## 4.3. Starting Point

The Pi boots a fresh OS install. No station software is present. The USB dongle and wifi relay are physically connected. The stream source URL is known. The relay's local IP and control interface are known.

---

# 5. System Architecture

## 5.1. Components

**stream-fetcher**
Connects to the remote stream URL, buffers incoming audio, and pipes it to the audio output. Restarts automatically on disconnect. On repeated failure, signals the watchdog to activate fallback mode.

**audio-router**
Configures the USB dongle as the active ALSA/PulseAudio output device. In normal operation this is static configuration, not a running process.

**relay-controller**
Issues HTTP (or MQTT) commands to the wifi power relay to switch the transmitter on or off. Called by the scheduler and the interlock logic. Stateless — each call is a single command.

**scheduler**
Runs on a timer. Consults the config file for active broadcast windows (time-of-day rules). At the start of a window, checks stream health before activating the transmitter. At the end of a window, deactivates the transmitter regardless of stream state.

**stream-watchdog**
Monitors the stream-fetcher process and audio output for signs of failure (process death, stalled bytes, repeated reconnection attempts). On confirmed failure, triggers recovery (restart stream-fetcher) or, after N retries, switches to fallback audio.

**fallback-player**
Plays a local audio file or playlist through the USB dongle when the stream is unavailable. Deactivates and yields to stream-fetcher when the stream recovers.

**config**
A single file (e.g., `config.toml`) holding all tunable parameters: stream URL, broadcast schedule, relay address and credentials, buffer size, retry limits, fallback audio path, and log verbosity.

## 5.2. Audio Pipeline Flow

```
[REMOTE STREAM SOURCE]
        |
        | (network / wifi)
        v
[stream-fetcher]  ←── restarts on failure (systemd)
        |
        | (piped audio)
        v
[USB audio dongle]  ←── configured as default ALSA output
        |
        | (analog audio signal)
        v
[FM transmitter input]
        |
        | (RF broadcast — only when relay is ON)
        v
[ON AIR]
```

## 5.3. Transmitter Control Flow

```
[scheduler]  (runs on timer, reads config)
        |
        |── Is current time within a broadcast window?
        |
        |── YES:
        |       |
        |       v
        |   [stream-watchdog: health check]
        |       |
        |       |── Stream healthy?
        |       |
        |       |── YES → [relay-controller: ON]  →  transmitter powers on
        |       |
        |       └── NO  → hold off; retry after interval
        |
        └── NO:
                v
            [relay-controller: OFF]  →  transmitter powers off
```

## 5.4. Failure and Recovery Flow

```
[stream-fetcher]
        |
        |── Dropout or crash detected
        |
        v
[stream-watchdog]
        |
        |── Attempt restart (up to N retries)
        |       |
        |       |── Recovery SUCCESS → resume normal pipeline
        |       |
        |       └── Still failing after N retries:
        |               |
        |               v
        |           [relay-controller: OFF]  ←── interlock: transmitter off
        |               |
        |               v
        |           [fallback-player: ON]  ←── local audio begins
        |               |
        |               └── stream-watchdog continues polling
        |                       |
        |                       └── Stream recovers:
        |                               |
        |                               v
        |                           [fallback-player: OFF]
        |                           [stream-fetcher: restart]
        |                           [relay-controller: ON if in window]
        v
[journalctl logs all transitions]
```

---

# 6. Strategic Options and Staged Approach

## 6.1. Approach: Modular Services

Each component is its own systemd service with defined dependencies. systemd handles ordering, restart policies, and logging for each independently.

- Components restart independently — a stream-fetcher crash doesn't affect the relay controller.
- systemd dependency ordering ensures correct startup sequence.
- Each service logs independently to journalctl; easy to isolate failures.
- Easy to stop, test, or replace one service without touching the others.
- Inter-process coordination via a small shared status mechanism (e.g., a state file or lightweight IPC).

## 6.2. Staged Implementation

The modular approach is built incrementally. Each phase is independently testable before moving to the next.

**Phase 1: Audio Pipeline**
- Configure USB dongle as default ALSA output.
- Install and configure stream-fetcher; verify audio plays through dongle.
- No transmitter involvement yet — test purely with headphones or a line-level meter.

**Phase 2: Systemd Integration**
- Write systemd unit file for stream-fetcher with auto-restart on failure.
- Verify journalctl logging works and log rotation is configured.
- Test that stream-fetcher survives a Pi reboot and an intentional kill.

**Phase 3: Relay Control**
- Implement relay-controller script; test on/off commands to the wifi relay independently.
- Confirm transmitter powers on and off cleanly.
- Wire relay-controller into manual invocation (SSH command) before automating it.

**Phase 4: Scheduler and Heuristics**
- Implement scheduler with time-of-day rules from config file.
- Add stream-health check as a gate before relay-controller ON.
- Test schedule transitions: transmitter activates at window start, deactivates at window end.

**Phase 5: Watchdog and Fallback**
- Implement stream-watchdog to monitor stream-fetcher health.
- Implement fallback-player for local audio on stream failure.
- Wire interlock: transmitter goes off when fallback activates.
- Test full failure-and-recovery cycle end-to-end.

**Phase 6: Hardening and Config**
- Consolidate all parameters into single config file.
- Review systemd unit dependencies and restart policies.
- Confirm SSH access and remote log tailing work as expected.
- Document the setup for reproducibility.
