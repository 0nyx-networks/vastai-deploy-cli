from __future__ import annotations

import json
import time
from pathlib import Path

from .config import ROOT

DENYLIST_PATH = ROOT / "denylist.json"


def _empty() -> dict:
    return {"machine_ids": [], "host_ids": [], "notes": {}}


def load() -> dict:
    if not DENYLIST_PATH.exists():
        return _empty()
    try:
        data = json.loads(DENYLIST_PATH.read_text())
    except json.JSONDecodeError:
        return _empty()
    data.setdefault("machine_ids", [])
    data.setdefault("host_ids", [])
    data.setdefault("notes", {})
    return data


def save(data: dict) -> None:
    DENYLIST_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def add(machine_id: int | None, host_id: int | None, note: str = "") -> None:
    data = load()
    if machine_id is not None and int(machine_id) not in data["machine_ids"]:
        data["machine_ids"].append(int(machine_id))
    if host_id is not None and int(host_id) not in data["host_ids"]:
        data["host_ids"].append(int(host_id))
    if machine_id is not None:
        data["notes"][str(int(machine_id))] = {
            "host_id": host_id,
            "added": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "note": note,
        }
    if host_id is not None:
        data["notes"][str(int(host_id))] = {
            "machine_id": machine_id,
            "added": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "note": note,
        }
    save(data)


def remove(machine_id: int | None = None, host_id: int | None = None) -> None:
    data = load()
    if machine_id is not None:
        data["machine_ids"] = [m for m in data["machine_ids"] if int(m) != int(machine_id)]
        data["notes"].pop(str(int(machine_id)), None)
    if host_id is not None:
        data["host_ids"] = [h for h in data["host_ids"] if int(h) != int(host_id)]
        data["notes"].pop(str(int(host_id)), None)
    save(data)


def is_denied(offer: dict) -> bool:
    data = load()
    mids = {int(m) for m in data["machine_ids"]}
    hids = {int(h) for h in data["host_ids"]}
    if offer.get("machine_id") in mids:
        return True
    if offer.get("host_id") in hids:
        return True
    return False
