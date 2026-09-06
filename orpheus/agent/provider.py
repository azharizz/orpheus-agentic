"""Explicit controller routing with sanitized attempt receipts."""

import time
from typing import Any
from urllib.parse import urlparse

from google.adk.models.base_llm import BaseLlm
from google.adk.models.google_llm import Gemini
from google.adk.models.lite_llm import LiteLlm
from pydantic import PrivateAttr

from ..config import (
    CONTROLLER_MAX_TOKENS,
    CONTROLLER_MODELS as MODELS,
    GOOGLE_CLOUD_LOCATION,
    GOOGLE_CLOUD_PROJECT,
    RUNTIME_MODE,
    VALUES,
)

PROFILE = str(
    VALUES.get("ORPHEUS_PROVIDER_PROFILE", "vertex" if RUNTIME_MODE == "cloud_run" else "openrouter")
).lower()
if PROFILE not in {"vertex", "openrouter"}:
    raise ValueError("ORPHEUS_PROVIDER_PROFILE must be vertex or openrouter")
VERTEX_MODEL = str(VALUES.get("ORPHEUS_VERTEX_MODEL", "gemini-3.0-flash"))
FAILOVER = str(VALUES.get("ORPHEUS_PROVIDER_FAILOVER", "1")).lower() in (
    "1",
    "true",
    "yes",
)


def provider_config():
    values = VALUES
    endpoint = values.get(
        "AGENT_PROVIDER_URL", "https://openrouter.ai/api/v1/chat/completions"
    ).rstrip("/")
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "openrouter.ai"
        or parsed.path not in ("/api/v1", "/api/v1/chat/completions")
    ):
        raise ValueError(
            "Only the explicitly requested OpenRouter HTTPS endpoint is allowed"
        )
    key = values.get("AGENT_PROVIDER_API_KEY")
    if not key:
        raise ValueError("AGENT_PROVIDER_API_KEY missing from Orpheus/.env")
    return (endpoint.removesuffix("/chat/completions"), key)


def _openrouter_clients():
    base, key = provider_config()
    return [
        LiteLlm(
            model="openrouter/" + name,
            api_base=base,
            api_key=key,
            timeout=180,
            num_retries=1,
            max_tokens=CONTROLLER_MAX_TOKENS,
            extra_body={
                "reasoning": {"enabled": False},
                "provider": {"require_parameters": True},
            },
        )
        for name in MODELS
    ]


def _vertex_client():
    if not GOOGLE_CLOUD_PROJECT:
        raise ValueError("GOOGLE_CLOUD_PROJECT is required for the Vertex profile")
    return Gemini(
        model=VERTEX_MODEL,
        client_kwargs={
            "vertexai": True,
            "project": GOOGLE_CLOUD_PROJECT,
            "location": GOOGLE_CLOUD_LOCATION,
        },
    )


class ControllerModel(BaseLlm):
    model: str = VERTEX_MODEL if PROFILE == "vertex" else MODELS[0]
    _clients: list = PrivateAttr(default_factory=list)
    _names: list = PrivateAttr(default_factory=list)
    _log: Any = PrivateAttr()
    _active: int = PrivateAttr(default=0)

    def __init__(self, log, clients=None):
        super().__init__()
        self._log = log
        if clients is not None:
            self._clients = clients
            self._names = [getattr(client, "model", MODELS[index]) for index, client in enumerate(clients)]
        else:
            if PROFILE == "vertex":
                self._clients = [_vertex_client()]
                self._names = [VERTEX_MODEL]
                if FAILOVER and VALUES.get("AGENT_PROVIDER_API_KEY"):
                    self._clients.extend(_openrouter_clients())
                    self._names.extend(MODELS)
            else:
                self._clients = _openrouter_clients()
                self._names = list(MODELS)

    async def generate_content_async(self, llm_request, stream=False):
        for index in range(self._active, len(self._clients)):
            started = time.monotonic()
            requested = self._names[index]
            self._log(
                "model_attempt", requested_model=requested, provider_profile=PROFILE
            )
            try:
                req = llm_request.model_copy(deep=True)
                req.model = getattr(self._clients[index], "model", requested)
                responses = [
                    r
                    async for r in self._clients[index].generate_content_async(
                        req, stream=False
                    )
                ]
                if (
                    not responses
                    or any(r.error_code for r in responses)
                    or (not any(r.content and r.content.parts for r in responses))
                ):
                    raise RuntimeError("Unusable provider response")
            except Exception as exc:
                self._log(
                    "model_failed",
                    requested_model=requested,
                    provider_profile=PROFILE,
                    error_type=type(exc).__name__,
                    status_code=getattr(exc, "status_code", None),
                    routing_diagnostic="No endpoints match requested parameters"
                    if "No endpoints" in str(exc)
                    else "See provider status code",
                    elapsed_s=round(time.monotonic() - started, 3),
                )
                continue
            self._active = index
            for response in responses:
                self._log(
                    "model_response",
                    requested_model=requested,
                    provider_profile=PROFILE,
                    served_model=response.model_version,
                    elapsed_s=round(time.monotonic() - started, 3),
                    usage=response.usage_metadata.model_dump(
                        mode="json", exclude_none=True
                    )
                    if response.usage_metadata
                    else None,
                )
                yield response
            return
        raise RuntimeError(
            "All approved vision providers failed; see sanitised model_failed events"
        )
