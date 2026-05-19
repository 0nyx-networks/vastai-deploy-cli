from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from . import denylist
from .config import SearchFilter, Settings, TargetProfile  # noqa: F401  (SearchFilter for type hint)
from .tailscale import TailscaleClient
from .vast import VastClient, VastError

log = logging.getLogger("deploy")


@dataclass
class DeployRequest:
    target: str
    name: str | None = None  # Tailscale hostname / Vast label
    offer_id: int | None = None
    dry_run: bool = False
    wait_for_tailscale: bool = True
    wait_timeout: float = 300.0


@dataclass
class DeployResult:
    target: str
    hostname: str
    offer_id: int
    instance_id: int | None
    deleted_offline_devices: list[str]
    auth_key_created: bool
    tailscale_online: bool
    raw: dict[str, Any]


_HOSTNAME_RE = re.compile(r"[^a-z0-9-]+")


def normalize_hostname(name: str) -> str:
    """Tailscale hostnames must be DNS-safe; lower-case and strip junk."""
    s = _HOSTNAME_RE.sub("-", name.lower()).strip("-")
    return s[:60] or "vast"


def _country_code(geo: str | None) -> str:
    """Extract ISO country code from a Vast.ai geolocation string.

    Vast.ai returns values like "California, US" or "Japan, JP". The trailing
    2-letter token is the ISO-3166 alpha-2 code.
    """
    if not geo:
        return ""
    tail = geo.rsplit(",", 1)[-1].strip().upper()
    return tail if len(tail) == 2 else geo.strip().upper()


