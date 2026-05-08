from __future__ import annotations

import logging
import re
import time
from typing import Any

from vastai import VastAI

log = logging.getLogger("vast")


def _scrub(msg: str) -> str:
    """Strip api_key=... from URLs the SDK leaks into exception text."""
    return re.sub(r"([?&])api_key=[^&\s\"]+", r"\1api_key=***", msg)


class VastError(RuntimeError):
    pass


class VastClient:
    """Thin wrapper around the official vastai SDK.

    Reference: https://docs.vast.ai/sdk/python/quickstart
    """

    def __init__(self, api_key: str, *, retry: int = 3):
        if not api_key:
            raise VastError("VAST_API_KEY is empty")
        self._sdk = VastAI(api_key=api_key, retry=retry, quiet=True)

    # context-manager API kept for symmetry with the previous httpx client.
    def close(self) -> None:
        return None

    def __enter__(self) -> "VastClient":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def search_offers(
        self,
        query: str,
        *,
        limit: int = 64,
        order: str = "dph_total",
        type_: str = "on-demand",
    ) -> list[dict[str, Any]]:
        try:
            offers = self._sdk.search_offers(
                query=query,
                type=type_,
                order=order,
                limit=limit,
            )
        except Exception as e:  # SDK raises a generic Exception family
            raise VastError(f"search_offers failed: {_scrub(str(e))}") from None
        return list(offers or [])

    def list_instances(self) -> list[dict[str, Any]]:
        try:
            return list(self._sdk.show_instances() or [])
        except Exception as e:
            raise VastError(f"show_instances failed: {_scrub(str(e))}") from None

    def show_instance(self, instance_id: int) -> dict[str, Any]:
        try:
            return self._sdk.show_instance(id=instance_id) or {}
        except TypeError:
            # SDK bug: when the instance is gone (destroyed), the upstream
            # response is None and the SDK still tries `row['start_date']`,
            # raising TypeError. Treat as not-found.
            return {}
        except Exception as e:
            raise VastError(f"show_instance failed: {_scrub(str(e))}") from None

    def destroy_instance(self, instance_id: int) -> bool:
        """Destroy an instance. Returns True if a deletion actually occurred,
        False if the instance was already gone (idempotent 404)."""
        try:
            self._sdk.destroy_instance(id=instance_id)
            return True
        except Exception as e:
            msg = _scrub(str(e))
            if "404" in msg or "Not Found" in msg:
                log.info("destroy_instance: %s already gone (404)", instance_id)
                return False
            raise VastError(f"destroy_instance failed: {msg}") from None

    def wait_for_destroyed(
        self,
        instance_id: int,
        *,
        timeout: float = 120.0,
        poll_interval: float = 3.0,
    ) -> bool:
        """Block until the instance is no longer present in the account.

        Polls list_instances and returns True once gone. Raises VastError on
        timeout.
        """
        target = int(instance_id)
        deadline = time.monotonic() + timeout
        last_log = 0.0
        while time.monotonic() < deadline:
            ids = {int(i.get("id", 0)) for i in self.list_instances()}
            if target not in ids:
                return True
            now = time.monotonic()
            if now - last_log > 15:
                log.info("waiting for instance %s to disappear", target)
                last_log = now
            time.sleep(poll_interval)
        raise VastError(f"timed out after {timeout:.0f}s waiting for instance {instance_id} to disappear")

    def stop_instance(self, instance_id: int) -> dict[str, Any]:
        try:
            return self._sdk.stop_instance(id=int(instance_id)) or {}
        except Exception as e:
            raise VastError(f"stop_instance failed: {_scrub(str(e))}") from None

    def start_instance(self, instance_id: int) -> dict[str, Any]:
        try:
            return self._sdk.start_instance(id=int(instance_id)) or {}
        except Exception as e:
            raise VastError(f"start_instance failed: {_scrub(str(e))}") from None

    def get_logs(
        self,
        instance_id: int,
        *,
        tail: int | None = None,
        filter_: str | None = None,
        daemon: bool = False,
    ) -> str:
        """Fetch instance logs as text. Returns empty string if not available yet."""
        try:
            result = self._sdk.logs(
                instance_id=int(instance_id),
                tail=str(tail) if tail else None,
                filter=filter_,
                daemon_logs=daemon,
            )
        except Exception as e:
            raise VastError(f"logs failed (instance_id={instance_id}): {_scrub(str(e))}") from None
        if isinstance(result, dict):
            # SDK returned an unresolved envelope (e.g. result still pending).
            return result.get("output") or result.get("logs") or ""
        return str(result or "")

    def attach_ssh(self, instance_id: int, ssh_pubkey: str) -> dict[str, Any]:
        """Attach an SSH public key to an existing instance.

        Vast.ai's create_instance does not accept a per-instance SSH key, so
        the key has to be attached after creation via this endpoint.
        """
        try:
            return self._sdk.attach_ssh(instance_id=int(instance_id), ssh_key=ssh_pubkey) or {}
        except Exception as e:
            raise VastError(f"attach_ssh failed (instance_id={instance_id}): {_scrub(str(e))}") from None

    def create_instance(
        self,
        offer_id: int,
        *,
        image: str,
        disk_gb: float,
        env: dict[str, str],
        onstart: str,
        runtype: str = "ssh",
        label: str | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "id": int(offer_id),
            "image": image,
            "disk": float(disk_gb),
            "env": env,
            "onstart_cmd": onstart,
            "runtype": runtype,
        }
        if label:
            kwargs["label"] = label
        try:
            return self._sdk.create_instance(**kwargs) or {}
        except Exception as e:
            raise VastError(f"create_instance failed (offer_id={offer_id}): {_scrub(str(e))}") from None
