# LPFM

*LPFM* is a low-power FM station that broadcasts a live internet radio stream over the air — automatically, unpredictably, and with a light touch of operational paranoia.

A Raspberry Pi grabs an audio stream from an Icecast server, routes it through a USB audio dongle into an FM transmitter, and controls transmitter power via a wifi-connected relay. The station doesn't run on a fixed schedule. Instead, a probabilistic risk model decides each morning whether to broadcast that night, and if so, picks random start and stop times within a configured window. Risk accumulates across days with exponential decay, so a long late-night session nudges the station toward caution — but only for a while.

---

## How It Works

Each morning at a configured decision time, the scheduler:

1. Calculates **accumulated risk** from recent broadcasts (exponential decay — risk from last night still matters, but fades over ~3 days)
2. Computes a **broadcast probability** (`1 − accumulated_risk`)
3. Rolls the dice
4. If broadcasting: picks a random **start time** (within leeway of window open) and **stop time** (within leeway of window close), sends an email notification
5. If not: sends a "dark tonight" notification and waits until tomorrow

The transmitter fires up at the decided start time and cuts at the decided stop time — no manual intervention needed. A watchdog monitors the stream continuously and cuts the transmitter if the stream dies, activating a local fallback player until the stream recovers.

---

## Architecture

```
main.py
├── StreamFetcher     grabs Icecast stream → USB audio dongle via ffmpeg
├── RelayController   controls FM transmitter power (Shelly 1 Mini Gen4 wifi relay)
├── Scheduler         daily probabilistic broadcast decision + timing
├── Watchdog          stream health monitor + recovery coordinator
├── FallbackPlayer    plays local audio when stream is unavailable
└── Notifier          sends email alerts at decision time
```

All components run as background threads inside a single Python process, managed by systemd on the Pi.

### Components

**StreamFetcher** — Spawns an ffmpeg subprocess that pulls the Icecast stream and routes audio to the configured output device (ALSA on Pi, audiotoolbox on macOS). Stderr is drained in a daemon thread to prevent pipe buffer stalls. Supports reconnect on stream drop.

**RelayController** — Sends HTTP commands to a Shelly 1 Mini Gen4 relay via its RPC API (`/rpc/Switch.Set`). Verifies state after each command with configurable retries before declaring success or failure.

**Scheduler** — Background thread that sleeps until the next scheduled event (decision time, broadcast start, or broadcast stop) rather than polling. Persists decisions and accumulated risk to `state/scheduler.json` so the risk memory survives restarts. Appends a record to `state/history.jsonl` for each decision. Handles midnight-crossing broadcast windows correctly.

**Watchdog** — Polls the stream fetcher at a fixed interval. Tracks consecutive failures with a cooldown between restart attempts. On stream failure beyond the retry limit: cuts the relay, starts the fallback player. On recovery: stops fallback, restores relay if inside the broadcast window.

**FallbackPlayer** — Scans a local audio directory and plays files in shuffled order via ffmpeg, cycling indefinitely until stopped.

**Notifier** — Sends plain-text email via Gmail SMTP at decision time: broadcast schedule and risk metrics if on air, or a dark-tonight notice if not.

---

## Risk Model

Each broadcast generates a risk score from four weighted factors:

| Factor | Higher risk when… |
|--------|-------------------|
| `start_risk` | broadcast starts earlier in the window |
| `stop_risk` | broadcast runs later into the window |
| `duration_risk` | broadcast runs longer |
| `day_risk` | broadcast falls on a higher-risk day of week |

Weights are configured as relative values and normalized at runtime — no need to sum to 1.0.

Risk accumulates across days:

```
accumulated_risk = last_broadcast_risk + decay_factor × prev_accumulated_risk
broadcast_probability = max(0, 1 − accumulated_risk)
```

A `broadcast_threshold` can hard-block broadcasts above a given risk level. Set to `0` to disable.

---

## Configuration

All behavioral settings live in `config/config.toml`. Sensitive and machine-specific values live in `.env` (never committed).

### config/config.toml

