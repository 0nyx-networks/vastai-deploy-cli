from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "templates"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    vast_api_key: str = Field(default="", alias="VAST_API_KEY")
    tailscale_api_key: str = Field(default="", alias="TAILSCALE_API_KEY")
    tailscale_tailnet: str = Field(default="-", alias="TAILSCALE_TAILNET")
    tailscale_tags: str = Field(default="tag:vast", alias="TAILSCALE_TAGS")
    ssh_public_key: str = Field(default="", alias="SSH_PUBLIC_KEY")

    @property
    def tailscale_tag_list(self) -> list[str]:
        return [t.strip() for t in self.tailscale_tags.split(",") if t.strip()]


class SearchFilter(BaseModel):
    """Subset of Vast.ai search-offers query fields we care about.

    Vast.ai uses a JSON object of comparison operators per field, e.g.
    {"gpu_ram": {"gte": 24}, "dph_total": {"lte": 0.5}}.
    """

    # Vast.ai gpu_name strings, e.g. "RTX 5090", "RTX 4090", "H100 PCIe".
    # Matches any of the listed names.
    gpu_name: list[str] = Field(default_factory=list)
    num_gpus: int = 1
    gpu_ram_gb_min: int | None = None
    disk_gb_min: int = 32
    cuda_min: float | None = None  # minimum required CUDA; mapped to Vast.ai's cuda_max_good (host max) with gte
    rentable: bool = True
    verified: bool = True
    max_dph: float | None = None
    order: str = "dph_total"  # asc by default
    # ISO-3166 alpha-2 country codes in priority order. The first listed
    # country is preferred; later ones are fallbacks. e.g. ["JP", "US"].
    geolocation: list[str] = Field(default_factory=list)

    def to_vast_query_string(self) -> str:
        """Build a query string for vastai SDK's `search_offers(query=...)`.

        Kept intentionally minimal — only the most differentiating filters
        (GPU model + region + count) go to the API. The SDK applies its own
        defaults (verified, rentable, rented=false, external=false). All other
        constraints (cuda_min, gpu_ram_gb_min, max_dph) are enforced
        client-side in `filter_offers` so SDK query-parsing quirks cannot
        cause silent under-matches.

        Format: whitespace-separated tokens. GPU names with spaces use `_`.
        """
        parts: list[str] = [f"num_gpus={self.num_gpus}"]
        if self.gpu_name:
            names = ",".join(n.replace(" ", "_") for n in self.gpu_name)
            parts.append(f"gpu_name in [{names}]")
        if self.geolocation:
            parts.append(f"geolocation in [{','.join(self.geolocation)}]")
        return " ".join(parts)


class TargetProfile(BaseModel):
    name: str
    image: str
    disk_gb: int = 32
    onstart_script: str  # path relative to template dir
    env: dict[str, Annotated[str, BeforeValidator(str)]] = Field(default_factory=dict)
    runtype: Literal["ssh", "ssh_proxy", "args", "jupyter"] = "ssh"
    search: SearchFilter = Field(default_factory=SearchFilter)
    exposed_ports: list[int] = Field(default_factory=list)

    @classmethod
    def load(cls, target: str) -> "TargetProfile":
        path = TEMPLATE_DIR / target / "profile.json"
        if not path.exists():
            raise FileNotFoundError(f"profile not found: {path}")
        data = json.loads(path.read_text())
        return cls.model_validate(data)

    def onstart_text(self) -> str:
        path = TEMPLATE_DIR / self.onstart_script
        return path.read_text()
