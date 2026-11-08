"""One switch for every AI feature in RumbleCut: local models on this PC, or cloud.

  Local  - a model server running on your own computer:
             * Ollama            (default, http://localhost:11434)
             * OpenAI-compatible (LM Studio, llama.cpp server, vLLM, Jan…)
           Private and free; speed depends on your GPU.
  Cloud  - Claude via the Anthropic API (needs an API key from console.anthropic.com).

Speech-to-text always runs locally with Whisper (see transcribe.py).

Used today for AI clip titles & captions; the upcoming Context Images feature uses the
same `complete()` call to work out what's being talked about.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request

from . import config

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_CLOUD_MODEL = "claude-haiku-5-5"


class AIError(RuntimeError):
    pass


# ----------------------------------------------------------------------------- API key storage
def _protect(text: str) -> str:
    """Encrypt with Windows DPAPI (only this Windows user can decrypt). Fallback: base64."""
    if not text:
        return ""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class BLOB(ctypes.Structure):
                _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
            raw = text.encode("utf-8")
            inp = BLOB(len(raw), ctypes.cast(ctypes.create_string_buffer(raw, len(raw)), ctypes.POINTER(ctypes.c_char)))
            out = BLOB()
            if ctypes.windll.crypt32.CryptProtectData(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
                data = ctypes.string_at(out.pbData, out.cbData)
                ctypes.windll.kernel32.LocalFree(out.pbData)
                return "dpapi:" + base64.b64encode(data).decode()
        except Exception:
            pass
    return "b64:" + base64.b64encode(text.encode()).decode()


def _unprotect(stored: str) -> str:
    if not stored:
        return ""
    kind, _, payload = stored.partition(":")
    data = base64.b64decode(payload)
    if kind == "dpapi" and os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
        inp = BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
        out = BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
            return ""
        txt = ctypes.string_at(out.pbData, out.cbData).decode("utf-8")
        ctypes.windll.kernel32.LocalFree(out.pbData)
        return txt
    if kind == "b64":
        return data.decode()
    return ""


def set_api_key(key: str) -> None:
    config.save_settings({"cloud_api_key": _protect(key.strip())})


def get_api_key() -> str:
    try:
        return _unprotect(config.load_settings().get("cloud_api_key", ""))
    except Exception:
        return ""


# ----------------------------------------------------------------------------- HTTP helper
def _post(url: str, body: dict, headers: dict | None = None, timeout: float = 180) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise AIError(f"The AI server answered {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise AIError(f"Couldn't reach the AI server at {url.split('/v1')[0].split('/api')[0]} "
                      f"({e.reason}).") from e


def _get(url: str, timeout: float = 5) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
        return json.load(r)


# ----------------------------------------------------------------------------- settings view
def settings() -> dict:
    s = config.load_settings()
    return {
        "mode": s.get("ai_mode", "local"),
        "local_kind": s.get("local_kind", "ollama"),
        "local_url": (s.get("local_url") or ("http://localhost:11434" if s.get("local_kind", "ollama") == "ollama"
                                             else "http://localhost:1234")).rstrip("/"),
        "local_model": s.get("local_model", ""),
        "cloud_model": s.get("cloud_model") or DEFAULT_CLOUD_MODEL,
    }


def describe() -> str:
    c = settings()
    if c["mode"] == "cloud":
        return f"Cloud · Claude ({c['cloud_model']})"
    return f"This PC · {c['local_model'] or 'no model chosen'} ({'Ollama' if c['local_kind'] == 'ollama' else 'local server'})"


# ----------------------------------------------------------------------------- local models
def list_local_models(kind: str | None = None, url: str | None = None) -> list[str]:
    c = settings()
    kind, url = kind or c["local_kind"], (url or c["local_url"]).rstrip("/")
    try:
        if kind == "ollama":
            return sorted(m["name"] for m in _get(f"{url}/api/tags").get("models", []))
        return sorted(m["id"] for m in _get(f"{url}/v1/models").get("data", []))
    except Exception as e:
        name = "Ollama" if kind == "ollama" else "the local model server"
        raise AIError(f"{name} isn't running at {url}. "
                      + ("Install it from ollama.com, then run e.g. 'ollama pull llama3.1'." if kind == "ollama"
                         else "Start the server in LM Studio (Developer tab) or your tool of choice.")) from e


# ----------------------------------------------------------------------------- one call for all features
def complete(prompt: str, system: str = "", json_mode: bool = False, max_tokens: int = 400) -> str:
    c = settings()
    if c["mode"] == "cloud":
        key = get_api_key()
        if not key:
            raise AIError("Add your Anthropic API key in Settings → AI models, or switch to local models.")
        body = {"model": c["cloud_model"], "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}]}
        if system:
            body["system"] = system
        r = _post(ANTHROPIC_URL, body, {"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout=120)
        return "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text").strip()

    if not c["local_model"]:
        raise AIError("Choose a local model in Settings → AI models.")
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    if c["local_kind"] == "ollama":
        body = {"model": c["local_model"], "messages": msgs, "stream": False,
                "options": {"num_predict": max_tokens, "temperature": 0.7}}
        if json_mode:
            body["format"] = "json"
        r = _post(f"{c['local_url']}/api/chat", body)
        return (r.get("message") or {}).get("content", "").strip()
    body = {"model": c["local_model"], "messages": msgs, "max_tokens": max_tokens, "temperature": 0.7}
    r = _post(f"{c['local_url']}/v1/chat/completions", body)
    return r["choices"][0]["message"]["content"].strip()


def complete_json(prompt: str, system: str = "", max_tokens: int = 400) -> dict:
    text = complete(prompt, system, json_mode=True, max_tokens=max_tokens)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)   # reasoning models
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise AIError(f"The model didn't return usable JSON: {text[:200]}")
    try:
        return json.loads(m.group(0))
    except ValueError as e:
        raise AIError(f"The model didn't return usable JSON: {text[:200]}") from e


def test() -> str:
    t = time.time()
    out = complete("Reply with exactly: RumbleCut AI is working", max_tokens=20)
    return f"✔ {describe()} replied in {time.time() - t:.1f}s: “{out[:60]}”"


# ----------------------------------------------------------------------------- features
TITLE_SYSTEM = ("You write titles and captions for short clips cut from livestreams. Titles are punchy, "
                "specific to what was said, at most 70 characters, no hashtags, no quotation marks, no emojis. "
                "Captions are one short line (at most 90 characters) that would sit on screen. "
                "Answer only with JSON: {\"title\": \"...\", \"caption\": \"...\"}")


def suggest_title(transcript: str, stream_title: str = "") -> dict:
    if not transcript.strip():
        raise AIError("No speech was found in this clip, so there's nothing to base a title on.")
    prompt = (f"Stream: {stream_title or 'unknown'}\n\nWhat is said in the clip:\n\"\"\"\n{transcript[:4000]}\n\"\"\"\n\n"
              "Write the title and caption.")
    d = complete_json(prompt, TITLE_SYSTEM, max_tokens=200)
    title = str(d.get("title", "")).strip().strip('"')[:90]
    caption = str(d.get("caption", "")).strip().strip('"')[:120]
    if not title:
        raise AIError("The model didn't suggest a title.")
    return {"title": title, "caption": caption}
