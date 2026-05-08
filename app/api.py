from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .deploy import DeployRequest, deploy
from .vast import VastClient, VastError
from .config import Settings

api = FastAPI(title="deploy-vast-ai", version="0.1.0")


class DeployBody(BaseModel):
    target: str = Field(..., examples=["ollama", "comfyui"])
    name: str | None = None
    offer_id: int | None = None
    dry_run: bool = False
    wait_for_tailscale: bool = True
    wait_timeout: float = 300.0


@api.post("/deploy")
def post_deploy(body: DeployBody) -> dict:
    try:
        result = deploy(DeployRequest(**body.model_dump()))
    except (VastError, FileNotFoundError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "target": result.target,
        "hostname": result.hostname,
        "offer_id": result.offer_id,
        "instance_id": result.instance_id,
        "deleted_offline_devices": result.deleted_offline_devices,
        "auth_key_created": result.auth_key_created,
        "tailscale_online": result.tailscale_online,
    }


@api.get("/instances")
def get_instances() -> dict:
    settings = Settings()
    with VastClient(settings.vast_api_key) as v:
        return {"instances": v.list_instances()}


@api.get("/healthz")
def healthz() -> dict:
    return {"ok": True}
