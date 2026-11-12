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


def is_configured() -> bool:
    """True when an AI model is chosen (local model picked, or a cloud key saved)."""
    c = settings()
    return bool(get_api_key()) if c["mode"] == "cloud" else bool(c["local_model"])


def describe() -> str:
    c = settings()
    if c["mode"] == "cloud":
        return f"Cloud · Claude ({c['cloud_model']})"
    return f"This PC · {c['local_model'] or 'no model chosen'} ({'Ollama' if c['local_kind'] == 'ollama' else 'local server'})"


# ----------------------------------------------------------------------------- model catalogues
# Claude models offered for cloud mode. The list is refreshed from your account (⟳) when a key is saved.
CLOUD_MODELS = [
    ("claude-haiku-5-5", "Claude Haiku 5.5", "fastest and cheapest - plenty for titles and highlights"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5", "balanced speed and quality"),
    ("claude-opus-5-5", "Claude Opus 5.5", "strongest for long, complex streams"),
    ("claude-fable-5-1", "Claude Fable 5.1", "most capable, slowest and priciest"),
]

# Well-known Ollama models you can download from inside RumbleCut (sizes are approximate downloads).
POPULAR_LOCAL = [
    ("llama3.2:3b", 2.0, "small, runs on almost anything"),
    ("qwen3:4b", 2.5, "small and sharp"),
    ("llama3.1:8b", 4.9, "solid all-rounder"),
    ("qwen3:8b", 5.2, "good all-rounder for 8 GB GPUs"),
    ("deepseek-r1:8b", 5.2, "thinks before answering, slower"),
    ("gemma3:12b", 8.1, "strong writing, needs 12 GB+"),
    ("qwen3:14b", 9.3, "better judgement, needs 12 GB+"),
    ("gpt-oss:20b", 14.0, "OpenAI's open model, needs 16 GB"),
    ("qwen3:30b", 19.0, "best quality here, needs 24 GB"),
]


def cloud_model_label(model_id: str, name: str = "", note: str = "") -> str:
    known = {m[0]: m for m in CLOUD_MODELS}.get(model_id)
    name = name or (known[1] if known else model_id)
    note = note or (known[2] if known else "")
    return f"{name}  ·  {note}" if note else name


def list_cloud_models(key: str | None = None) -> list[dict]:
    """Every Claude model your API key can use, newest first: {"id", "name"}."""
    key = key or get_api_key()
    if not key:
        raise AIError("Add your Anthropic API key first.")
    out, after = [], ""
    for _ in range(10):
        url = "https://api.anthropic.com/v1/models?limit=1000" + (f"&after_id={after}" if after else "")
        req = urllib.request.Request(url, headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.load(r)
        except urllib.error.HTTPError as e:
            raise AIError("Anthropic didn't accept the key (check it in console.anthropic.com)." if e.code in (401, 403)
                          else f"Anthropic answered {e.code} when listing models.") from e
        except urllib.error.URLError as e:
            raise AIError(f"Couldn't reach Anthropic ({e.reason}).") from e
        out += [{"id": m["id"], "name": m.get("display_name") or m["id"]} for m in d.get("data", [])]
        if not d.get("has_more"):
            break
        after = d.get("last_id", "")
    return out


def pull_local_model(name: str, url: str | None = None, on_progress=None, cancel=None) -> str:
    """Download a model into Ollama (POST /api/pull, streamed progress)."""
    url = (url or settings()["local_url"]).rstrip("/")
    req = urllib.request.Request(f"{url}/api/pull", data=json.dumps({"model": name, "stream": True}).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            for line in r:
                if cancel and cancel():
                    raise AIError("Download stopped. Start it again to carry on where it left off.")
                if not line.strip():
                    continue
                d = json.loads(line)
                if d.get("error"):
                    raise AIError(f"Ollama couldn't download {name}: {d['error']}")
                total, done = d.get("total") or 0, d.get("completed") or 0
                if on_progress:
                    frac = done / total if total else -1
                    on_progress(frac, f"Downloading {name}: {d.get('status', '')}"
                                + (f"  {fmt_size(done)} of {fmt_size(total)}" if total else ""))
                if d.get("status") == "success":
                    return name
    except urllib.error.URLError as e:
        raise AIError(f"Ollama isn't running at {url}. Start it, then try again.") from e
    return name


# ----------------------------------------------------------------------------- local models
def fmt_size(n: float | None) -> str:
    if not n:
        return ""
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def model_label(m: dict) -> str:
    """'llama3.1:8b  ·  8.0B  ·  4.9 GB' - name, parameter count and size on disk."""
    parts = [m["name"]] + [x for x in (m.get("params"), fmt_size(m.get("size"))) if x]
    return "  ·  ".join(parts)


def list_local_models(kind: str | None = None, url: str | None = None) -> list[dict]:
    """Installed models as {"name", "size" (bytes or None), "params"}, smallest first."""
    c = settings()
    kind, url = kind or c["local_kind"], (url or c["local_url"]).rstrip("/")
    try:
        if kind == "ollama":
            models = [{"name": m["name"], "size": m.get("size"),
                       "params": (m.get("details") or {}).get("parameter_size", "")}
                      for m in _get(f"{url}/api/tags").get("models", [])]
        else:
            models = [{"name": m["id"], "size": None, "params": ""}
                      for m in _get(f"{url}/v1/models").get("data", [])]
            try:   # LM Studio's own API also reports file sizes
                extra = {m.get("id"): m for m in _get(f"{url}/api/v0/models").get("data", [])}
                for m in models:
                    e = extra.get(m["name"]) or {}
                    m["size"] = e.get("size_bytes") or e.get("size")
                    m["params"] = e.get("params") or ""
            except Exception:
                pass
    except Exception as e:
        name = "Ollama" if kind == "ollama" else "the local model server"
        raise AIError(f"{name} isn't running at {url}. "
                      + ("Install it from ollama.com, then run e.g. 'ollama pull llama3.1'." if kind == "ollama"
                         else "Start the server in LM Studio (Developer tab) or your tool of choice.")) from e
    return sorted(models, key=lambda m: (m["size"] is None, m["size"] or 0, m["name"].lower()))


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


BOUNDARY_SYSTEM = ("You edit short clips cut from livestreams. You are shown numbered transcript lines around one "
                   "edge of a clip. Choose the line where the clip should {edge} so it works on its own: "
                   "{rule} Answer only with JSON: {{\"line\": <number>, \"why\": \"<one short sentence>\"}}")
BOUNDARY_RULES = {
    "start": "begin where the topic is introduced, so a viewer knows what is being talked about before the "
             "point lands; start on the first word of a sentence, never mid-thought, and don't include unrelated "
             "chatter before it.",
    "end": "end right after the point, punchline or reaction lands; finish on the end of a sentence and "
           "don't run on into the next topic.",
}


def pick_boundary(lines: list[tuple[float, float, str]], edge: str, current: float, about: str = "") -> dict:
    """lines: (start, end, text) around a clip edge. Returns {"t": new time, "why", "line"}."""
    if not lines:
        raise AIError("Nothing was said around there, so there's no sentence to start or end on.")
    numbered = "\n".join(f"{i + 1}. [{a:.1f}s] {txt.strip()}" for i, (a, b, txt) in enumerate(lines))
    prompt = (f"The clip is about: {about or 'unknown'}\n"
              f"The clip currently {'starts' if edge == 'start' else 'ends'} at {current:.1f}s.\n\n"
              f"Transcript:\n{numbered}\n\nWhich line should the clip {edge} {'on' if edge == 'start' else 'after'}?")
    d = complete_json(prompt, BOUNDARY_SYSTEM.format(edge=edge, rule=BOUNDARY_RULES[edge]), max_tokens=150)
    try:
        n = int(d.get("line"))
    except (TypeError, ValueError):
        raise AIError(f"The model didn't pick a line: {str(d)[:120]}")
    n = max(1, min(len(lines), n))
    a, b, _ = lines[n - 1]
    return {"t": a if edge == "start" else b, "why": str(d.get("why", "")).strip()[:200], "line": n}