def filter_offers(offers: list[dict[str, Any]], search: "SearchFilter") -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Defensive client-side filter. Tracks per-criterion rejection counts
    so a mismatch is observable instead of a silent zero.
    """
    out = []
    rejects = {"denylist": 0, "gpu_name": 0, "geolocation": 0, "cuda_min": 0,
               "gpu_ram": 0, "max_dph": 0, "verified": 0, "rentable": 0}
    gpu_names = {n.lower() for n in (search.gpu_name or [])}
    geos = {g.upper() for g in (search.geolocation or [])}
    for o in offers:
        if denylist.is_denied(o):
            rejects["denylist"] += 1; continue
        if gpu_names and (o.get("gpu_name") or "").lower() not in gpu_names:
            rejects["gpu_name"] += 1; continue
        if geos and _country_code(o.get("geolocation")) not in geos:
            rejects["geolocation"] += 1; continue
        if search.cuda_min is not None and float(o.get("cuda_max_good") or 0) < search.cuda_min:
            rejects["cuda_min"] += 1; continue
        # gpu_ram is reported in MB on Vast.ai; use *1000 to accept 32000 too.
        if search.gpu_ram_gb_min is not None and float(o.get("gpu_ram") or 0) < search.gpu_ram_gb_min * 1000:
            rejects["gpu_ram"] += 1; continue
        if search.max_dph is not None and float(o.get("dph_total") or 1e9) > search.max_dph:
            rejects["max_dph"] += 1; continue
        # `verified` may be True / False / None on the offer record. Treat
        # None as "unknown" and let it pass — the SDK already filters by
        # default with `verified=true`, so anything we got here was deemed
        # acceptable upstream.
        if search.verified and o.get("verified") is False:
            rejects["verified"] += 1; continue
        if search.rentable and o.get("rentable") is False:
            rejects["rentable"] += 1; continue
        out.append(o)
    if not out:
        log.warning("filter_offers rejected all %d offers: %s", len(offers), rejects)
    return out, rejects


def _offer_reject_reasons(o: dict[str, Any], search: "SearchFilter") -> list[str]:
    """Return a list of filter criteria that reject this single offer."""
    reasons: list[str] = []
    gpu_names = {n.lower() for n in (search.gpu_name or [])}
    geos = {g.upper() for g in (search.geolocation or [])}
    if denylist.is_denied(o):
        reasons.append("denylist")
    if gpu_names and (o.get("gpu_name") or "").lower() not in gpu_names:
        reasons.append(f"gpu_name={o.get('gpu_name')!r} not in {sorted(gpu_names)}")
    if geos and _country_code(o.get("geolocation")) not in geos:
        reasons.append(f"geolocation={o.get('geolocation')!r} not in {sorted(geos)}")
    if search.cuda_min is not None and float(o.get("cuda_max_good") or 0) < search.cuda_min:
        reasons.append(f"cuda_max_good={o.get('cuda_max_good')} < {search.cuda_min}")
    if search.gpu_ram_gb_min is not None and float(o.get("gpu_ram") or 0) < search.gpu_ram_gb_min * 1000:
        reasons.append(f"gpu_ram={o.get('gpu_ram')}MB < {search.gpu_ram_gb_min}GB")
    if search.max_dph is not None and float(o.get("dph_total") or 1e9) > search.max_dph:
        reasons.append(f"dph_total={o.get('dph_total')} > max_dph={search.max_dph}")
    if search.verified and o.get("verified") is False:
        reasons.append("verified=False")
    if search.rentable and o.get("rentable") is False:
        reasons.append("rentable=False")
    return reasons


def rank_offers(offers: list[dict[str, Any]], geo_priority: list[str]) -> list[dict[str, Any]]:
    """Sort offers by (country priority, price). Cheapest in preferred
    country first; countries outside the priority list are pushed to the end.
    """
    if not offers:
        raise RuntimeError("no Vast.ai offers matched the search filter")

    rank = {c.upper(): i for i, c in enumerate(geo_priority)}
    fallback_rank = len(rank)

    def sort_key(o: dict[str, Any]) -> tuple[int, float]:
        cc = _country_code(o.get("geolocation"))
        return (rank.get(cc, fallback_rank), float(o.get("dph_total", 1e9)))

    return sorted(offers, key=sort_key)


def _tag_list(profile_tag: str | None, settings: Settings) -> list[str]:
    """Resolve the tag list. Profile env wins over .env settings."""
    if profile_tag:
        return [t.strip() for t in profile_tag.split(",") if t.strip()]
    return settings.tailscale_tag_list


def _with_tag_prefix(tags: list[str]) -> list[str]:
    """Tailscale ACL requires tag identifiers to start with `tag:`."""
    return [t if t.startswith("tag:") else f"tag:{t}" for t in tags]


def deploy(req: DeployRequest, settings: Settings | None = None) -> DeployResult:
    settings = settings or Settings()
    profile = TargetProfile.load(req.target)

    # Hostname precedence: CLI --name > profile.env.TAILSCALE_HOSTNAME > auto.
    profile_hostname = profile.env.get("TAILSCALE_HOSTNAME")
    fallback = profile_hostname or f"vast-{req.target}-{int(time.time())}"
    hostname = normalize_hostname(req.name or fallback)
    log.info("hostname=%s target=%s", hostname, req.target)

    # Pre-flight: refuse if a Vast.ai instance already exists with this label.
    # Two concurrent instances would double-bill and force Tailscale to rename
    # the new node (ollama -> ollama-1). User must explicitly destroy first.
    if not req.dry_run:
        with VastClient(settings.vast_api_key) as _vast:
            existing = [
                i for i in _vast.list_instances()
                if str(i.get("label") or "") == hostname
            ]
        if existing:
            ids = [int(i["id"]) for i in existing]
            raise RuntimeError(
                f"Vast.ai instance(s) with label={hostname!r} already exist: {ids}. "
                f"Destroy first: python -m app destroy {ids[0]}"
            )

    # Tag precedence: profile.env.TAILSCALE_TAG > .env TAILSCALE_TAGS.
    raw_tags = _tag_list(profile.env.get("TAILSCALE_TAG"), settings)
    api_tags = _with_tag_prefix(raw_tags)

    deleted: list[str] = []
    auth_key: str = ""
    auth_key_id: str = ""

    # Purge stale offline devices up-front so the new node can claim the
    # hostname later. Auth-key creation is deferred until we know a Vast.ai
    # offer is actually deployable — otherwise we'd waste a (short-lived but
    # tagged) key when no candidate matches the search filter.
    with TailscaleClient(settings.tailscale_api_key, settings.tailscale_tailnet) as ts:
        deleted = ts.purge_offline(hostname)
        if deleted:
            log.info("deleted offline tailscale devices: %s", deleted)

    with VastClient(settings.vast_api_key) as vast:
        if req.offer_id is not None:
            candidates = [{"id": req.offer_id, "geolocation": "?", "gpu_name": "?", "dph_total": 0.0}]
            log.info("using user-specified offer id=%s", req.offer_id)
        else:
            qs = profile.search.to_vast_query_string()
            log.info("vast query: %s", qs)
            raw_offers = vast.search_offers(qs, limit=64)
            log.info("api returned %d raw offers", len(raw_offers))
            if raw_offers:
                # Always show one raw offer's relevant fields so the user can
                # confirm the schema (especially geolocation / gpu_name format).
                s = raw_offers[0]
                log.info(
                    "raw[0]: id=%s gpu_name=%r geolocation=%r cuda_max_good=%s gpu_ram=%s dph_total=%s verified=%s rentable=%s",
                    s.get("id"), s.get("gpu_name"), s.get("geolocation"),
                    s.get("cuda_max_good"), s.get("gpu_ram"), s.get("dph_total"),
                    s.get("verified"), s.get("rentable"),
                )
            filtered, rejects = filter_offers(raw_offers, profile.search)
            log.info("after client-side filter: %d offers", len(filtered))
            if not filtered:
                preview = [
                    {
                        "id": o.get("id"),
                        "gpu_name": o.get("gpu_name"),
                        "geolocation": o.get("geolocation"),
                        "cuda_max_good": o.get("cuda_max_good"),
                        "gpu_ram": o.get("gpu_ram"),
                        "dph_total": o.get("dph_total"),
                        "verified": o.get("verified"),
                        "rentable": o.get("rentable"),
                        "reject_reason": _offer_reject_reasons(o, profile.search),
                    }
                    for o in raw_offers[:5]
                ]
                raise RuntimeError(
                    f"no offers passed filter (raw={len(raw_offers)}). "
                    "Reason: " + json.dumps({k: v for k, v in rejects.items() if v > 0}, ensure_ascii=False) + "\n"
                    "Top raw samples:\n"
                    + json.dumps(preview, indent=2, ensure_ascii=False)
                    + "\nTry relaxing search criteria."
                )
            candidates = rank_offers(filtered, profile.search.geolocation)

        if req.dry_run:
            top = candidates[0]
            return DeployResult(
                target=req.target,
                hostname=hostname,
                offer_id=int(top["id"]),
                instance_id=None,
                deleted_offline_devices=deleted,
                auth_key_created=False,
                tailscale_online=False,
                raw={
                    "dry_run": True,
                    "env_keys": sorted({**profile.env, "TAILSCALE_HOSTNAME": "", "TAILSCALE_AUTHKEY": ""}.keys()),
                    "top_candidates": [
                        {k: c.get(k) for k in ("id", "gpu_name", "dph_total", "geolocation")}
                        for c in candidates[:5]
                    ],
                },
            )

        # A deployable candidate exists — only now do we issue a Tailscale
        # auth-key. The key is short-lived (10 min) and consumed at container
        # start, so issuing it any earlier (e.g. before the offer search)
        # would burn a tagged key whenever the filter returns nothing.
        with TailscaleClient(settings.tailscale_api_key, settings.tailscale_tailnet) as ts:
            key_record = ts.create_auth_key(
                tags=api_tags,
                ephemeral=True,
                reusable=False,
                preauthorized=True,
                expiry_seconds=600,
                description=f"deploy-vast-ai {hostname}",
            )
            auth_key = key_record["key"]
            auth_key_id = key_record["id"]
            log.info("created tailscale auth-key id=%s (ephemeral, tags=%s)",
                     auth_key_id, api_tags)

        # Container env: profile values are the source of truth; we only
        # inject the freshly-issued AUTHKEY and the resolved hostname.
        # TAILSCALE_TAG (if any) is preserved from profile verbatim.
        env = {
            **profile.env,
            "TAILSCALE_HOSTNAME": hostname,
            "TAILSCALE_AUTHKEY": auth_key,
        }

        resp: dict[str, Any] = {}
        instance_id = None
        offer_id = 0
        last_err: Exception | None = None
        for c in candidates[:10]:  # try up to 10 in priority order
            offer_id = int(c["id"])
            log.info(
                "create attempt offer_id=%s gpu=%s dph=%.4f geo=%s",
                offer_id, c.get("gpu_name"), c.get("dph_total", 0.0), c.get("geolocation"),
            )
            try:
                resp = vast.create_instance(
                    offer_id=offer_id,
                    image=profile.image,
                    disk_gb=float(profile.disk_gb),
                    env=env,
                    onstart=profile.onstart_text(),
                    runtype=profile.runtype,
                    label=hostname,
                )
                instance_id = resp.get("new_contract") or resp.get("instance_id")
                break
            except VastError as e:
                msg = str(e)
                last_err = e
                # 3603 = no_such_ask (offer just got taken). Try next.
                if "no_such_ask" in msg or "3603" in msg or "not available" in msg:
                    log.warning("offer %s unavailable; trying next", offer_id)
                    continue
                raise

        if instance_id is None:
            raise RuntimeError(
                f"all {len(candidates[:10])} top candidates were unavailable; last error: {last_err}"
            )

        # Per-instance SSH key must be attached after creation; create_instance
        # does not accept it as a kwarg.
        if settings.ssh_public_key:
            try:
                vast.attach_ssh(instance_id, settings.ssh_public_key)
                log.info("attached ssh key to instance %s", instance_id)
            except VastError as e:
                log.warning("attach_ssh failed (non-fatal): %s", e)

    # Wait for Tailscale registration outside the Vast.ai client context.
    online = False
    if req.wait_for_tailscale:
        log.info("waiting up to %.0fs for tailscale registration of %s",
                 req.wait_timeout, hostname)
        with TailscaleClient(settings.tailscale_api_key, settings.tailscale_tailnet) as ts:
            try:
                dev = ts.wait_for_online(hostname, timeout=req.wait_timeout)
                log.info("tailscale online: name=%s addresses=%s",
                         dev.get("name"), dev.get("addresses"))
                online = True
            except Exception as e:
                log.error(
                    "tailscale wait failed: %s. Instance %s is still running; "
                    "destroy it manually if you want to abort.",
                    e, instance_id,
                )

            # Revoke the auth key now that the device is registered. Already-
            # registered devices keep their connection (auth keys are only
            # used at registration time), so this just prevents a leaked key
            # from being used to enroll another node.
            if online and auth_key_id:
                try:
                    ts.revoke_key(auth_key_id)
                    log.info("revoked tailscale auth-key id=%s", auth_key_id)
                except Exception as e:
                    log.warning("revoke_key failed (non-fatal): %s", e)

    return DeployResult(
        target=req.target,
        hostname=hostname,
        offer_id=offer_id,
        instance_id=int(instance_id) if instance_id else None,
        deleted_offline_devices=deleted,
        auth_key_created=True,
        tailscale_online=online,
        raw=resp,
    )
