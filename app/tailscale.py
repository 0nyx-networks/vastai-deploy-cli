from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger("tailscale")

TS_BASE = "https://api.tailscale.com/api/v2"


class TailscaleError(RuntimeError):
    pass


class TailscaleClient:
    """Tailscale REST API client.

    Reference: https://tailscale.com/api
    """

    def __init__(self, api_key: str, tailnet: str = "-", *, timeout: float = 30.0):
        if not api_key:
            raise TailscaleError("TAILSCALE_API_KEY is empty")
        self._tailnet = tailnet
        self._client = httpx.Client(
            base_url=TS_BASE,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "TailscaleClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _raise(self, r: httpx.Response) -> dict[str, Any]:
        if r.status_code >= 400:
            raise TailscaleError(
                f"Tailscale {r.request.method} {r.request.url} -> {r.status_code}: {r.text}"
            )
        if not r.content:
            return {}
        return r.json()

    @property
    def tailnet_path(self) -> str:
        # "-" is a valid alias meaning the default tailnet for the token.
        return quote(self._tailnet, safe="")

    def list_devices(self) -> list[dict[str, Any]]:
        r = self._client.get(f"/tailnet/{self.tailnet_path}/devices")
        return self._raise(r).get("devices", [])

    def delete_device(self, device_id: str) -> None:
        r = self._client.delete(f"/device/{quote(device_id, safe='')}")
        self._raise(r)

    def find_offline_by_hostname(self, hostname: str) -> list[dict[str, Any]]:
        """Return devices whose hostname/name matches and that are not online.

        Tailscale exposes connectivity via the boolean `online` field on each
        device record (https://tailscale.com/api#tag/devices). We compare both
        `hostname` (Tailscale name) and the leading label of `name` (the
        FQDN), since Vast.ai pods commonly set hostname via the auth-key flow.
        """
        out: list[dict[str, Any]] = []
        target = hostname.lower()
        for d in self.list_devices():
            if d.get("online", False):
                continue
            host = (d.get("hostname") or "").lower()
            fqdn_label = (d.get("name") or "").split(".", 1)[0].lower()
            if target in (host, fqdn_label):
                out.append(d)
        return out

    def purge_offline(self, hostname: str) -> list[str]:
        """Delete every Offline device sharing this hostname. Returns deleted IDs."""
        deleted: list[str] = []
        for d in self.find_offline_by_hostname(hostname):
            dev_id = d.get("nodeId") or d.get("id")
            if not dev_id:
                continue
            self.delete_device(str(dev_id))
            deleted.append(str(dev_id))
        return deleted

    def find_all_by_hostname(self, hostname: str) -> list[dict[str, Any]]:
        """Return every device whose hostname/name matches (online + offline)."""
        out: list[dict[str, Any]] = []
        target = hostname.lower()
        for d in self.list_devices():
            host = (d.get("hostname") or "").lower()
            fqdn_label = (d.get("name") or "").split(".", 1)[0].lower()
            if target in (host, fqdn_label):
                out.append(d)
        return out

    def delete_by_hostname(self, hostname: str) -> list[str]:
        """Delete every device matching this hostname regardless of state."""
        deleted: list[str] = []
        for d in self.find_all_by_hostname(hostname):
            dev_id = d.get("nodeId") or d.get("id")
            if not dev_id:
                continue
            self.delete_device(str(dev_id))
            deleted.append(str(dev_id))
        return deleted

    def _candidate_labels(self, d: dict[str, Any]) -> set[str]:
        """All hostname-like strings on a device record we want to match against."""
        labels: set[str] = set()
        for key in ("hostname", "givenName", "displayName"):
            v = d.get(key)
            if isinstance(v, str) and v:
                labels.add(v.lower())
        name = d.get("name")
        if isinstance(name, str) and name:
            labels.add(name.split(".", 1)[0].lower())
        return labels

    def find_online_by_hostname(self, hostname: str) -> dict[str, Any] | None:
        """Return the first device matching this hostname that is not
        explicitly Offline. Tailscale's `online` field can be True, False,
        or None (unknown / not yet populated for freshly-joined devices),
        so we only reject `False`. The pre-flight `purge_offline` removes
        same-name offline duplicates, so any remaining match is the device
        we just created.
        """
        target = hostname.lower()
        for d in self.list_devices():
            if d.get("online") is False:
                continue
            if target in self._candidate_labels(d):
                return d
        return None

    def wait_for_online(
        self,
        hostname: str,
        *,
        timeout: float = 300.0,
        poll_interval: float = 5.0,
    ) -> dict[str, Any]:
        """Poll the device list until a matching online device appears.

        Raises TailscaleError on timeout. Logs progress every 30s, and on
        each progress tick surfaces near-miss candidates (devices whose
        labels contain the target as a substring) so naming mismatches
        can be diagnosed.
        """
        target = hostname.lower()
        deadline = time.monotonic() + timeout
        last_log = 0.0
        attempts = 0
        while time.monotonic() < deadline:
            attempts += 1
            devices = self.list_devices()
            for d in devices:
                # Treat None (unknown) the same as True — see find_online_by_hostname.
                if d.get("online") is False:
                    continue
                if target in self._candidate_labels(d):
                    return d

            now = time.monotonic()
            if now - last_log > 30:
                elapsed = int(timeout - (deadline - now))
                # Show every device whose any candidate label contains the
                # target as a substring, plus its online flag — usually
                # exposes auto-rename suffixes (`ollama-1`) or hostname
                # mismatches (OS hostname == container id).
                near = [
                    {
                        "id": d.get("nodeId") or d.get("id"),
                        "online": d.get("online"),
                        "hostname": d.get("hostname"),
                        "name": d.get("name"),
                        "labels": sorted(self._candidate_labels(d)),
                    }
                    for d in devices
                    if any(target in lbl for lbl in self._candidate_labels(d))
                ]
                log.info(
                    "waiting for tailscale hostname=%s (elapsed=%ds, attempt=%d, total_devices=%d, near_matches=%s)",
                    hostname, elapsed, attempts, len(devices), near,
                )
                last_log = now
            time.sleep(poll_interval)
        raise TailscaleError(
            f"timed out after {timeout:.0f}s waiting for tailscale device hostname={hostname!r}"
        )

    def create_auth_key(
        self,
        *,
        tags: list[str],
        ephemeral: bool = True,
        reusable: bool = False,
        preauthorized: bool = True,
        expiry_seconds: int = 3600,
        description: str = "deploy-vast-ai",
    ) -> dict[str, Any]:
        """Create an auth key. Returns the full API record (must include `id`
        and `key`). Caller can revoke later with `revoke_key(id)`.
        """
        body = {
            "capabilities": {
                "devices": {
                    "create": {
                        "reusable": reusable,
                        "ephemeral": ephemeral,
                        "preauthorized": preauthorized,
                        "tags": tags,
                    }
                }
            },
            "expirySeconds": expiry_seconds,
            "description": description,
        }
        r = self._client.post(f"/tailnet/{self.tailnet_path}/keys", json=body)
        data = self._raise(r)
        if not data.get("key") or not data.get("id"):
            raise TailscaleError(f"auth key creation returned incomplete record: {data}")
        return data

    def revoke_key(self, key_id: str) -> None:
        """Revoke an auth key by id. Already-registered devices keep their
        connections — auth keys are only used for the initial node-key
        exchange, so revoking after `wait_for_online` is safe.
        """
        r = self._client.delete(f"/tailnet/{self.tailnet_path}/keys/{quote(key_id, safe='')}")
        self._raise(r)
