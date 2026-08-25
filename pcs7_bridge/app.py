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
        if seed.get("points") and not saved.get("fresh_start"):
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
        # Backfill older bridge state: an active point is no longer pending
        # engineering. This keeps the dashboard state honest after upgrades.
        active_ids = {point.get("point_id") for point in saved.get("points", [])
                      if point.get("enabled")}
        saved["pending"] = [item for item in saved.get("pending", [])
                            if item.get("point_id") not in active_ids]
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


class CommandRequest(BaseModel):
    """One explicit DB58 CFC-signal command mapping."""
    entity_id: str = Field(pattern=r"^[a-z_]+\.[a-z0-9_]+$")
    name: str = Field(min_length=3, max_length=80)
    member_name: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,38}$")
    kind: str = Field(pattern=r"^(bool|pulse|real)$")
    action: str = Field(pattern=r"^(power|activate|set_value|set_temperature|percentage|brightness_pct|set_hvac_mode|set_fan_mode)$")
    byte_offset: int | None = Field(default=None, ge=16, le=65530)
    min_value: float | None = None
    max_value: float | None = None
    fixed_value: str | None = None
    risk_tier: int = Field(default=1, ge=1, le=3)


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
        "commands": len(current.get("commands", [])),
        "last_probe": current.get("last_probe"),
        "runtime": runtime.copy(),
    }


@app.get("/api/states")
async def get_states() -> list[dict[str, Any]]:
    try:
        raw = await ha_states()
    except httpx.HTTPError as exc:
        raise HTTPException(503, f"Home Assistant API unavailable: {exc}") from exc
    return [{"entity_id": item["entity_id"], "state": str(item.get("state", "")),
             "name": str(item.get("attributes", {}).get("friendly_name", item["entity_id"])),
             "command_options": {key: item.get("attributes", {}).get(key, [])
                                 for key in ("hvac_modes", "fan_modes")}}
            for item in raw if isinstance(item, dict) and "entity_id" in item]


@app.get("/api/points")
def get_points() -> list[dict[str, Any]]:
    return state()["points"]


@app.get("/api/commands")
def get_commands() -> list[dict[str, Any]]:
    """The current reviewed command allow-list; empty means no command path exists."""
    return state().get("commands", [])


def next_command_offset(commands: list[dict[str, Any]], kind: str) -> int:
    """Append only: DB58 is a typed CFC signal layout, never a free-for-all."""
    if not commands:
        return 16
    end = max(int(item["byte_offset"]) + (6 if item.get("kind") == "real" else 2)
              for item in commands)
    return end if end % 2 == 0 else end + 1


def proposed_command(request: CommandRequest, commands: list[dict[str, Any]]) -> dict[str, Any]:
    byte_offset = next_command_offset(commands, request.kind)
    if request.byte_offset is not None and request.byte_offset != byte_offset:
        raise HTTPException(409, f"DB58 byte {request.byte_offset} is not the next reserved slot; use byte {byte_offset}.")
    if request.kind in {"bool", "pulse"} and request.action not in {"power", "activate", "set_hvac_mode", "set_fan_mode"}:
        raise HTTPException(422, "BOOL/pulse commands require a supported switching action.")
    if request.kind == "real" and request.action not in {"set_value", "set_temperature", "percentage", "brightness_pct"}:
        raise HTTPException(422, "REAL commands require a numeric set action.")
    if request.min_value is not None and request.max_value is not None and request.min_value > request.max_value:
        raise HTTPException(422, "Minimum cannot exceed maximum.")
    if any(item.get("member_name") == request.member_name for item in commands):
        raise HTTPException(409, "That PCS 7 member name is already mapped.")
    command = {"command_id": f"HC_{len(commands) + 1:03d}", "entity_id": request.entity_id,
               "name": request.name.strip(), "member_name": request.member_name,
               "kind": request.kind, "action": request.action,
               "byte_offset": byte_offset, "min_value": request.min_value,
               "max_value": request.max_value, "fixed_value": request.fixed_value or "",
               "enabled": False, "risk_tier": request.risk_tier,
               "created_at": int(time.time())}
    try:
        Command.from_dict(command)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return command


