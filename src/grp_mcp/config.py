"""Instance configuration loading.

Two sources, in priority order:
  1. connections.json  (named profiles, supports many instances)
  2. environment vars   (a single instance named "default")

A connections file path can be given via GRP_MCP_CONNECTIONS; otherwise a
connections.json next to the project root is used if present.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()


class Instance(BaseModel):
    """Connection details for one Acumatica tenant."""

    base_url: str = Field(..., description="Root URL, e.g. https://host/Site")
    client_id: str
    client_secret: str
    username: str
    password: str
    endpoint_name: str = "Default"
    endpoint_version: str = "24.200.001"
    tenant: str = ""  # company login name, needed for OData / GI calls
    branch: str = ""  # optional login branch
    allow_publish: bool = False  # gate for Customization API write ops (publish/import/unpublish)

    @property
    def token_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/identity/connect/token"

    @property
    def entity_base(self) -> str:
        return (
            f"{self.base_url.rstrip('/')}/entity/"
            f"{self.endpoint_name}/{self.endpoint_version}"
        )

    @property
    def odata_base(self) -> str:
        root = self.base_url.rstrip("/")
        return f"{root}/odata/{self.tenant}" if self.tenant else f"{root}/odata"

    @property
    def dac_odata_base(self) -> str:
        """Base URL for the DAC-based OData v4 interface (<base>/t/<Tenant>/api/odata/dac)."""
        root = self.base_url.rstrip("/")
        return f"{root}/t/{self.tenant}/api/odata/dac" if self.tenant else f"{root}/api/odata/dac"


class Config(BaseModel):
    default: str
    instances: dict[str, Instance]

    def get(self, name: str | None) -> Instance:
        key = name or self.default
        if key not in self.instances:
            raise KeyError(
                f"Unknown instance '{key}'. Configured: {', '.join(self.instances)}"
            )
        return self.instances[key]


def _from_env() -> Config | None:
    base = os.getenv("GRP_MCP_BASE_URL")
    if not base:
        return None
    inst = Instance(
        base_url=base,
        client_id=os.environ["GRP_MCP_CLIENT_ID"],
        client_secret=os.environ["GRP_MCP_CLIENT_SECRET"],
        username=os.environ["GRP_MCP_USERNAME"],
        password=os.environ["GRP_MCP_PASSWORD"],
        endpoint_name=os.getenv("GRP_MCP_ENDPOINT_NAME", "Default"),
        endpoint_version=os.getenv("GRP_MCP_ENDPOINT_VERSION", "24.200.001"),
        tenant=os.getenv("GRP_MCP_TENANT", ""),
        branch=os.getenv("GRP_MCP_BRANCH", ""),
        allow_publish=os.getenv("GRP_MCP_ALLOW_PUBLISH", "").lower() in ("1", "true", "yes"),
    )
    return Config(default="default", instances={"default": inst})


def _from_file(path: Path) -> Config:
    data = json.loads(path.read_text(encoding="utf-8"))
    instances = {k: Instance(**v) for k, v in data["instances"].items()}
    default = data.get("default") or next(iter(instances))
    return Config(default=default, instances=instances)


def load_config() -> Config:
    explicit = os.getenv("GRP_MCP_CONNECTIONS")
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(Path.cwd() / "connections.json")
    candidates.append(Path(__file__).resolve().parents[2] / "connections.json")

    for path in candidates:
        if path.is_file():
            return _from_file(path)

    env_cfg = _from_env()
    if env_cfg:
        return env_cfg

    raise RuntimeError(
        "No configuration found. Set GRP_MCP_* env vars (see .env.example) "
        "or create connections.json (see connections.example.json)."
    )
