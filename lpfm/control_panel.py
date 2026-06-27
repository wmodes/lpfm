"""Control panel — web interface for monitoring and controlling the station.

Runs a small Flask server in a background daemon thread. Provides a single-page
dashboard showing broadcast history, current risk, tonight's schedule, and
controls for overriding the stream URL, editing the schedule, and triggering
an emergency shutoff.

Accessible at http://lpfm.local:<port> on the local network.
"""

import json
import logging
import threading

from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template_string, request

from lpfm.config_loader import ControlPanelConfig, SchedulerConfig, StreamConfig


TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LPFM Control Panel</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: monospace; background: #111; color: #ddd; padding: 24px; max-width: 860px; margin: 0 auto; }
  h1 { color: #f90; font-size: 1.4em; border-bottom: 1px solid #333; padding-bottom: 12px; margin-bottom: 24px; }
  h2 { color: #aaa; font-size: 0.8em; text-transform: uppercase; letter-spacing: 3px; margin: 28px 0 10px; }
  .card { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 4px; padding: 20px; }
  .card.danger { border-color: #600; }
  label { color: #aaa; font-size: 0.8em; display: block; margin-bottom: 5px; }
  input[type=time], input[type=text], select {
    background: #222; border: 1px solid #444; color: #eee;
    padding: 7px 10px; border-radius: 3px; font-family: monospace; font-size: 0.95em;
  }
  input[type=text] { width: 100%; }
  .row { display: flex; gap: 16px; align-items: flex-start; flex-wrap: wrap; margin-top: 14px; }
  .group { display: flex; gap: 16px; align-items: flex-start; flex-shrink: 0; }
  .field { display: flex; flex-direction: column; }
  .field-grow { flex: 1; min-width: 200px; }
  .btn { padding: 7px 18px; border: none; border-radius: 3px; cursor: pointer; font-family: monospace; font-size: 0.95em; }
  .btn-save { background: #1a5c1a; color: #8f8; }
  .btn-save:hover { background: #256325; }
  .btn-shutoff { background: #7a0000; color: #faa; padding: 7px 18px; }
  .btn-shutoff:hover { background: #990000; }
  .btn-restore { background: #0a4a0a; color: #8f8; padding: 7px 18px; }
  .btn-restore:hover { background: #0d600d; }
  .shutoff-banner { color: #f66; font-size: 0.85em; margin-top: 10px; }
  .manual-row { display: flex; align-items: center; flex-wrap: wrap; gap: 32px; }
  .status-wrap { display: flex; align-items: center; gap: 8px; white-space: nowrap; }
  .status-label { color: #aaa; font-size: 0.9em; }
  .toggle-wrap { display: flex; align-items: center; gap: 10px; }
  .toggle-label { color: #aaa; font-size: 0.85em; }
  .toggle-state { font-size: 0.85em; min-width: 2.5em; }
  .toggle-switch { position: relative; display: inline-block; width: 52px; height: 26px; }
  .toggle-switch input { opacity: 0; width: 0; height: 0; }
  .toggle-knob { position: absolute; cursor: pointer; inset: 0; background: #333; border-radius: 26px; transition: 0.25s; }
  .toggle-knob:before { content: ""; position: absolute; width: 20px; height: 20px; left: 3px; top: 3px; background: #aaa; border-radius: 50%; transition: 0.25s; }
  input:checked + .toggle-knob { background: #1a5c1a; }
  input:checked + .toggle-knob:before { transform: translateX(26px); background: #8f8; }
  .shutoff-form { flex: 1; min-width: 160px; }
  .btn-shutoff { background: #7a0000; color: #faa; padding: 7px 18px; width: 100%; }
  .btn-restore-full { background: #0a4a0a; color: #8f8; padding: 7px 18px; width: 100%; }
  .risk-bar { background: #2a2a2a; height: 6px; border-radius: 3px; overflow: hidden; margin: 8px 0 4px; }
  .risk-fill { height: 100%; background: linear-gradient(to right, #2a2, #aa2, #a22); transition: width 0.3s; }
  .risk-value { font-size: 1.6em; color: #f90; }
  .prob { color: #aaa; font-size: 0.85em; }
  table { width: 100%; border-collapse: collapse; font-size: 0.9em; }
  th { text-align: left; color: #aaa; font-weight: normal; padding: 6px 10px 8px; border-bottom: 1px solid #2a2a2a; }
  td { padding: 7px 10px; border-bottom: 1px solid #1e1e1e; }
  .on-air { color: #6f6; }
  .off-air { color: #aaa; }
  .separator { margin: 6px 0; }
</style>
</head>
<body>

<h1>LPFM Control Panel</h1>

<!-- Transmitter control -->
<h2>Transmitter Control</h2>
<div class="card {% if shutoff %}danger{% endif %}">
  <div class="manual-row">
    <div class="status-wrap">
      <span class="status-label">Status</span>
      <span id="relay-status" class="{{ 'on-air' if transmitting else 'off-air' }}" style="font-size:0.9em">
        {{ '● on air' if transmitting else '○ dark' }}
      </span>
    </div>
    <div class="toggle-wrap">
      <span class="toggle-label">Transmitter</span>
      <form method="post" action="/api/transmitter">
        <label class="toggle-switch" title="{% if transmitting %}Turn transmitter off{% else %}Turn transmitter on{% endif %}">
          <input type="checkbox" onchange="syncToggleLabel(this); this.form.submit()" {% if transmitting %}checked{% endif %}>
          <span class="toggle-knob"></span>
        </label>
      </form>
      <span class="toggle-state {% if transmitting %}on-air{% else %}off-air{% endif %}">
        {% if transmitting %}ON{% else %}OFF{% endif %}
      </span>
    </div>
    <form class="shutoff-form" method="post" action="/api/shutoff"
          onsubmit="return confirm('{% if shutoff %}Restore transmission?{% else %}Emergency shutoff — are you sure?{% endif %}')">
      <button type="submit" class="btn {% if shutoff %}btn-restore-full{% else %}btn-shutoff{% endif %}">
        {% if shutoff %}⚡ RESTORE TRANSMISSION{% else %}⚠ EMERGENCY SHUTOFF{% endif %}
      </button>
    </form>
  </div>
  {% if shutoff %}<p class="shutoff-banner">Transmission is currently suspended.</p>{% endif %}
</div>
<script>
function validateSchedule(form) {
  if (form.broadcasting.value !== 'true') return true;
  var start = document.getElementById('sched-start');
  var stop  = document.getElementById('sched-stop');
  var ok = true;
  [start, stop].forEach(function(el) {
    if (!el.value) { el.style.outline = '2px solid #c33'; ok = false; }
    else { el.style.outline = ''; }
  });
  return ok;
}
function syncToggleLabel(cb) {
  var wrap = cb.closest('.toggle-wrap');
  var state = wrap.querySelector('.toggle-state');
  state.textContent = cb.checked ? 'ON' : 'OFF';
  state.className = 'toggle-state ' + (cb.checked ? 'on-air' : 'off-air');
}
function updateRelayStatus() {
  fetch('/api/relay-status')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var el = document.getElementById('relay-status');
      if (data.on) {
        el.className = 'on-air'; el.textContent = '● on air';
      } else {
        el.className = 'off-air'; el.textContent = '○ dark';
      }
    })
    .catch(function() {});
}
updateRelayStatus();
setInterval(updateRelayStatus, 120000);
</script>

<!-- Tonight's broadcast -->
<h2>Tonight's Broadcast</h2>
<div class="card">
  <!-- Reroll form must live outside the schedule form (no nested forms) -->
  <form id="reroll-form" method="post" action="/api/reroll"></form>
  <form method="post" action="/api/schedule" onsubmit="return validateSchedule(this)">
    <div class="row">
      <div class="group">
        <div class="field">
          <label>&nbsp;</label>
          <button type="submit" form="reroll-form" class="btn"
                  style="background:#1a1a4a;color:#88f;border:1px solid #446"
                  title="Re-run today's broadcast decision with a new random roll">↺ Reroll</button>
        </div>
        <div class="field">
          <label>Broadcasting</label>
          <select name="broadcasting">
            <option value="true"  {% if today.get('broadcasting') %}selected{% endif %}>Yes</option>
            <option value="false" {% if not today.get('broadcasting') %}selected{% endif %}>No</option>
          </select>
        </div>
      </div>
      <div class="group">
        <div class="field">
          <label>Start</label>
          <input type="time" name="start" id="sched-start" value="{{ today.get('start', '') }}">
        </div>
        <div class="field">
          <label>Stop</label>
          <input type="time" name="stop" id="sched-stop" value="{{ today.get('stop', '') }}">
        </div>
        <div class="field">
          <label>&nbsp;</label>
          <button type="submit" class="btn btn-save">Save Schedule</button>
        </div>
      </div>
    </div>
  </form>

  <div class="separator"></div>

  <form method="post" action="/api/stream" onsubmit="return validateStreamUrl(this)">
    <div class="row">
      <div class="field field-grow">
        <label>Stream URL (tonight only — resets after broadcast)</label>
        <input type="text" name="url" id="stream-url" value="{{ today.get('stream_url_override', default_stream) }}">
      </div>
      <div class="field">
        <label>&nbsp;</label>
        <button type="submit" class="btn btn-save">Set Stream</button>
      </div>
      <div class="field">
        <label>&nbsp;</label>
        <button type="button" class="btn" style="background:#222;color:#aaa;border:1px solid #444"
                onclick="document.getElementById('stream-url').value='{{ default_stream }}'"
                title="Restore default stream URL">Reset</button>
      </div>
    </div>
    <p style="color:#aaa;font-size:0.75em;margin-top:6px">
      HTTP/HTTPS direct stream only (Icecast, SHOUTcast) — not .m3u/.pls playlists
    </p>
  </form>
<script>
function validateStreamUrl(form) {
  var url = form.url.value.trim();
  if (!url.match(/^https?:[/][/]/i)) {
    alert('URL must start with http:// or https://');
    return false;
  }
  if (url.match(/[.](m3u|pls|xspf)([?].*)?$/i)) {
    return confirm('This looks like a playlist file, not a direct stream — ffmpeg may not handle it correctly. Continue anyway?');
  }
  return true;
}
</script>
</div>

<!-- Risk -->
<h2>Accumulated Risk</h2>
<div class="card">
  <span class="risk-value">{{ "%.3f"|format(accumulated_risk) }}</span>
  <div class="risk-bar">
    <div class="risk-fill" style="width:{{ [accumulated_risk * 100, 100]|min|int }}%"></div>
  </div>
  <span class="prob">Broadcast probability: {{ "%.0f"|format([1.0 - accumulated_risk, 0.0]|max * 100) }}%</span>
</div>

<!-- History -->
<h2>Recent History</h2>
<div class="card">
  {% if history %}
  <table>
    <tr>
      <th>Date</th>
      <th>Start</th>
      <th>Stop</th>
      <th>Risk</th>
      <th>Acc. Risk</th>
      <th>Status</th>
    </tr>
    {% for entry in history %}
    <tr>
      <td>{{ entry.today.get('date', '—') }}</td>
      <td>{{ entry.today.get('start', '—') }}</td>
      <td>{{ entry.today.get('stop', '—') }}</td>
      <td>{{ "%.3f"|format(entry.today.get('risk_score', 0)) }}</td>
      <td>{{ "%.3f"|format(entry.get('accumulated_risk', 0)) }}</td>
      <td class="{{ 'on-air' if entry.today.get('broadcasting') else 'off-air' }}">
        {{ '● on air' if entry.today.get('broadcasting') else '○ dark' }}
      </td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p style="color:#555">No history yet.</p>
  {% endif %}
</div>

<p style="color:#333;font-size:0.75em;margin-top:20px">
  {{ now }} &mdash; <a href="/" style="color:#444">refresh</a>
</p>

</body>
</html>"""


class ControlPanel:
    """Web-based control panel for the LPFM station.

    Runs Flask in a background daemon thread. Reads and writes the scheduler
    state file directly; wakes the scheduler after any state change.

    Args:
        control_panel_config: Port and history settings from config.
        scheduler_config: Used to locate the state file.
        stream_config: Provides the default stream URL for the UI.
        scheduler: Called to wake the scheduling thread after state changes.
        stream: Called to switch the live stream URL immediately.
        relay: Queried for live transmitter status.
    """

    def __init__(
        self,
        control_panel_config: ControlPanelConfig,
        scheduler_config: SchedulerConfig,
        stream_config: StreamConfig,
        scheduler,
        stream,
        relay,
    ):
        self._config = control_panel_config
        self._scheduler_config = scheduler_config
        self._stream_config = stream_config
        self._scheduler = scheduler
        self._stream = stream
        self._relay = relay
        self._logger = logging.getLogger(__name__)
        self._app = Flask(__name__)
        self._app.logger.setLevel(logging.ERROR)  # suppress Flask request logs
        self._setup_routes()

    def start(self) -> None:
        """Start the Flask server in a background daemon thread."""
        thread = threading.Thread(
            target=self._app.run,
            kwargs={"host": "0.0.0.0", "port": self._config.port, "debug": False, "use_reloader": False},
            daemon=True,
            name="control-panel",
        )
        thread.start()
        self._logger.info(f"Control panel running at http://0.0.0.0:{self._config.port}")

    def stop(self) -> None:
        """No-op — daemon thread exits with the main process."""
        pass

    # ── Routes ────────────────────────────────────────────────────────────────

    def _setup_routes(self) -> None:
        app = self._app

        @app.route("/")
        def index():
            state = self._load_state()
            history = self._load_history(self._config.history_entries)
            return render_template_string(
                TEMPLATE,
                today=state.get("today", {}),
                accumulated_risk=state.get("accumulated_risk", 0.0),
                shutoff=state.get("emergency_shutoff", False),
                transmitting=self._scheduler.is_transmitting,
                default_stream=self._stream_config.url,
                history=history,
                now=datetime.now().strftime("%Y-%m-%d %H:%M"),
            )

        @app.route("/api/schedule", methods=["POST"])
        def update_schedule():
            broadcasting = request.form.get("broadcasting") == "true"
            start = request.form.get("start", "")
            stop = request.form.get("stop", "")
            self._scheduler.set_schedule(broadcasting, start, stop)
            return redirect("/")

        @app.route("/api/stream", methods=["POST"])
        def update_stream():
            url = request.form.get("url", "").strip()
            state = self._load_state()
            today = state.get("today", {})
            if url and url != self._stream_config.url:
                today["stream_url_override"] = url
                self._stream.set_url(url)
                self._logger.info(f"Stream URL override set via control panel: {url}")
            else:
                today.pop("stream_url_override", None)
                self._stream.reset_url()
                self._logger.info("Stream URL reset to default via control panel")
            state["today"] = today
            self._write_state(state)
            return redirect("/")

        @app.route("/api/relay-status")
        def relay_status():
            try:
                on = self._relay.get_state()
            except Exception:
                on = False
            return jsonify({"on": on})

        @app.route("/api/transmitter", methods=["POST"])
        def toggle_transmitter():
            current = self._scheduler.is_transmitting
            self._logger.info(
                f"Transmitter toggle via control panel: {'ON→OFF' if current else 'OFF→ON'}"
            )
            if current:
                self._scheduler.transmitter_off()
            else:
                self._scheduler.transmitter_on()
            return redirect("/")

        @app.route("/api/shutoff", methods=["POST"])
        def toggle_shutoff():
            state = self._load_state()
            new_state = not state.get("emergency_shutoff", False)
            state["emergency_shutoff"] = new_state
            self._write_state(state)
            self._logger.warning(
                f"Emergency shutoff {'ACTIVATED' if new_state else 'CLEARED'} via control panel"
            )
            self._scheduler.wake()
            return redirect("/")

        @app.route("/api/reroll", methods=["POST"])
        def reroll():
            self._logger.info("Tonight's broadcast decision rerolled via control panel")
            self._scheduler.reroll()
            return redirect("/")

    # ── State I/O ─────────────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        path = Path(self._scheduler_config.state_file)
        if not path.exists():
            return {}
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_state(self, state: dict) -> None:
        """Write state to disk without appending to history (Scheduler's job)."""
        path = Path(self._scheduler_config.state_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "w") as f:
                json.dump(state, f, indent=2)
        except OSError as e:
            self._logger.error(f"Failed to write state: {e}")

    def _load_history(self, n: int) -> list:
        path = Path(self._scheduler_config.state_file).parent / "history.jsonl"
        if not path.exists():
            return []
        try:
            with open(path) as f:
                lines = [l.strip() for l in f if l.strip()]
            entries = []
            for line in reversed(lines[-n:]):
                entries.append(json.loads(line))
            return entries
        except (json.JSONDecodeError, OSError):
            return []