def persist_commands(commands: list[dict[str, Any]]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    temporary = COMMAND_MAP_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps({"commands": commands}, indent=2) + "\n", encoding="utf-8")
    temporary.replace(COMMAND_MAP_FILE)


@app.post("/api/commands/preview")
def preview_command(request: CommandRequest) -> dict[str, Any]:
    return proposed_command(request, state().get("commands", []))


@app.post("/api/commands")
def add_command(request: CommandRequest) -> dict[str, Any]:
    current = state()
    commands = current.setdefault("commands", [])
    command = proposed_command(request, commands)
    commands.append(command)
    current.setdefault("pending", []).append({"type": "new_command", "command_id": command["command_id"],
        "created_at": command["created_at"], "status": "needs_pcs7_engineering",
        "steps": [f"Create {command['member_name']} at DB58 byte {command['byte_offset']}.",
                  "Compile/download DB58 and connect the CFC signal.",
                  "Return here and explicitly activate this command after verification."]})
    persist_commands(commands); save(current)
    return command


@app.post("/api/commands/{command_id}/activate")
def activate_command(command_id: str) -> dict[str, Any]:
    current = state()
    for command in current.get("commands", []):
        if command.get("command_id") == command_id:
            command["enabled"] = True
            persist_commands(current["commands"]); save(current)
            return command
    raise HTTPException(404, "Command not found.")


@app.delete("/api/commands/{command_id}")
def remove_command(command_id: str) -> dict[str, Any]:
    current = state(); commands = current.get("commands", [])
    kept = [item for item in commands if item.get("command_id") != command_id]
    if len(kept) == len(commands):
        raise HTTPException(404, "Command not found.")
    current["commands"] = kept
    current["pending"] = [row for row in current.get("pending", []) if row.get("command_id") != command_id]
    persist_commands(kept); save(current)
    return {"detail": "Command removed; DB58 itself was not modified."}


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
    persist_commands(commands)
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


@app.post("/api/telemetry/arm")
def arm_telemetry(request: dict[str, Any]) -> dict[str, Any]:
    """Persist the explicit HA-to-PLC telemetry writer arm."""
    enabled = request.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(422, "enabled must be a boolean.")
    override = read_json(RUNTIME_OPTIONS_FILE, {})
    override["write_enabled"] = enabled
    DATA.mkdir(parents=True, exist_ok=True)
    temporary = RUNTIME_OPTIONS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(override, indent=2) + "\n", encoding="utf-8")
    temporary.replace(RUNTIME_OPTIONS_FILE)
    return {"write_enabled": enabled}


@app.post("/api/points/activate-existing")
def activate_existing_points() -> dict[str, int]:
    """Activate only the reviewed imported map; user-created pending points stay off."""
    if not options().get("write_enabled"):
        raise HTTPException(409, "PLC writes are disabled in add-on settings.")
    current = state()
    count = 0
    for point in current["points"]:
        if point.get("existing") and not point.get("removed"):
            point["enabled"] = True
            count += 1
    save(current)
    return {"activated": count}


@app.post("/api/fresh-start")
def fresh_start() -> dict[str, Any]:
    """Clear imported maps while retaining the app, its connection settings, and audit probe."""
    current = state()
    fresh = {"version": 1, "points": [], "commands": [], "pending": [],
             "last_probe": current.get("last_probe"), "fresh_start": True}
    save(fresh)
    DATA.mkdir(parents=True, exist_ok=True)
    temporary = COMMAND_MAP_FILE.with_suffix(".tmp")
    temporary.write_text('{"commands": []}\n', encoding="utf-8")
    temporary.replace(COMMAND_MAP_FILE)
    return {"points": 0, "commands": 0, "detail": "Fresh bridge map ready."}


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
            # Activation is the operator's confirmation that the DB/CFC side
            # is live. It must also complete the local engineering checklist.
            current["pending"] = [item for item in current.get("pending", [])
                                  if item.get("point_id") != point_id]
            save(current)
            return point
    raise HTTPException(404, "Point not found.")


