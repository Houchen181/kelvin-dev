"""OpenAI's Responses API as Kelvin speaks it, to GPT with an API key (openai_provider.py) and on the ChatGPT plan
(chatgpt_provider.py).

Each request carries the whole conversation in `input` and nothing is kept on OpenAI's side (store=false). The model's
reasoning goes with the conversation, as Codex sends it: each answer's reasoning comes back encrypted (include
reasoning.encrypted_content, since nothing is stored) and is sent again with the turns after it, to the model that
wrote it only (another can't read it), so a step that follows a tool call builds on what the model thought before the
call instead of starting over. The turn's effort (Settings → Models → Thinking) goes as the reasoning effort. Either
one a model refuses is dropped, and remembered for that model (relax)."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Set, Tuple

from .base import DOCUMENTS_NOTE, PICTURES_NOTE, data_url, safe_id, text_of, user_text

INCLUDE = ["reasoning.encrypted_content"]
EFFORTS = ("low", "medium", "high", "xhigh")   # Kelvin's efforts the API takes ("max" goes as the model's own)


def plain(item: Any) -> Dict[str, Any]:
    if isinstance(item, dict):
        return item
    dump = getattr(item, "model_dump", None)
    return dump(exclude_none=True) if dump else dict(vars(item))


def _file(doc: Dict[str, str]) -> Dict[str, Any]:
    return {"type": "input_file", "filename": doc.get("name") or "document.pdf", "file_data": data_url(doc)}


def to_input(history: List[Dict[str, Any]], provider: str, model: Optional[str] = None,
             documents: bool = False) -> List[Dict[str, Any]]:
    """model: the model answering now; reasoning goes back only to the model that wrote it (for_model: the model
    Kelvin asked, as a service names its answering model as it likes, a dated name say). documents: PDFs go whole,
    for a model that reads them (they come with the history only for such a model: agent._pictures)."""
    out: List[Dict[str, Any]] = []
    for m in history:
        if m["role"] == "user":
            images = m.get("images") or []
            docs = (m.get("documents") or []) if documents else []
            out.append({"role": "user", "content": [{"type": "input_text", "text": user_text(m)}] + [
                {"type": "input_image", "image_url": data_url(i)} for i in images] + [_file(d) for d in docs]
                if images or docs else user_text(m)})
        elif m["role"] == "assistant":
            if m.get("provider") == provider and isinstance(m.get("raw"), list):
                for item in m["raw"]:   # our own earlier output, replayed as it was (without server ids)
                    if item.get("type") == "reasoning" and model and item.get("for_model") not in (None, model):
                        continue   # another model's reasoning
                    out.append({k: v for k, v in item.items() if k not in ("id", "status", "for_model")})
                continue
            text = text_of(m["blocks"])
            if text:
                out.append({"role": "assistant", "content": text})
            n = 0
            for b in m["blocks"]:
                if b["type"] == "tool_call":
                    n += 1
                    out.append({"type": "function_call", "call_id": safe_id(b["id"], n), "name": b["name"],
                                "arguments": json.dumps(b["input"] or {})})
        elif m["role"] == "tool":
            for r in m["results"]:
                out.append({"type": "function_call_output", "call_id": safe_id(r["id"], 0),
                            "output": r["content"] or "(empty)"})
            images = [i for r in m["results"] for i in r.get("images") or []]
            docs = [d for r in m["results"] for d in r.get("documents") or []] if documents else []
            parts: List[Dict[str, Any]] = []
            if images:   # the pictures and PDFs follow as the user's
                parts += [{"type": "input_text", "text": PICTURES_NOTE}] + [
                    {"type": "input_image", "image_url": data_url(i)} for i in images]
            if docs:
                parts += [{"type": "input_text", "text": DOCUMENTS_NOTE}] + [_file(d) for d in docs]
            if parts:
                out.append({"role": "user", "content": parts})
    return out


def read(output: Any, model: str, namespace: str = "") -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], bool]:
    """An answer's output items: Kelvin's blocks, what goes back with the turns after it (raw: its words, its calls,
    and the reasoning that led to them, marked with the model asked), and whether the model declined. namespace: the
    prefix a tool's name came with (the ChatGPT plan's)."""
    blocks: List[Dict[str, Any]] = []
    raw: List[Dict[str, Any]] = []
    declined = False
    for item in output or []:
        item = plain(item)
        kind = item.get("type")
        if kind == "message":
            parts = [plain(c) for c in item.get("content") or []]
            refusal = "".join(c.get("refusal") or "" for c in parts if c.get("type") == "refusal")
            text = "".join(c.get("text", "") for c in parts if c.get("type") == "output_text") or refusal
            declined = declined or bool(refusal)
            if text:
                blocks.append({"type": "text", "text": text})
                raw.append({"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": text}]})
        elif kind == "function_call":
            name = str(item.get("name") or "")
            if namespace and name.startswith(namespace + "."):
                name = name[len(namespace) + 1:]
            try:
                args = json.loads(item.get("arguments") or "{}")
                args = args if isinstance(args, dict) else None
            except ValueError:
                args = None
            blocks.append({"type": "tool_call", "id": item.get("call_id") or item.get("id"), "name": name,
                           "input": args})
            raw.append({k: v for k, v in item.items() if k in ("type", "call_id", "name", "arguments", "namespace")})
        elif kind == "reasoning" and item.get("encrypted_content"):   # sent again with the turns after it
            raw.append({"type": "reasoning", "summary": item.get("summary") or [],
                        "encrypted_content": item["encrypted_content"], "for_model": model})
    while raw and raw[-1]["type"] == "reasoning":   # reasoning goes back only with the item it led to
        raw.pop()
    return blocks, raw, declined


def without_calls(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A cut or declined answer as it is sent again: no call that never ran (a call without its output is refused),
    nor the reasoning that led to it."""
    return [i for i in raw if i["type"] not in ("function_call", "reasoning")]


def relax(body: Dict[str, Any], e: Exception, model: str, no_effort: Set[str], no_include: Set[str]) -> bool:
    """A request refused for its reasoning: the body without what was refused (True), remembered for a model that
    never takes it; False when the refusal was for something else.
    - earlier reasoning it can't read now (another sign-in or key): this request without it; the turns after it
      carry their own;
    - an effort it doesn't take: high instead of xhigh, for this request; else none, for this model;
    - no encrypted reasoning for this model: never asked for again."""
    said = str(e).lower()
    param = str(getattr(e, "param", None) or "").lower()
    sent_back = any(i.get("type") == "reasoning" for i in body["input"])
    if sent_back and (param.startswith("input") or "encrypted" in said and not param.startswith("include")):
        body["input"] = [i for i in body["input"] if i.get("type") != "reasoning"]
    elif "reasoning" in body and ("effort" in param or "effort" in said):
        if body["reasoning"].get("effort") == "xhigh":
            body["reasoning"] = {"effort": "high"}   # the deepest this model takes, for this request
        else:
            no_effort.add(model)   # this model takes no effort at all
            del body["reasoning"]
    elif "include" in body and ("include" in param or "include" in said or "encrypted" in said):
        no_include.add(model)   # the service gives this model's reasoning back to nobody
        del body["include"]
    else:
        return False
    return True