```toml
[scheduler]
decision_time = "08:00"              # time of day to make the daily broadcast decision
window_start = "19:00"               # earliest the broadcast may begin
window_end = "06:00"                 # latest the broadcast may end (crosses midnight)
start_leeway_max_minutes = 240       # random start is picked within this offset from window_start
stop_leeway_max_minutes = 240        # random stop is picked within this offset back from window_end

[risk]
decay_factor = 0.5                   # risk fades to ~12% of original after 3 broadcast-free days
broadcast_threshold = 0.85           # hard block above this accumulated risk (0 to disable)
weight_start = 1.0                   # relative weight — normalized at runtime
weight_stop = 0.8
weight_duration = 0.8
weight_day = 0.5

[risk.day_weights]
monday    = 1.0
tuesday   = 0.8
wednesday = 0.6
thursday  = 0.4
friday    = 0.2
saturday  = 0.0
sunday    = 0.0
```

### .env (copy from setupfiles/env)

```bash
LPFM_STREAM_URL=https://your-icecast-server/stream
LPFM_AUDIO_FORMAT=alsa          # alsa on Pi, audiotoolbox on macOS
LPFM_AUDIO_DEVICE=hw:1,0        # USB dongle on Pi (verify with: aplay -l)
LPFM_RELAY_URL=http://192.168.1.100
LPFM_RELAY_ON_PATH=/rpc/Switch.Set?id=0&on=true
LPFM_RELAY_OFF_PATH=/rpc/Switch.Set?id=0&on=false
LPFM_RELAY_STATUS_PATH=/rpc/Switch.GetStatus?id=0
LPFM_SMTP_HOST=smtp.gmail.com
LPFM_SMTP_PORT=587
LPFM_SMTP_USER=your@gmail.com
LPFM_SMTP_PASSWORD=xxxx xxxx xxxx xxxx   # Gmail app password
LPFM_NOTIFY_EMAIL=your@gmail.com
```

---

## Development Setup

Developed on macOS, deployed on Raspberry Pi 3B+. The audio output format is the only platform difference — everything else is identical.

### Prerequisites

- Python 3.9+
- ffmpeg installed system-wide (`brew install ffmpeg` on macOS)
- A Gmail account with an [app password](https://myaccount.google.com/apppasswords) for notifications

### Running locally

```bash
git clone https://github.com/wmodes/lpfm
cd lpfm
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp setupfiles/env .env
# edit .env with your values

python main.py
```

Set `LPFM_AUDIO_FORMAT=audiotoolbox` and `LPFM_AUDIO_DEVICE=default` in `.env` on macOS.

---

## Pi Deployment

### Prerequisites

```bash
sudo apt update && sudo apt install -y ffmpeg python3-venv
```

### Install

```bash
cd ~
git clone https://github.com/wmodes/lpfm
cd lpfm
python3 -m venv venv
venv/bin/pip install -r requirements.txt

cp setupfiles/env .env
# edit .env — set LPFM_AUDIO_FORMAT=alsa, LPFM_AUDIO_DEVICE=hw:1,0
# verify USB dongle device name with: aplay -l
```

### Find the USB audio dongle

```bash
aplay -l
# look for USB Audio entry, e.g. card 1, device 0 → hw:1,0
```

### Systemd service

```bash
sudo ln -sf /home/pi/lpfm/setupfiles/lpfm.service /etc/systemd/system/lpfm.service
sudo systemctl daemon-reload
sudo systemctl enable lpfm.service
sudo systemctl start lpfm.service
```

```bash
# View live logs
journalctl -u lpfm.service -f

# Restart after config changes
sudo systemctl restart lpfm.service
```

### After git pull

```bash
cd ~/lpfm && git pull
sudo systemctl restart lpfm.service
```

---

## State Files

```
state/scheduler.json    current accumulated risk and today's broadcast decision
state/history.jsonl     one JSON record per daily decision — full history
```

The state directory is gitignored. Back it up if risk history matters to you.

---

## Hardware

- **Raspberry Pi 3B+** — runs the station software
- **USB audio dongle** — audio output to FM transmitter input
- **FM transmitter** — broadcasts on your licensed LPFM frequency
- **Shelly 1 Mini Gen4** — wifi relay controlling transmitter power

---

## Technologies

- **Python 3.9+**
- **ffmpeg** — audio stream routing and fallback playback
- **tomli** — TOML config parsing (Python 3.9 backport)
- **python-dotenv** — `.env` loading
- **requests** — Shelly relay HTTP API
- **smtplib** (stdlib) — email notifications
- **threading** (stdlib) — background thread coordination
- **systemd** — service management on Pi

---

## Author

**Wesley Modes**  
University of Cincinnati  
ORCID: [0009-0000-1191-8245](https://orcid.org/0009-0000-1191-8245)

---

## License

MIT — see `MIT-LICENSE.txt`
