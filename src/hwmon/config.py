"""Runtime configuration, read once from the environment.

Kept in one place so the CLI, the sampler and the server all agree on the same
values, and so tests can construct a config explicitly instead of mutating
``os.environ``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from importlib import metadata

DEFAULT_AGENT_PORT = 9101
DEFAULT_WEB_PORT = 8080


def package_version() -> str:
    """Installed distribution version, or 'dev' when running from a checkout.

    ``HWMON_VERSION`` wins when set (CI writes ``major.minor.<commit count>``).
    """
    env = os.environ.get("HWMON_VERSION")
    if env:
        return env
    try:
        return metadata.version("hwmon")
    except metadata.PackageNotFoundError:
        return "dev"


@dataclass(frozen=True)
class Config:
    mode: str
    token: str
    version: str
    port: int
    agent_url: str
    label: str

    @property
    def is_agent(self) -> bool:
        return self.mode == "agent"

    @classmethod
    def from_env(cls, mode: str) -> "Config":
        default_port = DEFAULT_AGENT_PORT if mode == "agent" else DEFAULT_WEB_PORT
        return cls(
            mode=mode,
            token=os.environ.get("AGENT_TOKEN", ""),
            version=package_version(),
            port=int(os.environ.get("PORT", default_port)),
            agent_url=os.environ.get("AGENT_URL", "").rstrip("/"),
            label=os.environ.get("AGENT_LABEL", os.uname().nodename),
        )
