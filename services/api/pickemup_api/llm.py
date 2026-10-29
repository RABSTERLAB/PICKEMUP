"""Optional LLM layer — OpenAI-compatible chat-completions client.

Connection is OPTIONAL and provider-agnostic by design: any endpoint that
speaks `POST {base_url}/chat/completions` works (OpenAI, xAI, Anthropic via
litellm, Ollama, LM Studio, vLLM, llama.cpp server, Tailscale-served local
models...). Configuration comes from env (deployment default) and can be
overridden per-installation via the Settings page (persisted in KV).

When nothing is configured the product does NOT degrade to "no analysis": the
deterministic engines still run and `reporting.py` writes structured findings.
This module only adds the *narrative* + *reasoning* layer on top.

Security posture (local-first OSINT tool):
- base_url is allow-validated: https, or http ONLY to loopback/private hosts,
  so a stray config can never exfiltrate findings to a random clear-text host.
- api keys are write-only through the API surface (never echoed back).
- No prompt ever contains raw credentials; only evidence excerpts from the DB.
"""
from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import KV

KV_KEY = "llm_config"
DEFAULT_TIMEOUT_S = 120.0


@dataclass
class LLMConfig:
    enabled: bool = False
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    temperature: float = 0.2
    max_tokens: int = 1600
    timeout_s: float = DEFAULT_TIMEOUT_S
    source: str = "none"          # env | settings | none

    @property
    def usable(self) -> bool:
        return bool(self.enabled and self.base_url.strip() and self.model.strip())


