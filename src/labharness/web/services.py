"""Other model services: anything that speaks the OpenAI Chat Completions API.

OpenRouter (hundreds of models behind one key), OpenCode, DeepSeek, Groq, Mistral, xAI, or a gateway of the user's
own. Each service is its own provider (its own name in the model picker and in a project's rules), so a cloud
service is never mistaken for a local model. Kelvin needs models that can call tools."""
from __future__ import annotations

import ipaddress
import re
import urllib.parse
from typing import Any, Dict, List, Optional

BUILT_IN = {"anthropic", "openai", "gemini", "local"}
MAX_SERVICES = 12
PICTURES = ("", "on", "off")   # does the service's model see pictures: model by model (vision.py), or the person says

# id: name, base URL, where the model list is (if not base/models), which listed models to leave out, whether only
# models that report tool support are shown, the environment variable a key may come from, a hint.
PRESETS: Dict[str, Dict[str, Any]] = {
    "openrouter": {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "tools_only": True,
                   "env": "OPENROUTER_API_KEY", "headers": {"X-Title": "Kelvin"},
                   "hint": "One key for models from many companies; Kelvin lists the newest that can call tools."},
    "opencode": {"name": "OpenCode", "base_url": "https://opencode.ai/inference/openai/v1",
                 "models_url": "https://opencode.ai/inference/v1/models", "env": "OPENCODE_API_KEY",
                 "exclude": ["claude", "gpt", "o1", "o3", "o4", "gemini", "grok", "muse", "jev", "test",
                             "qwen3.8-flash", "qwen3.7", "qwen3.6-plus", "qwen3.5-plus"],   # other formats, per its docs
                 "hint": "OpenCode's Chat Completions models (Qwen, DeepSeek, Kimi, GLM, MiniMax, Mistral and its "
                         "free ones); its Claude, GPT and Gemini models use other formats."},
    "opencode-go": {"name": "OpenCode Go", "base_url": "https://opencode.ai/inference/go/openai/v1",
                    "models_url": "https://opencode.ai/inference/go/v1/models", "env": "OPENCODE_API_KEY",
                    "hint": "OpenCode's Go subscription: its own key and addresses; every model it offers."},
    "deepseek": {"name": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "env": "DEEPSEEK_API_KEY",
                 "hint": "DeepSeek's own API."},
    "groq": {"name": "Groq", "base_url": "https://api.groq.com/openai/v1", "env": "GROQ_API_KEY",
             "exclude": ["whisper", "distil-whisper", "playai", "guard"], "hint": "Fast open models."},
    "mistral": {"name": "Mistral", "base_url": "https://api.mistral.ai/v1", "env": "MISTRAL_API_KEY",
                "exclude": ["embed", "moderation", "ocr"], "hint": "Mistral's own API."},
    "xai": {"name": "xAI", "base_url": "https://api.x.ai/v1", "env": "XAI_API_KEY", "exclude": ["image", "vision"],
            "hint": "Grok models."},
    "custom": {"name": "", "base_url": "", "hint": "Any other service or gateway with an OpenAI-compatible "
                                                   "/chat/completions endpoint (e.g. Together, Fireworks, LiteLLM)."},
}


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:24]
    return s if s and s[0].isalpha() else ("svc-" + s if s else "")


def check_url(url: str) -> str:
    """https, or plain http only to this computer or the local network (an API key must not cross the internet
    unencrypted)."""
    url = str(url or "").strip().rstrip("/")
    parts = urllib.parse.urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("the address must start with https:// (e.g. https://openrouter.ai/api/v1)")
    if parts.scheme == "http":
        host = parts.hostname
        try:
            local = ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = host == "localhost" or host.endswith((".local", ".lan", ".internal"))
        if not local:
            raise ValueError("use https:// for a service on the internet; plain http only on this computer or network")
    return url


def pictures_of(value: Any) -> str:
    value = str(value or "")
    if value not in PICTURES:
        raise ValueError("pictures must be on, off or empty (model by model)")
    return value


def make(entry: Dict[str, Any], taken: List[str]) -> Dict[str, Any]:
    """A checked service entry from the form: {preset, name, base_url, api_key, models, pictures}."""
    preset = str(entry.get("preset") or "custom")
    if preset not in PRESETS:
        raise ValueError("unknown service %r" % preset)
    p = PRESETS[preset]
    name = " ".join(str(entry.get("name") or p["name"]).split())[:40]
    if not name:
        raise ValueError("give the service a name")
    sid = slug(name if preset == "custom" else preset)
    if sid in BUILT_IN or sid in taken:
        raise ValueError("there is already a service called %s; pick another name" % name)
    url = check_url(entry.get("base_url") or p["base_url"])
    models = ",".join(m.strip() for m in str(entry.get("models") or "").split(",") if m.strip())[:2000]
    key = str(entry.get("api_key") or "").strip()
    if not key and urllib.parse.urlparse(url).scheme == "https":
        raise ValueError("%s needs an API key" % name)
    out = {"id": sid, "preset": preset, "name": name, "base_url": url, "models": models, "api_key": key}
    pictures = pictures_of(entry.get("pictures"))
    return dict(out, pictures=pictures) if pictures else out


def view(entry: Dict[str, Any], key_from: Optional[str]) -> Dict[str, Any]:
    """What the browser may see: never the key."""
    return {"id": entry["id"], "preset": entry.get("preset", "custom"), "name": entry["name"],
            "base_url": entry["base_url"], "models": entry.get("models", ""), "key": key_from,
            "pictures": entry.get("pictures", "")}


def presets_view() -> List[Dict[str, Any]]:
    return [{"id": k, "name": v["name"] or "Other (OpenAI-compatible)", "base_url": v["base_url"], "hint": v["hint"]}
            for k, v in PRESETS.items()]
