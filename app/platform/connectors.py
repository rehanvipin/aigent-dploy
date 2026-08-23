"""CMS connector interface (platform-internal).

A connector is the *enforced* half of the CMS abstraction: authentication,
firm scoping, id mapping, retries. It returns the **envelope** the platform
relies on (ref, capabilities, normalized field names); the **payloads** inside
stay free-form because the CMS changes between firms (agents read, they don't
assume). The *informative* half — what this CMS's concepts mean and how staff
actually use it — lives in the connector's co-located **skill**, not in code.

Identifiers are opaque refs: the platform stores `case_ref` / `task_ref`
strings and never assumes their shape. Capability flags let the platform pick
staff surfaces by capability (chat vs email) instead of per-CMS branches.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import settings


def _get(url: str) -> dict:
    resp = httpx.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _post(url: str, payload: dict) -> dict:
    resp = httpx.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()


class CMSConnector:
    """Normalized interface every CMS connector implements."""

    key = "base"
    capabilities: dict[str, bool] = {"chat": False, "tasks": False}

    # -- reads ---------------------------------------------------------

    def get_case(self, case_ref: str) -> dict:
        raise NotImplementedError

    def get_task(self, task_ref: str) -> dict:
        raise NotImplementedError

    # -- writes (outcomes back to the system of record) -----------------

    def write_task(self, task_ref: str, status: str | None = None, note: str | None = None) -> dict:
        raise NotImplementedError

    def post_message(self, task_ref: str, body: str, author: str = "agent") -> dict:
        raise NotImplementedError

    def create_task(self, case_ref: str, title: str, notes: str = "") -> dict:
        raise NotImplementedError


class StubCMSConnector(CMSConnector):
    """Connector for the in-process stub CMS (Filevine stand-in).

    Refs are the stub's integer ids as strings — opaque to the platform.
    """

    key = "stub_cms"
    capabilities = {"chat": True, "tasks": True}

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or settings.cms_base_url).rstrip("/")

    def get_case(self, case_ref: str) -> dict:
        return _get(f"{self.base_url}/cms/api/cases/{case_ref}")

    def get_task(self, task_ref: str) -> dict:
        return _get(f"{self.base_url}/cms/api/tasks/{task_ref}")

    def write_task(self, task_ref: str, status: str | None = None, note: str | None = None) -> dict:
        resp = httpx.patch(
            f"{self.base_url}/cms/api/tasks/{task_ref}",
            json={"status": status, "notes": note},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def post_message(self, task_ref: str, body: str, author: str = "agent") -> dict:
        return _post(
            f"{self.base_url}/cms/api/tasks/{task_ref}/messages",
            {"author": author, "body": body},
        )

    def create_task(self, case_ref: str, title: str, notes: str = "") -> dict:
        return _post(
            f"{self.base_url}/cms/api/cases/{case_ref}/tasks",
            {"title": title, "notes": notes, "status": "open"},
        )


_REGISTRY: dict[str, type[CMSConnector]] = {
    StubCMSConnector.key: StubCMSConnector,
}


def get_connector(connector_key: str, config: dict[str, Any] | None = None) -> CMSConnector:
    """Resolve a firm's connector. `config` is the firm row's config_json —
    connector-specific settings (base URL today, credential refs later)."""
    cls = _REGISTRY.get(connector_key)
    if cls is None:
        raise KeyError(f"no CMS connector registered under {connector_key!r}")
    config = config or {}
    return cls(base_url=config.get("base_url"))
