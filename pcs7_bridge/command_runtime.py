"""Fail-closed DB58 command engine for PCS 7 Bridge.

The engine deliberately knows only explicitly declared slots.  It baselines
them at startup, so a container restart can never replay a PLC command.
"""
from __future__ import annotations

import json
import math
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

STATUS_IDLE = 0
STATUS_SUCCESS = 2
STATUS_DISABLED = 3
STATUS_VALIDATION = 4
STATUS_HA_FAILURE = 5
STATUS_REPLAY_SUPPRESSED = 6


@dataclass(frozen=True)
class Command:
    command_id: str
    byte_offset: int
    entity_id: str
    action: str
    kind: str
    min_value: float | None = None
    max_value: float | None = None
    fixed_value: object | None = None
    enabled: bool = False
    risk_tier: int = 1

    @classmethod
    def from_dict(cls, raw: dict) -> "Command":
        command = cls(
            command_id=str(raw["command_id"]), byte_offset=int(raw["byte_offset"]),
            entity_id=str(raw["entity_id"]), action=str(raw["action"]),
            kind=str(raw["kind"]), min_value=raw.get("min_value"),
            max_value=raw.get("max_value"), fixed_value=raw.get("fixed_value"),
            enabled=bool(raw.get("enabled", False)), risk_tier=int(raw.get("risk_tier", 1)),
        )
        if command.kind not in {"bool", "real", "pulse"}:
            raise ValueError("unsupported command kind")
        if command.byte_offset < 16 or command.byte_offset % 2:
            raise ValueError("invalid DB58 command offset")
        if command.risk_tier not in {1, 2, 3}:
            raise ValueError("invalid command risk tier")
        return command

    @property
    def size(self) -> int:
        return 6 if self.kind == "real" else 2

    def normalize(self, raw: float) -> object:
        if not math.isfinite(raw):
            raise ValueError("non-finite command value")
        if self.kind == "bool":
            if raw not in {0.0, 1.0}:
                raise ValueError("BOOL must be 0 or 1")
            return bool(raw)
        if self.kind == "pulse":
            if raw != 1.0:
                raise ValueError("pulse must be 1")
            return self.fixed_value if self.fixed_value is not None else True
        if self.min_value is not None and raw < float(self.min_value):
            raise ValueError("below configured minimum")
        if self.max_value is not None and raw > float(self.max_value):
            raise ValueError("above configured maximum")
        return raw


class Journal:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            self.data = {}

    def record(self, command_id: str, token: bytes) -> None:
        self.data[command_id] = token.hex()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.data, sort_keys=True))
        temp.replace(self.path)


class CommandEngine:
    """DB58 cfc-signal value-change command processor."""
    def __init__(self, commands: list[dict], read_db: Callable, write_db: Callable,
                 execute: Callable[[Command, object], None], journal: Journal,
                 max_risk_tier: int = 1):
        self.commands = [Command.from_dict(item) for item in commands]
        self.read_db, self.write_db, self.execute = read_db, write_db, execute
        self.journal, self.max_risk_tier = journal, max_risk_tier
        self.baselined = False
        self.last: dict[str, bytes] = {}
        self.db_size = max([16] + [command.byte_offset + command.size for command in self.commands])

    @staticmethod
    def _token(command: Command, image: bytes) -> bytes:
        raw = image[command.byte_offset:command.byte_offset + command.size]
        return raw[:4] if command.kind == "real" else bytes((raw[0] & 1,))

    @staticmethod
    def _value(command: Command, token: bytes) -> float:
        return struct.unpack(">f", token)[0] if command.kind == "real" else float(token[0] & 1)

    def _reply(self, command: Command, status: int) -> None:
        # DB58 header: active slot, status.  It is feedback only; PLC owns input slots.
        slot = sorted(self.commands, key=lambda c: c.byte_offset).index(command) + 1
        self.write_db(58, 8, struct.pack(">hh", slot, status))

    def poll(self) -> list[tuple[str, int]]:
        image = self.read_db(58, 0, self.db_size)
        if len(image) != self.db_size:
            raise RuntimeError("short DB58 read")
        current = {command.command_id: self._token(command, image) for command in self.commands}
        if not self.baselined:
            self.last = current
            for command, token in ((c, current[c.command_id]) for c in self.commands):
                self.journal.record(command.command_id, token)
                if self._value(command, token):
                    self._reply(command, STATUS_REPLAY_SUPPRESSED)
            self.baselined = True
            return []
        completed = []
        for command in self.commands:
            token, previous = current[command.command_id], self.last.get(command.command_id)
            if token == previous:
                continue
            self.last[command.command_id] = token
            raw = self._value(command, token)
            # A pulse dispatches only on its 0 -> 1 edge. Zero simply rearms it.
            if command.kind == "pulse" and (raw != 1.0 or previous == b"\x01"):
                continue
            self.journal.record(command.command_id, token)
            if not command.enabled or command.risk_tier > self.max_risk_tier:
                self._reply(command, STATUS_DISABLED); completed.append((command.command_id, STATUS_DISABLED)); continue
            try:
                value = command.normalize(raw)
            except ValueError:
                self._reply(command, STATUS_VALIDATION); completed.append((command.command_id, STATUS_VALIDATION)); continue
            try:
                self.execute(command, value)
            except Exception:
                self._reply(command, STATUS_HA_FAILURE); completed.append((command.command_id, STATUS_HA_FAILURE)); continue
            self._reply(command, STATUS_SUCCESS); completed.append((command.command_id, STATUS_SUCCESS))
        return completed