def _private_or_loopback(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        return host in {"localhost", "host.docker.internal"} or host.endswith(".local")


def validate_base_url(url: str) -> str | None:
    """Return an error string if the endpoint is not safe to store, else None."""
    u = url.strip()
    if not u:
        return "base_url is empty"
    m = re.match(r"^(https?)://(\[[0-9A-Fa-f:.]+\]|[^/:]+)(:\d+)?(/.*)?$", u)
    if not m:
        return "base_url must look like https://host/v1 (OpenAI-compatible root)"
    scheme, host = m.group(1), m.group(2).strip("[]")  # IPv6 literals like [::1]
    hl = host.lower()
    if hl.startswith("169.254.") or hl.startswith("fe80"):
        return "link-local / cloud-metadata addresses are not allowed as LLM endpoints"
    if scheme == "http" and not _private_or_loopback(host):
        return "plain http:// is only allowed for loopback/private hosts — use https:// for remote providers"
    return None


def _from_env() -> LLMConfig:
    import os
    base = (os.getenv("PICKEMUP_LLM_BASE_URL") or "").strip()
    model = (os.getenv("PICKEMUP_LLM_MODEL") or "").strip()
    key = (os.getenv("PICKEMUP_LLM_API_KEY") or "").strip()
    raw_en = os.getenv("PICKEMUP_LLM_ENABLED")
    enabled = (raw_en is None) or raw_en.strip().lower() in ("1", "true", "yes", "on")
    if base and model:
        return LLMConfig(enabled=enabled,
                         base_url=base.rstrip("/"), model=model, api_key=key, source="env")
    return LLMConfig(source="none")


def get_config(session: Session) -> LLMConfig:
    row = session.get(KV, KV_KEY)
    if row and isinstance(row.value, dict) and row.value.get("base_url"):
        d = dict(row.value)
        return LLMConfig(
            enabled=bool(d.get("enabled")),
            base_url=str(d.get("base_url", "")).rstrip("/"),
            model=str(d.get("model", "")),
            api_key=str(d.get("api_key", "")),
            temperature=float(d.get("temperature", 0.2)),
            max_tokens=int(d.get("max_tokens", 1600)),
            timeout_s=float(d.get("timeout_s", DEFAULT_TIMEOUT_S)),
            source="settings")
    return _from_env()


def save_config(session: Session, cfg: LLMConfig) -> None:
    if cfg.base_url:
        # single choke point: EVERY persisted endpoint is allow-validated
        err = validate_base_url(cfg.base_url)
        if err:
            raise ValueError(err)
    payload = {"enabled": cfg.enabled, "base_url": cfg.base_url.rstrip("/"), "model": cfg.model,
               "api_key": cfg.api_key, "temperature": cfg.temperature,
               "max_tokens": cfg.max_tokens, "timeout_s": cfg.timeout_s}
    row = session.get(KV, KV_KEY)
    if row:
        row.value = payload
    else:
        session.add(KV(key=KV_KEY, value=payload))
    session.commit()


def public_status(session: Session) -> dict:
    """Connection state WITHOUT the secret (safe for the browser)."""
    cfg = get_config(session)
    return {"configured": bool(cfg.base_url and cfg.model), "enabled": cfg.enabled,
            "usable": cfg.usable, "base_url": cfg.base_url, "model": cfg.model,
            "has_api_key": bool(cfg.api_key), "source": cfg.source,
            "temperature": cfg.temperature, "max_tokens": cfg.max_tokens}


# ---------------------------------------------------------------- transport

class LLMError(RuntimeError):
    pass


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict = field(default_factory=dict)
    fn: Callable[..., Any] | None = None


class LLMClient:
    """Thin OpenAI-compatible client. No SDK dependency, no streaming."""

    def __init__(self, cfg: LLMConfig):
        if not cfg.usable:
            raise LLMError("LLM not configured (enable it in Settings: base_url + model required)")
        self.cfg = cfg

    def _post(self, body: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        url = f"{self.cfg.base_url}/chat/completions"
        try:
            r = httpx.post(url, json=body, headers=headers, timeout=self.cfg.timeout_s)
        except httpx.HTTPError as e:
            raise LLMError(f"cannot reach {url}: {type(e).__name__}: {e}") from e
        if r.status_code >= 400:
            raise LLMError(f"{url} -> HTTP {r.status_code}: {r.text[:160]}")
        try:
            return r.json()
        except ValueError as e:
            raise LLMError(f"non-JSON response from provider: {r.text[:200]}") from e

    def chat(self, messages: list[dict], tools: list[ToolSpec] | None = None,
             tool_choice: str = "auto") -> dict:
        """Returns {'kind':'answer','content':str} or {'kind':'tool_call','name','arguments'}."""
        body: dict[str, Any] = {"model": self.cfg.model, "messages": messages,
                                "temperature": self.cfg.temperature,
                                "max_tokens": self.cfg.max_tokens}
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description,
                "parameters": t.parameters or {"type": "object", "properties": {}}}} for t in tools]
            body["tool_choice"] = tool_choice
        data = self._post(body)
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"unexpected provider response shape: {json.dumps(data)[:300]}") from e
        calls = msg.get("tool_calls") or []
        if calls:
            fn = calls[0].get("function", {})
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except ValueError:
                args = {}
            return {"kind": "tool_call", "name": fn.get("name", ""), "arguments": args,
                    "raw_message": msg}
        return {"kind": "answer", "content": str(msg.get("content") or "")}

    def complete(self, system: str, user: str) -> str:
        resp = self.chat([{"role": "system", "content": system},
                          {"role": "user", "content": user}])
        if resp.get("kind") != "answer":  # server-side-tool providers can reply
            raise LLMError("provider returned a tool call for a plain request")  # with tool_calls anyway
        return resp.get("content") or ""


def probe(session: Session) -> dict:
    """Connectivity test used by the Settings page 'Test connection' button."""
    cfg = get_config(session)
    if not cfg.usable:
        return {"ok": False, "detail": "not configured — set base_url + model and enable"}
    try:
        reply = LLMClient(cfg).complete(
            "You are a connectivity probe. Reply with exactly the word: READY",
            "ping")
        return {"ok": True, "detail": reply.strip()[:120], "model": cfg.model,
                "base_url": cfg.base_url}
    except LLMError as e:
        return {"ok": False, "detail": str(e)[:400]}
