"""PCS 7 Bridge Home Assistant app.

The app owns the approved mapping database in /data.  It is intentionally
fail-closed: S7 writes and PLC-to-HA commands are disabled unless separately
armed in the add-on settings.  This first release supplies the HA-native UI,
state discovery, mapping validation, and a read-only S7 connectivity probe.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import struct
import time
import urllib.request
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from command_runtime import Command, CommandEngine, Journal

DATA = Path("/data")
STATE_FILE = DATA / "pcs7-bridge.json"
COMMAND_MAP_FILE = DATA / "command-map.json"
OPTIONS_FILE = DATA / "options.json"
RUNTIME_OPTIONS_FILE = DATA / "bridge-runtime.json"
SEED_FILE = Path("/app/seed.json")
HA_API = "http://supervisor/core/api"
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,38}$")

DEFAULT_STATE = {
    "version": 1,
    "points": [],
    "commands": [],
    "pending": [],
    "last_probe": None,
}
runtime: dict[str, Any] = {
    "running": False, "last_sync": None, "last_error": None,
    "last_command_poll": None, "last_command_error": None, "last_command_events": [],
}
plc_lock = asyncio.Lock()
command_session: Any | None = None


def read_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else fallback.copy()
    except (OSError, json.JSONDecodeError):
        return fallback.copy()


def options() -> dict[str, Any]:
    defaults = {"plc_host": "192.168.40.200", "rack": 0, "slot": 3,
                "poll_interval_s": 5, "write_enabled": False,
                "commands_enabled": False, "command_poll_ms": 500,
                "command_max_risk_tier": 1}
    defaults.update(read_json(OPTIONS_FILE, {}))
    defaults.update(read_json(RUNTIME_OPTIONS_FILE, {}))
    return defaults


def state() -> dict[str, Any]:
    if STATE_FILE.exists():
        saved = read_json(STATE_FILE, DEFAULT_STATE)
        seed = read_json(SEED_FILE, DEFAULT_STATE)
        if seed.get("points"):
            # A previous preview/probe can create a small state file before
            # this release's reserved engineering map is available. Merge it
            # rather than losing its audit data, while retaining any future
            # user-added points as overrides/new rows.
            by_id = {point["point_id"]: point for point in seed["points"]}
            for point in saved.get("points", []):
                by_id[point["point_id"]] = point
            saved["points"] = list(by_id.values())
        if not saved.get("commands") and COMMAND_MAP_FILE.exists():
            command_map = read_json(COMMAND_MAP_FILE, {})
            saved["commands"] = command_map.get("commands", [])
        return saved
    # The seeded engineering map is read-only until the first local change.
    # It reserves all generated DB59/DB60 addresses so a new point cannot be
    # allocated into an established interface range.
    seeded = read_json(SEED_FILE, DEFAULT_STATE)
    if not seeded.get("commands") and COMMAND_MAP_FILE.exists():
        seeded["commands"] = read_json(COMMAND_MAP_FILE, {}).get("commands", [])
    return seeded


def save(value: dict[str, Any]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(STATE_FILE)


class PointRequest(BaseModel):
    entity_id: str = Field(pattern=r"^[a-z_]+\.[a-z0-9_]+$")
    name: str = Field(min_length=3, max_length=80)
    member_name: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,23}$")
    kind: str = Field(pattern=r"^(real|bool)$")
    stale_after_s: int = Field(ge=60, le=2_592_000)
    unit: str = Field(default="", max_length=20)


def next_address(current: list[dict[str, Any]], kind: str) -> tuple[int, int]:
    db = 60 if kind == "real" else 59
    members = [p for p in current if p.get("db_number") == db]
    if not members:
        return (4 if kind == "real" else 0), (8 if kind == "real" else 1)
    last = max(members, key=lambda p: int(p["byte_offset"]))
    start = int(last["byte_offset"]) + (6 if kind == "real" else 2)
    return start, start + (4 if kind == "real" else 1)


def proposed_point(request: PointRequest, current: list[dict[str, Any]]) -> dict[str, Any]:
    if any(p["entity_id"] == request.entity_id and not p.get("removed") for p in current):
        raise HTTPException(409, "This Home Assistant entity is already mapped.")
    if any(p["member_name"] == request.member_name for p in current):
        raise HTTPException(409, "This PCS 7 member name is already mapped.")
    offset, quality = next_address(current, request.kind)
    return {
        "point_id": f"HA_{len(current) + 1:03d}",
        "entity_id": request.entity_id,
        "name": request.name.strip(),
        "member_name": request.member_name,
        "kind": request.kind,
        "unit": request.unit.strip(),
        "db_number": 60 if request.kind == "real" else 59,
        "byte_offset": offset,
        "quality_byte_offset": quality,
        "stale_after_s": request.stale_after_s,
        "enabled": False,
        "created_at": int(time.time()),
    }


async def ha_states() -> list[dict[str, Any]]:
    if not TOKEN:
        return []
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(timeout=8) as client:
        response = await client.get(f"{HA_API}/states", headers=headers)
        response.raise_for_status()
        result = response.json()
    return result if isinstance(result, list) else []


def decode_state(point: dict[str, Any], raw: str) -> tuple[object, int]:
    """Convert HA state to an approved typed value and PCS 7 quality byte."""
    if raw.strip().lower() in {"", "unknown", "unavailable", "none"}:
        return (0.0 if point["kind"] == "real" else False), 0x00
    if point["kind"] == "real":
        try:
            number = float(raw)
        except ValueError:
            return 0.0, 0x00
        return (number, 0x80) if number == number and abs(number) != float("inf") else (0.0, 0x00)
    normalized = raw.strip().lower()
    if normalized in {"on", "true", "1", "open", "home"}:
        return True, 0x80
    if normalized in {"off", "false", "0", "closed", "not_home"}:
        return False, 0x80
    return False, 0x00


def write_points(points: list[dict[str, Any]], values: dict[str, str]) -> None:
    """Write only explicitly activated and typed mappings. Runs in a worker thread."""
    import snap7
    config = options()
    client = snap7.client.Client()
    try:
        client.connect(config["plc_host"], int(config["rack"]), int(config["slot"]))
        if not client.get_connected():
            raise RuntimeError("S7 connection did not complete")
        for point in points:
            value, quality = decode_state(point, values.get(point["entity_id"], "unavailable"))
            if point["kind"] == "real":
                payload = struct.pack(">fBB", float(value), quality, 0)
            else:
                payload = bytes((1 if value else 0, quality))
            client.db_write(int(point["db_number"]), int(point["byte_offset"]), payload)
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


def execute_command(command: Command, value: object) -> None:
    """Call only the fixed service family approved by a saved command mapping."""
    domain = command.entity_id.split(".", 1)[0]
    data: dict[str, object] = {"entity_id": command.entity_id}
    if command.action == "power":
        service = "turn_on" if value else "turn_off"
    elif command.action == "set_temperature":
        domain, service, data = "climate", "set_temperature", {**data, "temperature": value}
    elif command.action == "set_value":
        service, data = "set_value", {**data, "value": value}
    elif command.action == "percentage":
        domain, service, data = "fan", "set_percentage", {**data, "percentage": value}
    elif command.action == "brightness_pct":
        domain, service, data = "light", "turn_on", {**data, "brightness_pct": value}
    elif command.action == "select_option":
        service, data = "select_option", {**data, "option": value}
    elif command.action == "set_hvac_mode":
        domain, service, data = "climate", "set_hvac_mode", {**data, "hvac_mode": value}
    elif command.action == "set_fan_mode":
        domain, service, data = "climate", "set_fan_mode", {**data, "fan_mode": value}
    elif command.action == "activate":
        service = "press" if domain == "button" else "turn_on"
    else:
        raise ValueError(f"unsupported approved action {command.action!r}")
    body = json.dumps(data).encode("utf-8")
    request = urllib.request.Request(
        f"{HA_API}/services/{domain}/{service}", data=body, method="POST",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"Home Assistant returned HTTP {response.status}")


class CommandSession:
    """One persistent, serialized DB58 session.  Reconfiguration re-baselines."""
    def __init__(self) -> None:
        self.client = None
        self.engine = None
        self.signature = None

    def close(self) -> None:
        if self.client:
            try:
                self.client.disconnect()
            except Exception:
                pass
        self.client = self.engine = None
        self.signature = None

    def poll(self, commands: list[dict[str, Any]], config: dict[str, Any]) -> list[tuple[str, int]]:
        """Poll only named, allow-listed commands; initial values are never dispatched."""
        import snap7
        signature = json.dumps({"commands": commands, "host": config["plc_host"],
                                "rack": config["rack"], "slot": config["slot"],
                                "tier": config["command_max_risk_tier"]}, sort_keys=True)
        if signature != self.signature:
            self.close()
            self.client = snap7.client.Client()
            self.client.connect(config["plc_host"], int(config["rack"]), int(config["slot"]))
            if not self.client.get_connected():
                self.close()
                raise RuntimeError("S7 connection did not complete")
            self.engine = CommandEngine(
                commands, lambda db, start, size: bytes(self.client.db_read(db, start, size)),
                lambda db, start, payload: self.client.db_write(db, start, payload), execute_command,
                Journal(DATA / "command-journal.json"), max_risk_tier=int(config["command_max_risk_tier"]),
            )
            self.signature = signature
        return self.engine.poll()


async def sync_loop() -> None:
    """The operational data loop. It is inert until the add-on is armed."""
    runtime["running"] = True
    while True:
        try:
            config = options()
            current = state()
            active = [p for p in current["points"] if p.get("enabled")]
            if config.get("write_enabled") and active:
                raw = await ha_states()
                values = {item["entity_id"]: str(item.get("state", "")) for item in raw
                          if isinstance(item, dict) and isinstance(item.get("entity_id"), str)}
                async with plc_lock:
                    await asyncio.to_thread(write_points, active, values)
                runtime["last_sync"] = int(time.time())
                runtime["last_error"] = None
        except Exception as exc:
            runtime["last_error"] = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(max(1, int(options().get("poll_interval_s", 5))))


async def command_loop() -> None:
    """Separate fail-closed DB58 loop; it never runs unless explicitly armed."""
    global command_session
    command_session = CommandSession()
    while True:
        config = options()
        commands = state().get("commands", [])
        try:
            if config.get("commands_enabled") and commands:
                async with plc_lock:
                    events = await asyncio.to_thread(command_session.poll, commands, config)
                runtime["last_command_poll"] = int(time.time())
                runtime["last_command_events"] = events[-12:]
                runtime["last_command_error"] = None
            else:
                command_session.close()
        except Exception as exc:
            runtime["last_command_error"] = f"{type(exc).__name__}: {exc}"
            command_session.close()
        await asyncio.sleep(max(0.1, int(config.get("command_poll_ms", 500)) / 1000))


app = FastAPI(title="PCS 7 Bridge")


@app.get("/api/status")
def status() -> dict[str, Any]:
    current = state()
    active_points = [point for point in current["points"] if not point.get("removed")]
    return {
        "plc": {key: options()[key] for key in ("plc_host", "rack", "slot", "write_enabled", "commands_enabled")},
        "points": len(active_points),
        "removed": len(current["points"]) - len(active_points),
        "pending": len(current["pending"]),
        "last_probe": current.get("last_probe"),
        "runtime": runtime.copy(),
    }


@app.get("/api/states")
async def get_states() -> list[dict[str, str]]:
    try:
        raw = await ha_states()
    except httpx.HTTPError as exc:
        raise HTTPException(503, f"Home Assistant API unavailable: {exc}") from exc
    return [{"entity_id": item["entity_id"], "state": str(item.get("state", "")),
             "name": str(item.get("attributes", {}).get("friendly_name", item["entity_id"]))}
            for item in raw if isinstance(item, dict) and "entity_id" in item]


@app.get("/api/points")
def get_points() -> list[dict[str, Any]]:
    return state()["points"]


@app.get("/api/commands")
def get_commands() -> list[dict[str, Any]]:
    """The current reviewed command allow-list; empty means no command path exists."""
    return state().get("commands", [])


@app.post("/api/commands/import")
def import_commands(command_map: dict[str, Any]) -> dict[str, int]:
    """Persist a reviewed DB58 map after validating every typed slot."""
    commands = command_map.get("commands")
    if not isinstance(commands, list) or not commands:
        raise HTTPException(422, "Expected a non-empty reviewed command map.")
    try:
        parsed = [Command.from_dict(item) for item in commands]
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(422, f"Invalid command map: {exc}") from exc
    if len({item.command_id for item in parsed}) != len(parsed):
        raise HTTPException(422, "Command IDs must be unique.")
    DATA.mkdir(parents=True, exist_ok=True)
    temporary = COMMAND_MAP_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps({"commands": commands}, indent=2) + "\n", encoding="utf-8")
    temporary.replace(COMMAND_MAP_FILE)
    return {"commands": len(parsed)}


@app.post("/api/commands/arm")
def arm_commands(request: dict[str, Any]) -> dict[str, Any]:
    """Persist the explicit global command arm independently of HA app options."""
    enabled = request.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(422, "enabled must be a boolean.")
    override = read_json(RUNTIME_OPTIONS_FILE, {})
    override["commands_enabled"] = enabled
    temporary = RUNTIME_OPTIONS_FILE.with_suffix(".tmp")
    DATA.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(override, indent=2) + "\n", encoding="utf-8")
    temporary.replace(RUNTIME_OPTIONS_FILE)
    return {"commands_enabled": enabled}


@app.post("/api/points/preview")
def preview_point(request: PointRequest) -> dict[str, Any]:
    return proposed_point(request, state()["points"])


@app.post("/api/points")
def add_point(request: PointRequest) -> dict[str, Any]:
    current = state()
    point = proposed_point(request, current["points"])
    current["points"].append(point)
    current["pending"].append({
        "type": "new_input", "point_id": point["point_id"], "created_at": point["created_at"],
        "status": "needs_pcs7_engineering",
        "steps": [
            f"Append {point['member_name']} to existing DB{point['db_number']} (never recreate the DB).",
            "Compile/download the updated PCS 7 DB.",
            "Connect the approved structured value and status in CFC.",
            "Return here and explicitly enable the point after live verification.",
        ],
    })
    save(current)
    return point


@app.post("/api/points/{point_id}/activate")
def activate_point(point_id: str) -> dict[str, Any]:
    """Require both an HA option arm and an explicit per-point activation."""
    if not options().get("write_enabled"):
        raise HTTPException(409, "PLC writes are disabled in add-on settings.")
    current = state()
    for point in current["points"]:
        if point["point_id"] == point_id:
            point["enabled"] = True
            save(current)
            return point
    raise HTTPException(404, "Point not found.")


@app.delete("/api/points/{point_id}")
def remove_point(point_id: str) -> dict[str, Any]:
    """Remove a mapping from the bridge without reusing its PCS 7 address."""
    current = state()
    for point in current["points"]:
        if point["point_id"] == point_id:
            point["removed"] = True
            point["enabled"] = False
            current["pending"] = [item for item in current["pending"]
                                  if item.get("point_id") != point_id]
            save(current)
            return {"point_id": point_id, "removed": True,
                    "detail": "Removed from the bridge map; its DB address remains reserved until PCS 7 is separately cleaned up."}
    raise HTTPException(404, "Point not found.")


@app.post("/api/probe")
def probe() -> dict[str, Any]:
    """One read-only S7 probe. Never writes, and never runs automatically."""
    config = options()
    result: dict[str, Any] = {"at": int(time.time()), "ok": False, "detail": ""}
    try:
        import snap7
        client = snap7.client.Client()
        client.connect(config["plc_host"], int(config["rack"]), int(config["slot"]))
        result["ok"] = bool(client.get_connected())
        result["detail"] = "S7 session established (read-only probe); no DB read or write was performed."
        client.disconnect()
    except Exception as exc:  # surfaced in UI without a traceback
        result["detail"] = f"S7 probe failed: {type(exc).__name__}: {exc}"
    current = state()
    current["last_probe"] = result
    save(current)
    return result


UI = r'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PCS 7 Bridge</title><style>:root{color-scheme:dark}*{min-width:0}html,body{max-width:100%;overflow-x:hidden}body{font:15px system-ui;margin:0;background:#101827;color:#e5e7eb}main{box-sizing:border-box;width:100%;max-width:1180px;margin:auto;padding:18px}h1,h2{margin:0 0 8px;overflow-wrap:anywhere}h2{font-size:1.25rem}.sub,.muted{color:#9ca3af;overflow-wrap:anywhere}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(185px,100%),1fr));gap:12px;margin:18px 0}.card,form,.map-list{box-sizing:border-box;max-width:100%;background:#182234;border:1px solid #304056;border-radius:12px;padding:16px}.label{color:#94a3b8;font-size:.75rem;letter-spacing:.06em;text-transform:uppercase}strong{font-size:1.1rem;overflow-wrap:anywhere}.nav,.sort-bar{display:flex;gap:8px;flex-wrap:wrap;margin:20px 0}.nav button,.sort-bar button{width:auto;max-width:100%;margin:0;background:#243247}.nav button.active{background:#2563eb}.section{display:none}.section.active{display:block}input,select,button{box-sizing:border-box;max-width:100%;width:100%;padding:10px;margin:5px 0 12px;border-radius:7px;border:1px solid #40516a;background:#0f172a;color:white}button{background:#2563eb;border:0;font-weight:700;cursor:pointer}button.danger{background:#991b1b;width:auto;margin:0;padding:8px 12px}button.warn{background:#92400e}select{white-space:normal}.notice{box-sizing:border-box;max-width:100%;overflow-wrap:anywhere;white-space:pre-wrap;padding:12px;border-radius:8px;background:#172554;display:none}.pill{display:inline-block;max-width:100%;overflow-wrap:anywhere;padding:3px 7px;border-radius:20px;background:#26364c;color:#cbd5e1;font-size:.78rem}.map-list{display:grid;gap:10px;padding:12px}.map-item{box-sizing:border-box;max-width:100%;padding:13px;border:1px solid #304056;border-radius:9px;background:#111b2b}.map-fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.map-value{overflow-wrap:anywhere;word-break:break-word}.map-action{margin-top:12px}.empty{padding:18px;color:#94a3b8;text-align:center}@media(max-width:720px){main{padding:12px}.card,form,.map-list{padding:12px}.nav{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}.nav button{width:100%;font-size:.88rem}.sort-bar{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;margin:12px 0}.sort-bar button{width:100%;font-size:.82rem}.map-fields{grid-template-columns:1fr;gap:9px}}</style></head><body><main>
<h1>PCS 7 Bridge</h1><p class="sub">One controlled map between Home Assistant and PCS 7. PLC writes remain disabled until a reviewed cutover.</p>
<div class="grid" id="status"></div><p id="notice" class="notice"></p>
<nav class="nav"><button class="active" data-tab="add">Add input</button><button data-tab="map">Point map</button><button data-tab="commands">Commands</button><button data-tab="connection">Connection</button></nav>
<section class="section active" id="add"><h2>Add Home Assistant input</h2><form id="addForm"><label>Find an unmapped Home Assistant entity</label><input id="entitySearch" autocomplete="off" placeholder="Search by name or entity ID"><label>Available entities</label><select id="entity" size="9" required><option value="">Loading Home Assistant entities…</option></select><p class="muted" id="entityCount"></p><label>Display name</label><input id="name" required><label>PCS 7 member name</label><input id="member" required maxlength="24" placeholder="HA_GARAGE_TEMP"><label>Type</label><select id="kind"><option value="real">REAL · DB60</option><option value="bool">BOOL · DB59</option></select><label>Unit (optional)</label><input id="unit" placeholder="°F"><label>Stale after seconds</label><input id="stale" type="number" min="60" value="900"><button type="button" id="preview">Preview mapping</button><button type="submit">Add as pending deployment</button></form></section>
<section class="section" id="map"><h2>Point map cleanup</h2><p class="muted">Each point is a full-width card, so every field and action remains visible. Removing a point only removes it from this bridge map; its PCS 7 address stays reserved until you separately clean up the DB/CFC project.</p><input id="mapSearch" placeholder="Search mapped points"><div class="sort-bar"><button data-sort="name">Name ↕</button><button data-sort="entity_id">Home Assistant ↕</button><button data-sort="pcs7">PCS 7 ↕</button><button data-sort="status">Status ↕</button></div><div class="map-list" id="points"></div></section>
<section class="section" id="commands"><h2>PCS 7 → Home Assistant commands</h2><p class="muted">DB58 accepts only a reviewed, named command allow-list. The runtime baselines any live PLC values at startup, so a restart cannot replay a command. Global command arming remains off unless separately enabled in app settings.</p><div class="map-list" id="commandsList"></div></section>
<section class="section" id="connection"><h2>Connection test</h2><div class="card"><button class="warn" id="probe">Run one read-only Snap7 connection probe</button><p class="muted">This opens then closes one S7 session. It does not read or write a PLC DB.</p></div></section>
</main><script>
const j=(u,o={})=>fetch(u,o).then(async r=>{let x=await r.json();if(!r.ok)throw Error(x.detail||r.statusText);return x});const n=document.querySelector('#notice');let allEntities=[],points=[],commands=[],sortKey='name',sortDirection=1;const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));function say(x){n.textContent=x;n.style.display='block'}
function memberFor(text){return ('HA_'+text.toUpperCase().replace(/[^A-Z0-9]+/g,'_').replace(/^_+|_+$/g,'')).slice(0,24).replace(/_+$/,'')}
function renderEntities(){let q=entitySearch.value.trim().toLowerCase(),mapped=new Set(points.filter(x=>!x.removed).map(x=>x.entity_id)),shown=allEntities.filter(x=>!mapped.has(x.entity_id)&&(!q||x.entity_id.toLowerCase().includes(q)||x.name.toLowerCase().includes(q)));entity.innerHTML=shown.map(x=>`<option value="${esc(x.entity_id)}">${esc(x.name)} — ${esc(x.entity_id)} (${esc(x.state)})</option>`).join('')||'<option value="">No unmapped entities match</option>';entityCount.textContent=`${shown.length} available · ${mapped.size} already mapped`}
function pointSortValue(x){if(sortKey==='pcs7')return `${String(x.db_number).padStart(4,'0')}:${String(x.byte_offset).padStart(6,'0')}`;if(sortKey==='status')return x.enabled?'active':x.existing?'existing map':'pending';return String(x[sortKey]||'').toLowerCase()}function renderPoints(){let q=mapSearch.value.trim().toLowerCase(),shown=points.filter(x=>!x.removed&&(!q||[x.name,x.entity_id,x.member_name,x.point_id].join(' ').toLowerCase().includes(q))).sort((a,b)=>pointSortValue(a).localeCompare(pointSortValue(b),undefined,{numeric:true})*sortDirection);document.querySelector('#points').innerHTML=shown.map(x=>`<article class="map-item"><div class="map-fields"><div><div class=label>Name</div><div class=map-value>${esc(x.name)}<br><span class=pill>${esc(x.point_id)}</span></div></div><div><div class=label>Home Assistant</div><div class=map-value>${esc(x.entity_id)}</div></div><div><div class=label>PCS 7</div><div class=map-value>DB${x.db_number} · ${esc(x.member_name)}<br><span class=muted>byte ${x.byte_offset}</span></div></div><div><div class=label>Status</div><div class=map-value>${x.enabled?'Active':x.existing?'Existing map':'Pending'}</div></div></div><div class=map-action><button class=danger data-remove="${esc(x.point_id)}">Remove mapping</button></div></article>`).join('')||'<div class=empty>No mapped points match.</div>';document.querySelectorAll('[data-remove]').forEach(b=>b.onclick=()=>removePoint(b.dataset.remove));document.querySelectorAll('[data-sort]').forEach(b=>b.textContent=b.dataset.sort==='pcs7'?`PCS 7 ${sortKey==='pcs7'?(sortDirection===1?'↑':'↓'):'↕'}`:`${b.dataset.sort==='entity_id'?'Home Assistant':b.dataset.sort[0].toUpperCase()+b.dataset.sort.slice(1)} ${sortKey===b.dataset.sort?(sortDirection===1?'↑':'↓'):'↕'}`)}
function renderCommands(){document.querySelector('#commandsList').innerHTML=commands.map(x=>`<article class=map-item><div class=map-fields><div><div class=label>Command</div><div class=map-value>${esc(x.command_id)}</div></div><div><div class=label>Home Assistant</div><div class=map-value>${esc(x.entity_id)}</div></div><div><div class=label>Action</div><div class=map-value>${esc(x.action)} · ${esc(x.kind)}</div></div><div><div class=label>Safety</div><div class=map-value>${x.enabled?'Mapped':'Disabled'} · Tier ${esc(x.risk_tier)}</div></div></div></article>`).join('')||'<div class=empty>No reviewed DB58 commands are loaded. The command runtime cannot issue any Home Assistant actions.</div>'}
async function load(){let[s,p,e,c]=await Promise.all([j('api/status'),j('api/points'),j('api/states').catch(()=>[]),j('api/commands')]);points=p;allEntities=e;commands=c;document.querySelector('#status').innerHTML=`<div class=card><div class=label>PLC endpoint</div><strong>${s.plc.plc_host} · R${s.plc.rack}/S${s.plc.slot}</strong></div><div class=card><div class=label>Mapped points</div><strong>${s.points}</strong></div><div class=card><div class=label>Pending engineering</div><strong>${s.pending}</strong></div><div class=card><div class=label>PLC writes</div><strong>${s.plc.write_enabled?'ARMED':'DISABLED'}</strong></div>`;renderEntities();renderPoints();renderCommands()}
function payload(){return {entity_id:entity.value,name:name.value,member_name:member.value,kind:kind.value,unit:unit.value,stale_after_s:Number(stale.value)}};document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-tab]').forEach(x=>x.classList.toggle('active',x===b));document.querySelectorAll('.section').forEach(x=>x.classList.toggle('active',x.id===b.dataset.tab))});document.querySelectorAll('[data-sort]').forEach(b=>b.onclick=()=>{if(sortKey===b.dataset.sort)sortDirection*=-1;else{sortKey=b.dataset.sort;sortDirection=1}renderPoints()});entitySearch.oninput=renderEntities;mapSearch.oninput=renderPoints;entity.onchange=()=>{let x=allEntities.find(x=>x.entity_id===entity.value);if(x){if(!name.value)name.value=x.name;if(!member.value)member.value=memberFor(x.name)}};async function removePoint(id){if(!confirm(`Remove ${id} from this bridge map? Its PCS 7 address remains reserved.`))return;try{let x=await j(`api/points/${id}`,{method:'DELETE'});say(x.detail);load()}catch(e){say(e.message)}}preview.onclick=async()=>{try{let x=await j('api/points/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload())});say(`Preview\n${x.point_id}: ${x.entity_id}\nDB${x.db_number}.${x.member_name} at byte ${x.byte_offset}; quality byte ${x.quality_byte_offset}\nNo change has been saved.`)}catch(e){say(e.message)}};addForm.onsubmit=async e=>{e.preventDefault();try{let x=await j('api/points',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload())});say(`Added ${x.point_id} as a pending deployment. It is not enabled and cannot write to the PLC.`);load()}catch(e){say(e.message)}};probe.onclick=async()=>{try{let x=await j('api/probe',{method:'POST'});say(x.detail);load()}catch(e){say(e.message)}};load();</script></body></html>'''


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return UI


if __name__ == "__main__":
    import uvicorn
    @app.on_event("startup")
    async def start_sync_loop() -> None:
        asyncio.create_task(sync_loop())
        asyncio.create_task(command_loop())
    uvicorn.run(app, host="0.0.0.0", port=8099)
