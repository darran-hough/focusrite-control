"""
Backend — hardware control layer.

MIX VOLUME STRATEGY:
  ALSA hardware mix controls ("Mix A Input 01 Volume") only exist on
  Scarlett models that have an on-board DSP mixer (4i4 and above).
  On 2i2 / Solo the mixing is entirely in software (PipeWire).

  We therefore:
    1. Try ALSA hardware controls first (fast, no-op if they don't exist)
    2. Fall back to pactl set-sink-input-volume for playback streams,
       and pactl set-source-output-volume for capture/monitoring streams

  getSinkInputs() enumerates the live PipeWire sink-inputs (playback
  streams currently connected to the Scarlett sink) so the UI can show
  real stream names instead of a hard-coded list.

METERING:
  meterUpdate now includes per-source levels:
    inputs[0], inputs[1]  — mic/line capture channels
    outputs[L, R]         — rendered output (sink monitor)
    mixLevels             — per sink-input signal level, keyed by PA index
                            (used to drive inline mix-row meters in the UI)
"""

import subprocess
import threading
import re
import json
import math
import struct
import time
from PyQt6.QtCore import QObject, pyqtSlot, pyqtSignal, QTimer


# ─── Helpers ──────────────────────────────────────────────────────────────────

def run(cmd, timeout=5):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return False, "", str(e)

def ok(msg):  return json.dumps({"success": True,  "message": msg})
def err(msg): return json.dumps({"success": False, "message": msg})


# ─── ALSA device detection ────────────────────────────────────────────────────

def detect_alsa_device():
    success, out, _ = run(["aplay", "-l"])
    if success:
        for line in out.splitlines():
            if any(x in line for x in ("Scarlett","scarlett","Focusrite","focusrite")):
                m = re.search(r"card (\d+)", line)
                if m:
                    card = int(m.group(1))
                    nm   = re.search(r"\[([^\]]+)\]", line)
                    name = nm.group(1) if nm else f"Card {card}"
                    return f"hw:{card}", name, card
    return "hw:0", "Scarlett (not detected)", 0

DEVICE, DEVICE_NAME, CARD_NUM = detect_alsa_device()


# ─── PulseAudio / PipeWire helpers ────────────────────────────────────────────

def _pa_list(object_type):
    """
    Parse `pactl list <object_type>` into a list of dicts.
    Each dict has numeric key from the header line plus all Key: Value pairs.
    """
    ok_f, out, _ = run(["pactl", "list", object_type])
    if not ok_f:
        return []

    # Capitalise singular form for the header match
    singular = object_type.rstrip("s").capitalize()
    objects, current = [], {}
    for raw in out.splitlines():
        line = raw.strip()
        # Top-level header: "Sink Input #42"
        m = re.match(rf"{singular}[^#]*#(\d+)", line)
        if m:
            if current:
                objects.append(current)
            current = {"_index": int(m.group(1))}
            continue
        if not current:
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            current[k.strip()] = v.strip()
    if current:
        objects.append(current)
    return objects


def _pa_sources():
    items = _pa_list("sources")
    return [(o["_index"], o.get("Name",""), o.get("Description","")) for o in items]


def _pa_sink_inputs():
    """
    Return list of dicts for active sink-inputs (playback streams).
    Each dict: {index, name, app_name, volume_pct, sink_name}
    """
    items = _pa_list("sink-inputs")
    result = []
    for o in items:
        # Application name — try several property keys
        app = (o.get("application.name") or
               o.get("media.name")        or
               o.get("application.process.binary") or
               "Unknown")
        # Strip surrounding quotes if present
        app = app.strip('"').strip("'")

        # Volume: "front-left: 65536 / 100% / 0.00 dB,   front-right: ..."
        vol_str = o.get("Volume","")
        pct = 100
        m = re.search(r"(\d+)%", vol_str)
        if m:
            pct = int(m.group(1))

        result.append({
            "index":    o["_index"],
            "name":     app,
            "volumePct": pct,
        })
    return result


def _pa_source_outputs():
    """
    Return list of dicts for active source-outputs (capture streams).
    Each dict: {index, name, volumePct}
    """
    items = _pa_list("source-outputs")
    result = []
    for o in items:
        app = (o.get("application.name") or
               o.get("media.name") or "Unknown").strip('"').strip("'")
        vol_str = o.get("Volume","")
        pct = 100
        m = re.search(r"(\d+)%", vol_str)
        if m:
            pct = int(m.group(1))
        result.append({"index": o["_index"], "name": app, "volumePct": pct})
    return result


