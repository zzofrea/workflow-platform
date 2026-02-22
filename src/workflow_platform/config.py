"""Configuration for the workflow platform."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class PlatformConfig(BaseSettings):
    """Platform-wide config from env vars."""

    model_config = {"case_sensitive": False, "extra": "ignore"}

    dokploy_url: str = "http://localhost:3000"
    dokploy_api_key: str = ""
    dokploy_project_id: str = ""
    dokploy_prod_env_id: str = ""

    # Resource guard thresholds
    max_containers: int = 18
    min_free_ram_mb: int = 3072  # 3 GB
