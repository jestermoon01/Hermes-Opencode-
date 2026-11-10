"""Load gateway settings; keep all service listeners and upstreams on loopback."""

from pathlib import Path
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


def loopback_url(value: str) -> str:
    url = urlsplit(value)
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Only loopback HTTP backend URLs are allowed")
    if url.username or url.password or url.query or url.fragment:
        raise ValueError("Backend URLs must not contain credentials, queries, or fragments")
    return value.rstrip("/")


class Backend(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    priority: int = 1
    base_url: str
    health_url: str
    api_key_env: str | None = None
    models: dict[str, str]
    context_limit: int = Field(ge=1)
    enabled: bool = True

    _validate_urls = field_validator("base_url", "health_url")(loopback_url)


class Launcher(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_key: str = "twil-8b"
    model_identifier: str = "hermes-local"
    context_length: int = 65536


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1024, le=65535)
    model: str = "hermes-local"
    log_file: Path = Path(".runtime/gateway-usage.jsonl")
    connect_timeout: float = Field(default=5, gt=0)
    read_timeout: float = Field(default=180, gt=0)
    backends: list[Backend]
    launcher: Launcher = Field(default_factory=Launcher)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if value != "127.0.0.1":
            raise ValueError("Gateway must bind to 127.0.0.1")
        return value


def load_settings(path: Path) -> Settings:
    settings = Settings.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    if not settings.log_file.is_absolute():
        settings.log_file = path.resolve().parent / settings.log_file
    names = [backend.name for backend in settings.backends]
    if len(names) != len(set(names)):
        raise ValueError("Backend names must be unique")
    return settings