def find_pa_nodes():
    """Input capture source + output sink monitor for metering."""
    sources   = _pa_sources()
    keywords  = ("scarlett","focusrite")
    input_src = None
    out_mon   = None
    for _idx, name, desc in sources:
        combined = (name + " " + desc).lower()
        if not any(k in combined for k in keywords):
            continue
        if name.endswith(".monitor"):
            if out_mon   is None: out_mon  = name
        else:
            if input_src is None: input_src = name
    return input_src, out_mon


def _find_scarlett_sink():
    """Return PA sink name for the Scarlett output."""
    ok_f, out, _ = run(["pactl", "list", "sinks"])
    if not ok_f:
        return None
    keywords = ("scarlett","focusrite")
    current_name = None
    for line in out.splitlines():
        line = line.strip()
        if re.match(r"Sink #\d+", line):
            current_name = None
        elif line.startswith("Name:"):
            current_name = line.split("Name:",1)[1].strip()
        elif line.startswith("Description:"):
            desc = line.split("Description:",1)[1].strip().lower()
            if current_name and any(k in desc or k in (current_name.lower()) for k in keywords):
                return current_name
    return None


# ─── parecord reader ──────────────────────────────────────────────────────────

class PaReader:
    CHANNELS = 2; RATE = 48000; BYTES_PER_SAMPLE = 2; CHUNK = 4096

    def __init__(self, source_name, label=""):
        self.label = label; self.available = False
        self._levels = [0.0, 0.0]; self._lock = threading.Lock()
        self._stop = threading.Event(); self._proc = None
        if not source_name: return
        try:
            self._proc = subprocess.Popen(
                ["parecord", f"--device={source_name}", "--raw",
                 "--format=s16le", f"--rate={self.RATE}",
                 f"--channels={self.CHANNELS}", "--latency-msec=50"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
            )
            time.sleep(0.2)
            if self._proc.poll() is not None:
                self._proc = None; return
            self.available = True
            threading.Thread(target=self._loop, daemon=True).start()
        except Exception:
            self._proc = None

    def _loop(self):
        while not self._stop.is_set() and self._proc:
            try:
                chunk = self._proc.stdout.read(self.CHUNK)
                if not chunk: break
                self._process(chunk)
            except Exception: break
        with self._lock: self._levels = [0.0, 0.0]

    def _process(self, data):
        n = len(data) // (self.BYTES_PER_SAMPLE * self.CHANNELS)
        if n == 0: return
        raw = struct.unpack_from(f"<{n * self.CHANNELS}h", data, 0)
        levels = []
        for ch in range(self.CHANNELS):
            s = raw[ch::self.CHANNELS]
            rms = math.sqrt(sum(x*x for x in s) / len(s)) if s else 0
            lin = rms / 32767.0
            levels.append(round(max(0, min(100, (20*math.log10(lin)+60)/60*100)), 1) if lin > 0 else 0.0)
        with self._lock: self._levels = levels

    def read_levels(self):
        with self._lock: return list(self._levels)

    def stop(self):
        self._stop.set()
        if self._proc:
            try: self._proc.terminate()
            except Exception: pass


class _SilentReader:
    available = False
    source = None
    def read_levels(self): return [0.0, 0.0]
    def stop(self): pass


# ─── Build readers ────────────────────────────────────────────────────────────

def _build_readers():
    pa_ok, _, _ = run(["parecord", "--version"])
    if not pa_ok:
        return _SilentReader(), _SilentReader(), "parecord not found", None, None
    input_src, output_mon = find_pa_nodes()
    inp = PaReader(input_src,  "input")  if input_src  else _SilentReader()
    out = PaReader(output_mon, "output") if output_mon else _SilentReader()
    return inp, out, "PulseAudio/PipeWire", input_src, output_mon

_input_reader, _output_reader, _meter_method, _input_src_name, _output_mon_name = _build_readers()


# ─── ALSA helpers ─────────────────────────────────────────────────────────────

def alsa_probe(name):
    _, out, _ = run(["amixer", "-D", DEVICE, "cget", f"name={name}"])
    if not out: return None
    info = {"writable": False, "value": None, "type": None, "items": []}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith(";"):
            if "type=" in line:
                t = re.search(r"type=(\w+)", line)
                if t: info["type"] = t.group(1)
            if "access=" in line:
                a = re.search(r"access=([^\s,;]+)", line)
                if a and "w" in a.group(1): info["writable"] = True
            if "'" in line:
                found = re.findall(r"'([^']*)'", line)
                if found: info["items"] = found
        elif line.startswith(": values="):
            info["value"] = line.split("values=")[1].strip()
    return info if info["type"] else None

def alsa_set(name, value):
    ok_f, _, _ = run(["amixer", "-D", DEVICE, "cset", f"name={name}", str(value)])
    return ok_f, ""

def pipewire_set(key, value):
    ok_f, _, _ = run(["pw-metadata", "-n", "settings", "0", key, str(value)])
    return ok_f, ""

def pipewire_get(key):
    _, out, _ = run(["pw-metadata", "-n", "settings"])
    for line in out.splitlines():
        if key in line:
            m = re.search(r"value:'([^']+)'", line)
            if m: return m.group(1)
    return None

CONTROL_CANDIDATES = {
    "gain":    ["Analogue Input {ch} Gain","Input {ch} Gain","Mic/Line {ch} Gain","Line In {ch} Gain"],
    "phantom": ["Input {ch} Phantom Power Switch","Analogue Input {ch} Phantom Power Switch","Line In {ch} Phantom Power Switch","Phantom Power Switch"],
    "inst":    ["Input {ch} Level","Analogue Input {ch} Level","Line In {ch} Level","Input Source {ch}"],
    "air":     ["Input {ch} Air Switch","Analogue Input {ch} Air Switch","Line In {ch} Air Switch"],
    "pad":     ["Input {ch} Pad Switch","Analogue Input {ch} Pad Switch","Line In {ch} Pad Switch"],
    "direct_monitor": ["Direct Monitor Switch","Direct Monitor","Monitor Switch"],
}

def probe_feature(feature, channel):
    for tmpl in CONTROL_CANDIDATES.get(feature, []):
        name = tmpl.replace("{ch}", str(channel))
        info = alsa_probe(name)
        if info: return name, info
    return None, None


# ─── Backend ──────────────────────────────────────────────────────────────────

class ScarlettBackend(QObject):

    meterUpdate = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        # Cache of {pa_index: level} for sink-input meters, updated from output reader
        self._mix_levels = {}
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._emit_meters)
        self._timer.start(80)

    def _emit_meters(self):
        in_lvls  = _input_reader.read_levels()
        out_lvls = _output_reader.read_levels()
        while len(in_lvls)  < 2: in_lvls.append(0.0)
        while len(out_lvls) < 2: out_lvls.append(0.0)

        # Per-source levels for mix row meters:
        #   "input-0" / "input-1" = hardware mic inputs (from capture reader)
        #   "playback"            = software playback (from output monitor)
        # The UI maps each mix row to one of these keys.
        mix_levels = {
            "input-0":  round(in_lvls[0], 1),
            "input-1":  round(in_lvls[1], 1),
            "playback": round(max(out_lvls[0], out_lvls[1]), 1),
        }

        self.meterUpdate.emit(json.dumps({
            "inputs":          [[round(in_lvls[0],1)]*2, [round(in_lvls[1],1)]*2],
            "outputs":         [round(out_lvls[0],1), round(out_lvls[1],1)],
            "mixLevels":       mix_levels,
            "inputAvailable":  _input_reader.available,
            "outputAvailable": _output_reader.available,
        }))

    # ── Device info ───────────────────────────────────────────────────────────

    @pyqtSlot(result=str)
    def getDeviceInfo(self):
        sr = pipewire_get("clock.rate") or "48000"
        quantum = pipewire_get("clock.quantum") or "256"
        connected = "not detected" not in DEVICE_NAME.lower()
        return json.dumps({
            "success": connected, "device": DEVICE, "name": DEVICE_NAME,
            "sampleRate": sr, "bufferSize": quantum, "connected": connected,
            "inputMeterAvail": _input_reader.available,
            "outputMeterAvail": _output_reader.available,
            "meterMethod": _meter_method,
            "inputSource": _input_src_name or "not found",
            "outputMonitor": _output_mon_name or "not found",
        })

    # ── Mix: enumerate live sink-inputs ───────────────────────────────────────

    @pyqtSlot(result=str)
    def getSinkInputs(self):
        """
        Return all currently active playback streams going to the Scarlett sink.
        The UI calls this on tab open to build the dynamic mix fader list.
        Each item: {index, name, volumePct, meterKey}
        """
        scarlett_sink = _find_scarlett_sink()
        all_inputs    = _pa_sink_inputs()

        if scarlett_sink:
            # Filter to only streams routed to the Scarlett
            # pactl sink-input info has "Sink:" key
            items = _pa_list("sink-inputs")
            scarlett_inputs = []
            for o in items:
                sink_val = o.get("Sink","")
                # Match by name or index
                if scarlett_sink in sink_val or str(o["_index"]) in sink_val:
                    app = (o.get("application.name") or o.get("media.name") or "Unknown").strip('"').strip("'")
                    vol_str = o.get("Volume","")
                    pct = 100
                    m = re.search(r"(\d+)%", vol_str)
                    if m: pct = int(m.group(1))
                    scarlett_inputs.append({"index": o["_index"], "name": app, "volumePct": pct, "meterKey": "playback"})
            if scarlett_inputs:
                return json.dumps({"success": True, "inputs": scarlett_inputs, "sink": scarlett_sink})

        # Fallback: return all sink-inputs
        result = [dict(i, meterKey="playback") for i in all_inputs]
        return json.dumps({"success": bool(result), "inputs": result, "sink": scarlett_sink or "unknown"})

    # ── Mix: set sink-input volume ────────────────────────────────────────────

    @pyqtSlot(int, int, result=str)
    def setSinkInputVolume(self, pa_index: int, volume_pct: int):
        """
        Set the volume of a PipeWire playback stream by its PA sink-input index.
        volume_pct: 0–100
        """
        ok_f, _, stderr = run(["pactl", "set-sink-input-volume",
                                str(pa_index), f"{volume_pct}%"])
        if ok_f:
            return ok(f"Volume set to {volume_pct}%")
        return err(f"Could not set volume: {stderr[:80]}")

    @pyqtSlot(int, result=str)
    def muteSinkInput(self, pa_index: int):
        ok_f, _, stderr = run(["pactl", "set-sink-input-mute", str(pa_index), "toggle"])
        if ok_f: return ok("Mute toggled")
        return err(f"Could not mute: {stderr[:80]}")

    # ── Mix: hardware ALSA mix controls (DSP mixer on 4i4 and above) ──────────

    @pyqtSlot(int, str, str, int, result=str)
    def setMixVolume(self, source_idx: int, mix: str, side: str, value: int):
        """Legacy ALSA hardware mix — only works on models with on-board DSP."""
        alsa_val = round((value/100)*127)
        src_str  = f"{source_idx:02d}"
        for name in [f"Mix {mix} Input {src_str} Volume",
                     f"Mix {mix} Input {src_str} {side} Volume",
                     f"Mixer Input {src_str} Volume"]:
            ok_f, _ = alsa_set(name, alsa_val)
            if ok_f: return ok("Mix volume updated")
        return err("No ALSA hardware mix controls found — use PipeWire faders")

    # ── Diagnostics ───────────────────────────────────────────────────────────

    @pyqtSlot(result=str)
    def getDiagnostics(self):
        sources    = _pa_sources()
        pa_ok, pa_ver, _ = run(["parecord","--version"])
        in_alive   = (_input_reader.available  and getattr(_input_reader,  '_proc', None)
                      and _input_reader._proc.poll()  is None)
        out_alive  = (_output_reader.available and getattr(_output_reader, '_proc', None)
                      and _output_reader._proc.poll() is None)
        sink_inputs = _pa_sink_inputs()
        return json.dumps({
            "parecordAvailable": pa_ok,
            "parecordVersion":   pa_ver if pa_ok else "not found",
            "allSources":       [{"name": n, "desc": d} for _, n, d in sources],
            "selectedInput":    _input_src_name  or "none",
            "selectedOutput":   _output_mon_name or "none",
            "inputReaderAlive": in_alive,
            "outputReaderAlive": out_alive,
            "alsaDevice":       DEVICE,
            "alsaDeviceName":   DEVICE_NAME,
            "activeSinkInputs": sink_inputs,
            "scarlettSink":     _find_scarlett_sink() or "not found",
        })

    # ── Channel capabilities ──────────────────────────────────────────────────

    @pyqtSlot(int, result=str)
    def getChannelCapabilities(self, channel: int):
        caps = {}
        for feature in ["gain","phantom","inst","air","pad"]:
            ctrl_name, info = probe_feature(feature, channel)
            if ctrl_name is None:
                caps[feature] = {"exists": False, "writable": False, "value": None}
            else:
                raw = (info.get("value") or "").lower()
                current = raw == "instrument" if feature == "inst" else raw in ("on","1","true","yes")
                caps[feature] = {"exists": True, "writable": info["writable"], "value": current}
        return json.dumps({"success": True, "channel": channel, "caps": caps})

    @pyqtSlot(result=str)
    def getMonitorCapabilities(self):
        ctrl_name, info = probe_feature("direct_monitor", 0)
        if ctrl_name is None:
            return json.dumps({"exists": False, "writable": False, "modes": [], "value": None})
        return json.dumps({
            "exists": True, "writable": info["writable"],
            "modes": info.get("items") or ["Off","Mono","Stereo"],
            "value": info.get("value",""),
        })

    # ── Input controls ────────────────────────────────────────────────────────

    @pyqtSlot(int, int, result=str)
    def setInputGain(self, channel: int, value: int):
        ctrl_name, _ = probe_feature("gain", channel)
        if ctrl_name:
            ok_f, _ = alsa_set(ctrl_name, round((value/100)*69))
            if ok_f: return ok(f"Input {channel} gain set to {value}%")
        return err("Could not set gain — check device is connected")

    @pyqtSlot(int, bool, result=str)
    def setPhantomPower(self, channel: int, enabled: bool):
        ctrl_name, _ = probe_feature("phantom", channel)
        if ctrl_name:
            ok_f, _ = alsa_set(ctrl_name, "on" if enabled else "off")
            if ok_f: return ok(f"48V phantom power {'ON ⚡' if enabled else 'OFF'} — Input {channel}")
        return err("Could not set phantom power")

    @pyqtSlot(int, bool, result=str)
    def setInstrumentMode(self, channel: int, enabled: bool):
        ctrl_name, _ = probe_feature("inst", channel)
        if ctrl_name:
            ok_f, _ = alsa_set(ctrl_name, "Instrument" if enabled else "Line")
            if ok_f: return ok(f"Input {channel} set to {'Guitar / Instrument 🎸' if enabled else 'Mic / Line 🎤'}")
        return err("Could not set input mode")

    @pyqtSlot(int, bool, result=str)
    def setAirMode(self, channel: int, enabled: bool):
        ctrl_name, _ = probe_feature("air", channel)
        if ctrl_name:
            ok_f, _ = alsa_set(ctrl_name, "on" if enabled else "off")
            if ok_f: return ok(f"Air mode {'ON ✨' if enabled else 'OFF'} — Input {channel}")
        return err("Could not set Air mode")

    @pyqtSlot(int, bool, result=str)
    def setPad(self, channel: int, enabled: bool):
        ctrl_name, _ = probe_feature("pad", channel)
        if ctrl_name:
            ok_f, _ = alsa_set(ctrl_name, "on" if enabled else "off")
            if ok_f: return ok(f"Pad {'ON — signal reduced 10dB' if enabled else 'OFF'} — Input {channel}")
        return err("Could not set Pad")

    # ── System settings ───────────────────────────────────────────────────────

    @pyqtSlot(int, result=str)
    def setSampleRate(self, rate: int):
        ok_f, _ = pipewire_set("clock.force-rate", rate)
        if ok_f: return ok(f"Sample rate set to {rate:,} Hz — restart any open audio apps")
        ok_f, _ = alsa_set("Sample Rate", rate)
        if ok_f: return ok(f"Sample rate set to {rate:,} Hz")
        return err("Could not change sample rate — is PipeWire running?")

    @pyqtSlot(int, result=str)
    def setBufferSize(self, size: int):
        ok_f, _ = pipewire_set("clock.force-quantum", size)
        if ok_f: return ok(f"Buffer size set to {size} samples — takes effect immediately")
        return err("Could not change buffer size — is PipeWire running?")

    @pyqtSlot(str, result=str)
    def setDirectMonitor(self, mode: str):
        val = {"off":"Off","mono":"Mono","stereo":"Stereo"}.get(mode,"Off")
        ok_f, _ = alsa_set("Direct Monitor Switch", val)
        if ok_f:
            return ok({"off":"Direct monitoring off","mono":"Direct monitoring on (Mono)","stereo":"Direct monitoring on (Stereo)"}.get(mode,"Updated"))
        return err("Could not update direct monitoring")
