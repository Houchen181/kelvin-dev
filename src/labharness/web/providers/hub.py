"""Model connections through the Kelvin hub (web/hubmodels.py, hub/llm.py): Anthropic's, OpenAI's and Gemini's own
connections, with the hub as their address and the person's hub token as their key, under names of their own."""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List

from ..hubmodels import SERVICES, HubModels
from .anthropic_provider import AnthropicProvider
from .gemini_provider import GeminiProvider
from .base import SESSION
from .openai_provider import OpenAIProvider


def opencode_session() -> Dict[str, str]:
    """OpenCode routes and caches by conversation, and its Go plan refuses a request without one: a stable session
    for each chat, not the chat's own id (the hub sends one per person for a request outside any chat)."""
    chat = SESSION.get()
    return {"x-opencode-session": "kelvin-" + hashlib.sha256(chat.encode()).hexdigest()[:24]} if chat else {}


class _ThroughHub:
    """Offered only while the hub has a key for its service; asks the hub what it offers before models are listed."""
    hub_service = ""
    hub: HubModels
    client: Any

    def available(self) -> bool:
        return self.client is not None and self.hub.offers(self.hub_service)

    async def prepare(self) -> None:
        await self.hub.prepare()


class HubClaude(_ThroughHub, AnthropicProvider):
    hub_service = "anthropic"

    def __init__(self, hub: HubModels, vision: Any = None):
        name, label = SERVICES[self.hub_service]
        AnthropicProvider.__init__(self, hub.token, base_url=hub.url + "/llm/anthropic", name=name, label=label)
        self.hub = hub

    async def models(self) -> List[Any]:
        offered = self.hub.models_of(self.hub_service)
        return [m for m in await AnthropicProvider.models(self) if offered is None or m.id in offered]


class HubGPT(_ThroughHub, OpenAIProvider):
    hub_service = "openai"

    def __init__(self, hub: HubModels, vision: Any = None):
        name, label = SERVICES[self.hub_service]
        OpenAIProvider.__init__(self, name, label, hub.token, base_url=hub.url + "/llm/openai/v1", api="openai")
        self.hub = hub


class HubGemini(_ThroughHub, GeminiProvider):
    hub_service = "gemini"

    def __init__(self, hub: HubModels, vision: Any = None):
        name, label = SERVICES[self.hub_service]
        GeminiProvider.__init__(self, hub.token, base_url=hub.url + "/llm/gemini", name=name, label=label)
        self.hub = hub


class HubOpenCode(_ThroughHub, OpenAIProvider):
    hub_service = "opencode"

    def __init__(self, hub: HubModels, vision: Any = None):
        from ..services import PRESETS
        name, label = SERVICES[self.hub_service]
        OpenAIProvider.__init__(self, name, label, hub.token, base_url=hub.url + "/llm/opencode/openai/v1",
                                models_url=hub.url + "/llm/opencode/v1/models",
                                exclude=PRESETS["opencode"]["exclude"], preset="opencode", vision=vision)
        self.hub = hub

    def extra_headers(self) -> Dict[str, str]:
        return opencode_session()


class HubOpenCodeGo(_ThroughHub, OpenAIProvider):
    """OpenCode's Go subscription: its Chat Completions address serves every model it offers (OpenCode translates
    the others' formats), so none is left out."""
    hub_service = "opencode-go"

    def __init__(self, hub: HubModels, vision: Any = None):
        name, label = SERVICES[self.hub_service]
        OpenAIProvider.__init__(self, name, label, hub.token, base_url=hub.url + "/llm/opencode-go/openai/v1",
                                models_url=hub.url + "/llm/opencode-go/v1/models", preset="opencode-go",
                                vision=vision)
        self.hub = hub

    def extra_headers(self) -> Dict[str, str]:
        return opencode_session()


KINDS = (HubClaude, HubGPT, HubGemini, HubOpenCode, HubOpenCodeGo)
