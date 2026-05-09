from __future__ import annotations

import json
import logging
import sys
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .config import Settings, TEMPLATE_DIR, TargetProfile
from .deploy import DeployRequest, deploy
from .tailscale import TailscaleClient, TailscaleError
from .vast import VastClient, VastError

app = typer.Typer(
    add_completion=True,
    help="Deploy Vast.ai pods with Tailscale auth-key provisioning. "
    "Run `--install-completion bash` to enable shell tab-completion.",
)
console = Console()


def _die(msg: str) -> None:
    """Print a one-line red error and exit with code 1, no traceback."""
    console.print(f"[red]error:[/red] {msg}")
    raise typer.Exit(code=1)


def _resolve_instance_id(vast: VastClient, ref: str) -> int:
    """Resolve an instance reference (numeric id or label) to an int id.

    All-digits → treated as id. Otherwise looked up against instance labels;
    raises if 0 or 2+ matches.
    """
    s = str(ref).strip()
    if s.isdigit():
        return int(s)
    matches = [i for i in vast.list_instances() if str(i.get("label") or "") == s]
    if not matches:
        raise VastError(f"no instance with label={s!r}")
    if len(matches) > 1:
        ids = [int(m["id"]) for m in matches]
        raise VastError(f"label={s!r} matches multiple instances {ids}; pass an id instead")
    return int(matches[0]["id"])


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)