@app.post("/api/points/reset")
def reset_points() -> dict[str, str]:
    """Clear the user-managed point map for a deliberate fresh start.

    This only changes the bridge's local allow-list.  It never writes to the
    PLC and does not alter any PCS 7 DB/CFC engineering.
    """
    current = state()
    current["points"] = []
    current["pending"] = []
    current["fresh_start"] = True
    save(current)
    return {"detail": "Bridge point map cleared. No PLC data was written."}


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
<section class="section" id="map"><h2>Point map</h2><p class="muted">Activate a point only after its PCS 7 DB/CFC engineering is live and PLC writes have been armed in the app settings. Removing a point only removes it from this bridge map; it does not modify PCS 7.</p><button class="danger" id="resetPoints">Start over — clear all bridge mappings</button><input id="mapSearch" placeholder="Search mapped points"><div class="sort-bar"><button data-sort="name">Name ↕</button><button data-sort="entity_id">Home Assistant ↕</button><button data-sort="pcs7">PCS 7 ↕</button><button data-sort="status">Status ↕</button></div><div class="map-list" id="points"></div></section>
<section class="section" id="commands"><h2>PCS 7 → Home Assistant commands</h2><p class="muted">Each command is a named DB58 CFC signal. The bridge allocates the next DB58 slot, baselines it at startup, and cannot replay it after a restart.</p><div class="card"><strong id="commandArmState">Commands status loading…</strong><p class="muted">The global arm and the individual command activation must both be on before an action can execute.</p><button class="warn" id="commandArm">Loading…</button></div><form id="commandForm"><label>Find an existing Home Assistant entity</label><input id="cmdSearch" autocomplete="off" placeholder="Search by name or entity ID"><label>Available compatible entities</label><select id="cmdEntity" size="8" required><option value="">Loading Home Assistant entities…</option></select><p class="muted" id="cmdEntityCount"></p><label>Display name</label><input id="cmdName" required><label>PCS 7 DB58 member name</label><input id="cmdMember" required maxlength="39" placeholder="CMD_TEST"><label>Signal and action</label><select id="cmdAction" required></select><label>Allocated DB58 byte address</label><input id="cmdByte" readonly value="16"><p class="muted">This is allocated automatically. Add the member to DB58 at this address; do not reuse or edit it.</p><div class="map-fields"><div><label>Minimum (numeric only)</label><input id="cmdMin" type="number" step="any"></div><div><label>Maximum (numeric only)</label><input id="cmdMax" type="number" step="any"></div></div><label>Risk tier</label><select id="cmdTier"><option value="1">1 — test/low impact</option><option value="2">2 — controlled device</option><option value="3">3 — high impact</option></select><button type="button" id="cmdPreview">Preview command</button><button type="submit">Add as pending command</button></form><h2 style="margin-top:20px">Command map</h2><div class="map-list" id="commandsList"></div></section>
<section class="section" id="connection"><h2>Connection test</h2><div class="card"><button class="warn" id="probe">Run one read-only Snap7 connection probe</button><p class="muted">This opens then closes one S7 session. It does not read or write a PLC DB.</p></div></section>
</main><script>
const j=(u,o={})=>fetch(u,o).then(async r=>{let x=await r.json();if(!r.ok){let d=x.detail;if(Array.isArray(d))d=d.map(v=>`${(v.loc||[]).slice(1).join('.')||'field'}: ${v.msg}`).join('\n');throw Error(d||r.statusText)}return x});const n=document.querySelector('#notice'),displayName=document.querySelector('#name');let allEntities=[],points=[],commands=[],sortKey='name',sortDirection=1;const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));function say(x){n.textContent=x;n.style.display='block'}
function memberFor(text){return ('HA_'+text.toUpperCase().replace(/[^A-Z0-9]+/g,'_').replace(/^_+|_+$/g,'')).slice(0,24).replace(/_+$/,'')}
function cmdMemberFor(text){return ('CMD_'+text.toUpperCase().replace(/[^A-Z0-9]+/g,'_').replace(/^_+|_+$/g,'')).slice(0,39).replace(/_+$/,'')};function commandChoices(entityId){let d=(entityId||'').split('.',1)[0];if(['button','input_button'].includes(d))return [{kind:'pulse',action:'activate',label:'Pulse · activate / press'}];if(['input_boolean','switch'].includes(d))return [{kind:'bool',action:'power',label:'BOOL · power on/off'}];if(d==='number'||d==='input_number')return [{kind:'real',action:'set_value',label:'REAL · set numeric value'}];if(d==='climate')return [{kind:'real',action:'set_temperature',label:'REAL · set temperature'}];if(d==='fan')return [{kind:'bool',action:'power',label:'BOOL · power on/off'},{kind:'real',action:'percentage',label:'REAL · set percentage'}];if(d==='light')return [{kind:'bool',action:'power',label:'BOOL · power on/off'},{kind:'real',action:'brightness_pct',label:'REAL · set brightness %'}];return []};function nextCommandByte(kind){let end=16;commands.forEach(x=>end=Math.max(end,Number(x.byte_offset)+(x.kind==='real'?6:2)));return end%2?end+1:end};function chooseCommandEntity(){let x=allEntities.find(x=>x.entity_id===cmdEntity.value),choices=commandChoices(cmdEntity.value);cmdAction.innerHTML=choices.map(x=>`<option value="${x.action}" data-kind="${x.kind}">${x.label}</option>`).join('')||'<option value="">No supported command action</option>';if(x){if(!cmdName.value)cmdName.value=x.name;if(!cmdMember.value)cmdMember.value=cmdMemberFor(x.name)}cmdByte.value=nextCommandByte(cmdAction.selectedOptions[0]?.dataset.kind||'bool')};function renderCommandEntities(){let q=cmdSearch.value.trim().toLowerCase(),shown=allEntities.filter(x=>commandChoices(x.entity_id).length&&(!q||x.entity_id.toLowerCase().includes(q)||x.name.toLowerCase().includes(q)));cmdEntity.innerHTML=shown.map(x=>`<option value="${esc(x.entity_id)}">${esc(x.name)} — ${esc(x.entity_id)} (${esc(x.state)})</option>`).join('')||'<option value="">No compatible entities match</option>';cmdEntityCount.textContent=`${shown.length} compatible entities`;chooseCommandEntity()}
function chooseEntity(){let x=allEntities.find(x=>x.entity_id===entity.value);if(x){if(!displayName.value)displayName.value=x.name;if(!member.value)member.value=memberFor(x.name)}}function renderEntities(){let q=entitySearch.value.trim().toLowerCase(),mapped=new Set(points.filter(x=>!x.removed).map(x=>x.entity_id)),shown=allEntities.filter(x=>!mapped.has(x.entity_id)&&(!q||x.entity_id.toLowerCase().includes(q)||x.name.toLowerCase().includes(q)));entity.innerHTML=shown.map(x=>`<option value="${esc(x.entity_id)}">${esc(x.name)} — ${esc(x.entity_id)} (${esc(x.state)})</option>`).join('')||'<option value="">No unmapped entities match</option>';entityCount.textContent=`${shown.length} available · ${mapped.size} already mapped`;chooseEntity()}
function pointSortValue(x){if(sortKey==='pcs7')return `${String(x.db_number).padStart(4,'0')}:${String(x.byte_offset).padStart(6,'0')}`;if(sortKey==='status')return x.enabled?'active':x.existing?'existing map':'pending';return String(x[sortKey]||'').toLowerCase()}function renderPoints(){let q=mapSearch.value.trim().toLowerCase(),shown=points.filter(x=>!x.removed&&(!q||[x.name,x.entity_id,x.member_name,x.point_id].join(' ').toLowerCase().includes(q))).sort((a,b)=>pointSortValue(a).localeCompare(pointSortValue(b),undefined,{numeric:true})*sortDirection);document.querySelector('#points').innerHTML=shown.map(x=>`<article class="map-item"><div class="map-fields"><div><div class=label>Name</div><div class=map-value>${esc(x.name)}<br><span class=pill>${esc(x.point_id)}</span></div></div><div><div class=label>Home Assistant</div><div class=map-value>${esc(x.entity_id)}</div></div><div><div class=label>PCS 7</div><div class=map-value>DB${x.db_number} · ${esc(x.member_name)}<br><span class=muted>byte ${x.byte_offset}</span></div></div><div><div class=label>Status</div><div class=map-value>${x.enabled?'Active':x.existing?'Existing map':'Pending'}</div></div></div><div class=map-action>${x.enabled?'':`<button class=warn data-activate="${esc(x.point_id)}">Activate point</button>`}<button class=danger data-remove="${esc(x.point_id)}">Remove mapping</button></div></article>`).join('')||'<div class=empty>No mapped points match.</div>';document.querySelectorAll('[data-remove]').forEach(b=>b.onclick=()=>removePoint(b.dataset.remove));document.querySelectorAll('[data-activate]').forEach(b=>b.onclick=()=>activatePoint(b.dataset.activate));document.querySelectorAll('[data-sort]').forEach(b=>b.textContent=b.dataset.sort==='pcs7'?`PCS 7 ${sortKey==='pcs7'?(sortDirection===1?'↑':'↓'):'↕'}`:`${b.dataset.sort==='entity_id'?'Home Assistant':b.dataset.sort[0].toUpperCase()+b.dataset.sort.slice(1)} ${sortKey===b.dataset.sort?(sortDirection===1?'↑':'↓'):'↕'}`)}
function renderCommands(){document.querySelector('#commandsList').innerHTML=commands.map(x=>`<article class=map-item><div class=map-fields><div><div class=label>Command</div><div class=map-value>${esc(x.name)}<br><span class=pill>${esc(x.command_id)}</span></div></div><div><div class=label>Home Assistant</div><div class=map-value>${esc(x.entity_id)} · ${esc(x.action)}</div></div><div><div class=label>PCS 7</div><div class=map-value>DB58 · ${esc(x.member_name)}<br><span class=muted>byte ${esc(x.byte_offset)}</span></div></div><div><div class=label>Safety</div><div class=map-value>${x.enabled?'Active':'Pending'} · Tier ${esc(x.risk_tier)}</div></div></div><div class=map-action>${x.enabled?'':`<button class=warn data-cmd-activate="${esc(x.command_id)}">Activate command</button>`}<button class=danger data-cmd-remove="${esc(x.command_id)}">Remove command</button></div></article>`).join('')||'<div class=empty>No commands are mapped.</div>';document.querySelectorAll('[data-cmd-activate]').forEach(b=>b.onclick=()=>activateCommand(b.dataset.cmdActivate));document.querySelectorAll('[data-cmd-remove]').forEach(b=>b.onclick=()=>removeCommand(b.dataset.cmdRemove))}
async function load(){let[s,p,e,c]=await Promise.all([j('api/status'),j('api/points'),j('api/states').catch(()=>[]),j('api/commands')]);points=p;allEntities=e;commands=c;document.querySelector('#status').innerHTML=`<div class=card><div class=label>PLC endpoint</div><strong>${s.plc.plc_host} · R${s.plc.rack}/S${s.plc.slot}</strong></div><div class=card><div class=label>Mapped points</div><strong>${s.points}</strong></div><div class=card><div class=label>Commands</div><strong>${s.commands} · ${s.plc.commands_enabled?'ARMED':'DISABLED'}</strong></div><div class=card><div class=label>PLC writes</div><strong>${s.plc.write_enabled?'ARMED':'DISABLED'}</strong></div>`;commandArmState.textContent=s.plc.commands_enabled?'Global command arm: ON':'Global command arm: OFF';commandArm.textContent=s.plc.commands_enabled?'Disarm all commands':'Arm command runtime';renderEntities();renderPoints();renderCommands();renderCommandEntities()}
function payload(){return {entity_id:entity.value,name:displayName.value,member_name:member.value,kind:kind.value,unit:unit.value,stale_after_s:Number(stale.value)}};function commandPayload(){let n=x=>x.value===''?null:Number(x.value);return {entity_id:cmdEntity.value,name:cmdName.value,member_name:cmdMember.value,kind:cmdKind.value,action:cmdAction.value,byte_offset:Number(cmdByte.value),min_value:n(cmdMin),max_value:n(cmdMax),risk_tier:Number(cmdTier.value)}};async function removeCommand(id){if(!confirm(`Remove ${id}? This does not modify DB58.`))return;try{let x=await j(`api/commands/${id}`,{method:'DELETE'});say(x.detail);load()}catch(e){say(e.message)}}async function activateCommand(id){if(!confirm(`Activate ${id}? Its first live value will be baselined; it will not execute until a later valid change.`))return;try{let x=await j(`api/commands/${id}/activate`,{method:'POST'});say(`${x.command_id} is active.`);load()}catch(e){say(e.message)}};document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-tab]').forEach(x=>x.classList.toggle('active',x===b));document.querySelectorAll('.section').forEach(x=>x.classList.toggle('active',x.id===b.dataset.tab))});document.querySelectorAll('[data-sort]').forEach(b=>b.onclick=()=>{if(sortKey===b.dataset.sort)sortDirection*=-1;else{sortKey=b.dataset.sort;sortDirection=1}renderPoints()});entitySearch.oninput=renderEntities;mapSearch.oninput=renderPoints;entity.onchange=chooseEntity;async function removePoint(id){if(!confirm(`Remove ${id} from this bridge map? Its PCS 7 address remains reserved.`))return;try{let x=await j(`api/points/${id}`,{method:'DELETE'});say(x.detail);load()}catch(e){say(e.message)}}async function activatePoint(id){if(!confirm(`Activate ${id}? Only do this after its PCS 7 engineering is live.`))return;try{let x=await j(`api/points/${id}/activate`,{method:'POST'});say(`${x.point_id} is active.`);load()}catch(e){say(e.message)}}resetPoints.onclick=async()=>{if(!confirm('Clear every bridge mapping and pending deployment? This does not alter PCS 7.'))return;try{let x=await j('api/points/reset',{method:'POST'});say(x.detail);load()}catch(e){say(e.message)}};preview.onclick=async()=>{try{let x=await j('api/points/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload())});say(`Preview\n${x.point_id}: ${x.entity_id}\nDB${x.db_number}.${x.member_name} at byte ${x.byte_offset}; quality byte ${x.quality_byte_offset}\nNo change has been saved.`)}catch(e){say(e.message)}};addForm.onsubmit=async e=>{e.preventDefault();try{let x=await j('api/points',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload())});say(`Added ${x.point_id} as a pending deployment. It is not enabled and cannot write to the PLC.`);load()}catch(e){say(e.message)}};cmdPreview.onclick=async()=>{try{let x=await j('api/commands/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(commandPayload())});say(`Command preview\n${x.command_id}: DB58.${x.member_name} at byte ${x.byte_offset}\nNo change has been saved.`)}catch(e){say(e.message)}};commandForm.onsubmit=async e=>{e.preventDefault();try{let x=await j('api/commands',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(commandPayload())});say(`Added ${x.command_id} as pending. It cannot execute until you explicitly activate it.`);load()}catch(e){say(e.message)}};probe.onclick=async()=>{try{let x=await j('api/probe',{method:'POST'});say(x.detail);load()}catch(e){say(e.message)}};load();</script></body></html>'''


UI = UI.replace('<p class="sub">One controlled map between Home Assistant and PCS 7. PLC writes remain disabled until a reviewed cutover.</p>', '<p class="sub">Live Home Assistant ↔ PCS 7 bridge</p>')
UI = UI.replace('<p class="muted">Activate a point only after its PCS 7 DB/CFC engineering is live and PLC writes have been armed in the app settings. Removing a point only removes it from this bridge map; it does not modify PCS 7.</p>', '')
UI = UI.replace('Start over — clear all bridge mappings', 'Clear bridge map')
UI = UI.replace('Add as pending deployment', 'Add input')
UI = UI.replace('<p class="muted">Each command is a named DB58 CFC signal. The bridge allocates the next DB58 slot, baselines it at startup, and cannot replay it after a restart.</p>', '')
UI = UI.replace('<p class="muted">The global arm and the individual command activation must both be on before an action can execute.</p>', '')
UI = UI.replace('<p class="muted">This is allocated automatically. Add the member to DB58 at this address; do not reuse or edit it.</p>', '')
UI = UI.replace('<p class="muted">This opens then closes one S7 session. It does not read or write a PLC DB.</p>', '')
UI = UI.replace("</body>", r'''<script>
function commandChoices(entityId){let e=allEntities.find(x=>x.entity_id===entityId),d=(entityId||'').split('.',1)[0],a=e?.command_options||{},choice=[];if(['button','input_button'].includes(d))choice.push({kind:'pulse',action:'activate',label:'Pulse · activate / press'});else if(['input_boolean','switch'].includes(d))choice.push({kind:'bool',action:'power',label:'BOOL · power on/off'});else if(d==='number'||d==='input_number')choice.push({kind:'real',action:'set_value',label:'REAL · set numeric value'});else if(d==='climate'){choice.push({kind:'real',action:'set_temperature',label:'REAL · set temperature'});(Array.isArray(a.hvac_modes)?a.hvac_modes:[]).forEach(v=>choice.push({kind:'pulse',action:'set_hvac_mode',fixed_value:v,label:`Pulse · HVAC mode: ${v}`}));(Array.isArray(a.fan_modes)?a.fan_modes:[]).forEach(v=>choice.push({kind:'pulse',action:'set_fan_mode',fixed_value:v,label:`Pulse · fan mode: ${v}`}))}else if(d==='fan')choice.push({kind:'bool',action:'power',label:'BOOL · power on/off'},{kind:'real',action:'percentage',label:'REAL · set percentage'});else if(d==='light')choice.push({kind:'bool',action:'power',label:'BOOL · power on/off'},{kind:'real',action:'brightness_pct',label:'REAL · set brightness %'});return choice};function chooseCommandEntity(){let x=allEntities.find(x=>x.entity_id===cmdEntity.value),choices=commandChoices(cmdEntity.value);cmdAction.innerHTML=choices.map(x=>`<option value="${x.action}" data-kind="${x.kind}" data-fixed="${esc(x.fixed_value||'')}">${esc(x.label)}</option>`).join('')||'<option value="">No supported command action</option>';if(x){if(!cmdName.value)cmdName.value=x.name;if(!cmdMember.value)cmdMember.value=cmdMemberFor(x.name)}cmdByte.value=nextCommandByte(cmdAction.selectedOptions[0]?.dataset.kind||'bool')};function renderCommandEntities(){let q=cmdSearch.value.trim().toLowerCase(),shown=allEntities.filter(x=>commandChoices(x.entity_id).length&&(!q||x.entity_id.toLowerCase().includes(q)||x.name.toLowerCase().includes(q)));cmdEntity.innerHTML=shown.map(x=>`<option value="${esc(x.entity_id)}">${esc(x.name)} — ${esc(x.entity_id)} (${esc(x.state)})</option>`).join('')||'<option value="">No compatible entities match</option>';cmdEntityCount.textContent=`${shown.length} compatible entities`;chooseCommandEntity()}
function commandPayload(){let n=x=>x.value===''?null:Number(x.value),choice=cmdAction.selectedOptions[0];return {entity_id:cmdEntity.value,name:cmdName.value,member_name:cmdMember.value,kind:choice?.dataset.kind,action:cmdAction.value,fixed_value:choice?.dataset.fixed||null,byte_offset:Number(cmdByte.value),min_value:n(cmdMin),max_value:n(cmdMax),risk_tier:Number(cmdTier.value)}}
cmdSearch.oninput=renderCommandEntities;cmdEntity.onchange=chooseCommandEntity;cmdAction.onchange=()=>{cmdByte.value=nextCommandByte(cmdAction.selectedOptions[0]?.dataset.kind||'bool')};commandArm.onclick=async()=>{let on=commandArm.textContent.includes('Arm command runtime');if(!confirm(`${on?'Arm':'Disarm'} the global command runtime?`))return;try{await j('api/commands/arm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:on})});load().then(renderCommandEntities)}catch(e){say(e.message)}};
setTimeout(()=>{renderCommandEntities();j('api/status').then(s=>{commandArmState.textContent=s.plc.commands_enabled?'Global command arm: ON':'Global command arm: OFF';commandArm.textContent=s.plc.commands_enabled?'Disarm all commands':'Arm command runtime'}).catch(()=>{})},250);
</script></body>''')


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