@app.command("deploy")
def cmd_deploy(
    target: Optional[str] = typer.Argument(None, help="Target template name under templates/ (e.g. ollama, comfyui). Auto-selected if only one template exists."),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Hostname / Vast label. Auto-generated if omitted."),
    offer_id: Optional[int] = typer.Option(None, "--offer-id", help="Vast.ai offer id. Skips auto-search if set."),
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--execute",
        help="Default is dry-run (plan only). Pass --execute to actually deploy.",
    ),
    wait: bool = typer.Option(
        True,
        "--wait/--no-wait",
        help="Wait for the new pod to register with Tailscale before returning.",
    ),
    wait_timeout: float = typer.Option(
        300.0, "--wait-timeout", help="Tailscale registration wait timeout (seconds)."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """End-to-end one-shot deploy."""
    _setup_logging(verbose)
    if target is None:
        available = sorted(p.name for p in TEMPLATE_DIR.iterdir() if p.is_dir() and not p.name.startswith("_"))
        if len(available) == 1:
            target = available[0]
        elif available:
            _die(f"TARGET argument is required. Available targets: {', '.join(available)}")
        else:
            _die("TARGET argument is required (no templates found)")
    req = DeployRequest(
        target=target, name=name, offer_id=offer_id, dry_run=dry_run,
        wait_for_tailscale=wait, wait_timeout=wait_timeout,
    )
    try:
        result = deploy(req)
    except (VastError, TailscaleError, FileNotFoundError, RuntimeError) as e:
        _die(str(e))
    console.print_json(
        json.dumps(
            {
                "target": result.target,
                "hostname": result.hostname,
                "offer_id": result.offer_id,
                "instance_id": result.instance_id,
                "deleted_offline_devices": result.deleted_offline_devices,
                "auth_key_created": result.auth_key_created,
                "tailscale_online": result.tailscale_online,
                "dry_run": dry_run,
            },
            ensure_ascii=False,
        )
    )


@app.command("offers")
def cmd_offers(
    target: Optional[str] = typer.Argument(None, help="Target template name under templates/ (e.g. ollama, comfyui). Auto-selected if only one template exists."),
    limit: int = typer.Option(10, "--limit"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Show Vast.ai offers that match the target's search filter."""
    _setup_logging(verbose)
    if target is None:
        available = sorted(p.name for p in TEMPLATE_DIR.iterdir() if p.is_dir() and not p.name.startswith("_"))
        if len(available) == 1:
            target = available[0]
        elif available:
            _die(f"TARGET argument is required. Available targets: {', '.join(available)}")
        else:
            _die("TARGET argument is required (no templates found)")
    settings = Settings()
    profile = TargetProfile.load(target)
    try:
        with VastClient(settings.vast_api_key) as vast:
            offers = vast.search_offers(profile.search.to_vast_query_string(), limit=limit)
    except VastError as e:
        _die(str(e))
    t = Table(title=f"Vast.ai offers for {target}")
    for col in ("id", "gpu_name", "num_gpus", "gpu_ram_gb", "cpu_cores", "dph_total", "geolocation"):
        t.add_column(col)
    for o in offers:
        t.add_row(
            str(o.get("id", "")),
            str(o.get("gpu_name", "")),
            str(o.get("num_gpus", "")),
            f"{(o.get('gpu_ram') or 0) / 1024:.0f}",
            str(o.get("cpu_cores", "")),
            f"{o.get('dph_total', 0):.4f}",
            str(o.get("geolocation", "")),
        )
    console.print(t)


@app.command("instances")
def cmd_instances(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """List my Vast.ai instances."""
    _setup_logging(verbose)
    settings = Settings()
    try:
        with VastClient(settings.vast_api_key) as vast:
            instances = vast.list_instances()
    except VastError as e:
        _die(str(e))
    t = Table(title="Vast.ai instances")
    for col in ("id", "label", "status", "gpu_name", "image", "dph_total"):
        t.add_column(col)
    for i in instances:
        t.add_row(
            str(i.get("id", "")),
            str(i.get("label", "")),
            str(i.get("actual_status") or i.get("intended_status") or ""),
            str(i.get("gpu_name", "")),
            str(i.get("image_uuid") or i.get("image", "")),
            f"{i.get('dph_total', 0):.4f}",
        )
    console.print(t)


@app.command("logs")
def cmd_logs(
    target: str = typer.Argument(..., help="Vast.ai instance id or label (e.g. 'ollama')"),
    follow: bool = typer.Option(False, "-f", "--follow", help="Stream new log lines (poll-based)."),
    tail: int = typer.Option(100, "--tail", "-n", help="Number of trailing lines to show."),
    daemon: bool = typer.Option(False, "--daemon", help="Show Vast.ai container daemon logs instead of the workload."),
    interval: float = typer.Option(3.0, "--interval", help="Follow polling interval in seconds."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Show or follow logs from a Vast.ai instance.

    `target` can be a numeric instance id or a label (defaults to the target
    name like `ollama` / `comfyui`).
    """
    import time as _time

    _setup_logging(verbose)
    settings = Settings()
    try:
        with VastClient(settings.vast_api_key) as vast:
            instance_id = _resolve_instance_id(vast, target)
            if not follow:
                text = vast.get_logs(instance_id, tail=tail, daemon=daemon)
                if text:
                    console.print(text, end="")
                    if not text.endswith("\n"):
                        console.print()
                else:
                    console.print("[dim](no logs yet)[/dim]")
                return

            # Follow mode: track what we've already shown by suffix-matching
            # against the previous fetch. The Vast.ai logs endpoint returns
            # the most recent N lines, not a strict stream, so we must dedupe.
            last_seen = ""
            while True:
                text = vast.get_logs(instance_id, tail=tail, daemon=daemon)
                if text:
                    if last_seen and text.startswith(last_seen):
                        new_part = text[len(last_seen):]
                    elif last_seen and last_seen in text:
                        # Older lines rolled off; resume from the last anchor.
                        idx = text.rfind(last_seen) + len(last_seen)
                        new_part = text[idx:]
                    else:
                        new_part = text
                    if new_part:
                        console.print(new_part, end="")
                    last_seen = text[-2000:]  # keep a sliding anchor of recent text
                _time.sleep(interval)
    except KeyboardInterrupt:
        return
    except VastError as e:
        _die(str(e))


@app.command("status")
def cmd_status(
    target: Optional[str] = typer.Argument(None, help="Instance id or label; omit to list all."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Show status of one or all instances."""
    _setup_logging(verbose)
    settings = Settings()
    try:
        with VastClient(settings.vast_api_key) as vast:
            if target is None:
                instances = vast.list_instances()
            else:
                instance_id = _resolve_instance_id(vast, target)
                single = vast.show_instance(instance_id)
                if not single:
                    console.print(f"instance {instance_id}: not found (likely destroyed)")
                    return
                instances = [single]
    except VastError as e:
        _die(str(e))
    t = Table(title="Vast.ai instances")
    for col in ("id", "label", "intended", "actual", "cur_state", "gpu_name", "geo", "dph_total", "machine_id", "host_id"):
        t.add_column(col)
    for i in instances:
        if not i:
            continue
        t.add_row(
            str(i.get("id", "")),
            str(i.get("label", "")),
            str(i.get("intended_status") or ""),
            str(i.get("actual_status") or ""),
            str(i.get("cur_state") or ""),
            str(i.get("gpu_name", "")),
            str(i.get("geolocation", "")),
            f"{i.get('dph_total', 0):.4f}",
            str(i.get("machine_id", "")),
            str(i.get("host_id", "")),
        )
    console.print(t)


@app.command("stop")
def cmd_stop(
    target: str = typer.Argument(..., help="Vast.ai instance id or label"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Stop (pause) an instance. Storage billing continues until destroy."""
    _setup_logging(verbose)
    settings = Settings()
    try:
        with VastClient(settings.vast_api_key) as vast:
            instance_id = _resolve_instance_id(vast, target)
            resp = vast.stop_instance(instance_id)
    except VastError as e:
        _die(str(e))
    console.print({
        "stopped_request_for": instance_id,
        "api_response": resp,
        "note": "actual state may take ~10-60s to flip; run `status` again. storage still billed.",
    })


@app.command("start")
def cmd_start(
    target: str = typer.Argument(..., help="Vast.ai instance id or label"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Resume a stopped instance."""
    _setup_logging(verbose)
    settings = Settings()
    try:
        with VastClient(settings.vast_api_key) as vast:
            instance_id = _resolve_instance_id(vast, target)
            vast.start_instance(instance_id)
    except VastError as e:
        _die(str(e))
    console.print(f"started instance {instance_id}")


@app.command("destroy")
def cmd_destroy(
    target: str = typer.Argument(..., help="Vast.ai instance id or label"),
    deny: bool = typer.Option(
        False,
        "--denylist",
        help="Also add this instance's machine to the denylist so future searches skip it.",
    ),
    note: str = typer.Option("", "--note", help="Note to attach to the denylist entry."),
    wait_timeout: float = typer.Option(
        120.0, "--wait-timeout",
        help="Timeout (s) to wait for the Vast.ai instance to actually disappear.",
    ),
    skip_tailscale: bool = typer.Option(
        False, "--skip-tailscale",
        help="Skip Tailscale device deletion. Use if the label/hostname does not match a Tailnet device.",
    ),
    yes: bool = typer.Option(
        False, "-y", "--yes",
        help="Skip the y/N confirmation prompt.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Destroy an instance, wait for confirmation, then clean up its Tailscale device.

    Steps: (1) record label/machine_id; (2) destroy on Vast.ai; (3) poll until
    the instance is gone; (4) delete every Tailscale device with the matching
    hostname (online or offline).
    """
    from . import denylist as dl

    _setup_logging(verbose)
    settings = Settings()

    try:
        with VastClient(settings.vast_api_key) as vast:
            instance_id = _resolve_instance_id(vast, target)
            msg: dict = {"destroyed": instance_id}
            # 1. Capture label / machine_id BEFORE destroy.
            info = vast.show_instance(instance_id)
            label = (info.get("label") or "").strip() if info else ""
            msg["label"] = label
            if deny:
                msg["denylisted"] = {
                    "machine_id": info.get("machine_id"),
                    "host_id": info.get("host_id"),
                }

            # 1.5 Confirm with the user (unless -y was given).
            if not yes:
                summary = (
                    f"  id={instance_id}  label={label!r}\n"
                    f"  gpu={info.get('gpu_name')!r}  geo={info.get('geolocation')!r}  "
                    f"dph={info.get('dph_total', 0):.4f}"
                )
                tail_actions = []
                tail_actions.append("destroy Vast.ai instance")
                if not skip_tailscale and label:
                    tail_actions.append(f"delete Tailscale device(s) named {label!r}")
                if deny:
                    tail_actions.append("add machine to denylist")
                console.print(
                    f"[yellow]About to:[/yellow] {', '.join(tail_actions)}\n{summary}"
                )
                if not typer.confirm("proceed?", default=False):
                    console.print("[dim]aborted by user[/dim]")
                    raise typer.Exit(code=1)

            # 2. Destroy. Returns False if already gone (404).
            existed = vast.destroy_instance(instance_id)
            msg["already_gone"] = not existed

            # 3. Wait for Vast.ai to confirm disappearance.
            if existed:
                try:
                    vast.wait_for_destroyed(instance_id, timeout=wait_timeout)
                    msg["vast_confirmed"] = True
                except VastError as e:
                    msg["vast_confirmed"] = False
                    msg["vast_wait_error"] = str(e)
            else:
                msg["vast_confirmed"] = True
    except VastError as e:
        _die(str(e))

    # 4. Tailscale cleanup.
    if skip_tailscale or not label:
        msg["tailscale_deleted"] = []
        if not label and not skip_tailscale:
            msg["tailscale_skipped_reason"] = "no label on instance"
    else:
        try:
            with TailscaleClient(settings.tailscale_api_key, settings.tailscale_tailnet) as ts:
                deleted = ts.delete_by_hostname(label)
            msg["tailscale_deleted"] = deleted
        except Exception as e:
            msg["tailscale_deleted"] = []
            msg["tailscale_error"] = str(e)

    # 5. Denylist (if requested).
    if deny:
        dl.add(
            machine_id=info.get("machine_id"),
            host_id=info.get("host_id"),
            note=note or f"destroyed via CLI ({instance_id})",
        )

    console.print(msg)


@app.command("denylist")
def cmd_denylist(
    action: str = typer.Argument("show", help="show | add | remove"),
    machine_id: Optional[int] = typer.Option(None, "--machine-id"),
    host_id: Optional[int] = typer.Option(None, "--host-id"),
    note: str = typer.Option("", "--note"),
) -> None:
    """Inspect / edit the denylist (machine_ids excluded from future searches)."""
    from . import denylist as dl

    if action == "show":
        console.print_json(json.dumps(dl.load(), ensure_ascii=False))
    elif action == "add":
        if machine_id is None and host_id is None:
            raise typer.BadParameter("--machine-id or --host-id required")
        dl.add(machine_id=machine_id, host_id=host_id, note=note)
        console.print({"added": {"machine_id": machine_id, "host_id": host_id}})
    elif action == "remove":
        dl.remove(machine_id=machine_id, host_id=host_id)
        console.print({"removed": {"machine_id": machine_id, "host_id": host_id}})
    else:
        raise typer.BadParameter(f"unknown action: {action}")


@app.command("ts-find")
def cmd_ts_find(
    hostname: str = typer.Argument(..., help="Hostname to look up"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Print every Tailscale device whose label contains the given hostname.

    Useful when `wait` keeps polling — exposes the actual values of `name`
    / `hostname` / `givenName` / `displayName` / `online` for devices that
    look related to the target.
    """
    _setup_logging(verbose)
    settings = Settings()
    target = hostname.lower()
    with TailscaleClient(settings.tailscale_api_key, settings.tailscale_tailnet) as ts:
        devices = ts.list_devices()
    rows = []
    for d in devices:
        labels = ts._candidate_labels(d)
        if any(target in lbl for lbl in labels):
            rows.append({
                "id": d.get("nodeId") or d.get("id"),
                "online": d.get("online"),
                "hostname": d.get("hostname"),
                "name": d.get("name"),
                "givenName": d.get("givenName"),
                "lastSeen": d.get("lastSeen"),
            })
    console.print_json(json.dumps({"target": hostname, "matches": rows}, ensure_ascii=False))


@app.command("ts-purge")
def cmd_ts_purge(
    name: str = typer.Argument(..., help="Hostname to purge offline duplicates of"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Delete all Offline Tailscale devices that share this hostname."""
    _setup_logging(verbose)
    settings = Settings()
    with TailscaleClient(settings.tailscale_api_key, settings.tailscale_tailnet) as ts:
        deleted = ts.purge_offline(name)
    console.print({"deleted": deleted})


@app.command("serve")
def cmd_serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
) -> None:
    """Run the FastAPI HTTP wrapper (POST /deploy)."""
    import uvicorn

    uvicorn.run("app.api:api", host=host, port=port, reload=False)


def main() -> None:  # entrypoint for `python -m app`
    app()


if __name__ == "__main__":
    sys.exit(main() or 0)
