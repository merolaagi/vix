"""Model router: every place in vix that wants a language model asks for a *role* (chat, proposer, critic, analyst,
council), and the router picks the model the founder assigned to it in Settings.

Providers: local models through Ollama (free, default), any OpenAI-compatible server (LM Studio, vLLM, llama.cpp),
and cloud APIs: Anthropic, OpenAI, Google Gemini, xAI, Mistral, DeepSeek, Groq, OpenRouter, Together.

The founder's base model is Claude through their claude.ai subscription. A subscription is not an API, so vix never
calls it; instead Claude takes part through MCP (it reads dossiers and votes, or proposes improvements, when the
founder works with it in claude.ai). The paid Claude API is optional and stays off until a monthly limit is set.

Keys live in data/llm_settings.json (owner-only permissions, inside the git-ignored data folder) or in environment
variables, which win. Every call is logged with tokens and, when prices are entered, cost."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid

import httpx

from . import db
from .config import DATA_DIR

SETTINGS = DATA_DIR / "llm_settings.json"

PROVIDERS = {
    # id: (label, api kind, default base URL, key env var, needs key, key page)
    "ollama": ("Ollama (models on this Mac)", "ollama", "http://localhost:11434", "", False, "https://ollama.com/download"),
    "lmstudio": ("LM Studio / any OpenAI-compatible server", "openai", "http://localhost:1234/v1", "", False, "https://lmstudio.ai"),
    "anthropic": ("Anthropic (Claude API, paid)", "anthropic", "https://api.anthropic.com", "ANTHROPIC_API_KEY", True, "https://console.anthropic.com/settings/keys"),
    "openai": ("OpenAI", "openai", "https://api.openai.com/v1", "OPENAI_API_KEY", True, "https://platform.openai.com/api-keys"),
    "gemini": ("Google Gemini", "gemini", "https://generativelanguage.googleapis.com/v1beta", "GEMINI_API_KEY", True, "https://aistudio.google.com/apikey"),
    "xai": ("xAI (Grok)", "openai", "https://api.x.ai/v1", "XAI_API_KEY", True, "https://console.x.ai"),
    "mistral": ("Mistral", "openai", "https://api.mistral.ai/v1", "MISTRAL_API_KEY", True, "https://console.mistral.ai/api-keys"),
    "deepseek": ("DeepSeek", "openai", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", True, "https://platform.deepseek.com/api_keys"),
    "groq": ("Groq", "openai", "https://api.groq.com/openai/v1", "GROQ_API_KEY", True, "https://console.groq.com/keys"),
    "openrouter": ("OpenRouter", "openai", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", True, "https://openrouter.ai/keys"),
    "together": ("Together AI", "openai", "https://api.together.xyz/v1", "TOGETHER_API_KEY", True, "https://api.together.xyz/settings/api-keys"),
}
ROLES = {
    "chat": "Ask vix, the assistant in HQ and over MCP",
    "proposer": "Proposes harness improvements in the self-improvement lab",
    "critic": "Screens proposed improvements for metric gaming",
    "analyst": "Digests evaluation results for the next proposal",
    "council": "Members of the bench that deliberates on tough decisions",
}
CLAUDE_SEAT = "claude:subscription"   # Claude via the founder's claude.ai subscription, through MCP
SUGGESTED_LOCAL = ["qwen3.6:27b", "qwen3:32b", "qwen3-coder:latest", "qwen3:14b", "qwen3:8b", "llama3.1:8b", "mistral-nemo:12b", "qwen2.5:7b", "gemma3:12b", "phi4:14b"]

USAGE_SCHEMA = """
create table if not exists llm_usage (
  id integer primary key, ts real, month text, provider text, model text, role text, in_tokens integer, out_tokens integer,
  cost_usd real, latency_s real, ok integer, error text
);
"""
_lock = threading.Lock()
_pulls: dict[str, dict] = {}


class BudgetExceeded(RuntimeError):
    pass


class NoModel(RuntimeError):
    pass


def init():
    with db.conn() as c:
        c.executescript(USAGE_SCHEMA)


# ---------------------------------------------------------------- settings


def _default() -> dict:
    return {"providers": {}, "default": "", "roles": {r: [] for r in ROLES}, "council": {"members": [CLAUDE_SEAT], "auto_convene": True,
            "quorum": 2, "mode": "advise"}, "anthropic_monthly_token_limit": 0, "prices": {}, "timeout_s": 120, "temperature": 0.2, "max_tokens": 1200}


def load() -> dict:
    s = _default()
    try:
        if SETTINGS.exists():
            got = json.loads(SETTINGS.read_text())
            s.update({k: v for k, v in got.items() if k in s})
            s["roles"] = {**_default()["roles"], **(got.get("roles") or {})}
            s["council"] = {**_default()["council"], **(got.get("council") or {})}
    except Exception:  # noqa: BLE001
        pass
    if os.environ.get("VIX_OLLAMA_URL"):
        s["providers"].setdefault("ollama", {})["base_url"] = os.environ["VIX_OLLAMA_URL"]
    return s


def save(s: dict):
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(SETTINGS)


def provider_cfg(pid: str, s: dict | None = None) -> dict:
    s = s or load()
    label, kind, url, env, needs, page = PROVIDERS[pid]
    p = s["providers"].get(pid, {})
    key = (os.environ.get(env) if env else "") or p.get("key", "")
    return {"id": pid, "label": label, "kind": kind, "base_url": (p.get("base_url") or url).rstrip("/"), "key": key,
            "key_source": "environment" if env and os.environ.get(env) else ("settings" if p.get("key") else None),
            "needs_key": needs, "enabled": p.get("enabled", not needs or bool(key)), "key_page": page, "models": p.get("models", [])}


def mask(k: str) -> str:
    return f"{k[:4]}…{k[-4:]}" if k and len(k) > 10 else ("set" if k else "")


def public_settings() -> dict:
    s = load()
    provs = []
    for pid in PROVIDERS:
        c = provider_cfg(pid, s)
        provs.append({**{k: v for k, v in c.items() if k != "key"}, "key_masked": mask(c["key"])})
    return {"providers": provs, "default": s["default"], "roles": s["roles"], "role_help": ROLES, "council": s["council"],
            "anthropic_monthly_token_limit": s["anthropic_monthly_token_limit"], "prices": s["prices"], "timeout_s": s["timeout_s"],
            "temperature": s["temperature"], "max_tokens": s["max_tokens"], "claude_seat": CLAUDE_SEAT, "usage": usage_month(),
            "base_model_note": "Your base model is Claude on your claude.ai subscription. It works vix through MCP and holds a seat on "
                               "the council; vix never calls the subscription. The Claude API below is optional and paid.",
            "suggested_local": SUGGESTED_LOCAL}


def update(patch: dict) -> dict:
    s = load()
    for pid, p in (patch.get("providers") or {}).items():
        if pid not in PROVIDERS:
            raise ValueError(f"unknown provider {pid}")
        cur = s["providers"].setdefault(pid, {})
        for k in ("base_url", "enabled", "models"):
            if k in p:
                cur[k] = p[k]
        if "key" in p:
            if p["key"]:
                cur["key"] = p["key"].strip()
            else:
                cur.pop("key", None)
    for k in ("default", "anthropic_monthly_token_limit", "prices", "timeout_s", "temperature", "max_tokens"):
        if k in patch:
            s[k] = patch[k]
    if "roles" in patch:
        for r, models in patch["roles"].items():
            if r in ROLES:
                s["roles"][r] = [m for m in models if m]
    if "council" in patch:
        s["council"].update({k: v for k, v in patch["council"].items() if k in ("members", "auto_convene", "quorum", "mode")})
    save(s)
    return public_settings()


# ---------------------------------------------------------------- usage and budget


def _month():
    return time.strftime("%Y-%m")


def usage_month() -> list[dict]:
    init()
    return db.rows("select provider, model, count(*) calls, sum(in_tokens) in_tokens, sum(out_tokens) out_tokens,"
                   " round(sum(cost_usd),4) cost_usd, sum(1-ok) errors from llm_usage where month=? group by provider, model"
                   " order by calls desc", (_month(),))


def _log(provider, model, role, tin, tout, latency, ok, error=None):
    init()
    price = (load()["prices"] or {}).get(f"{provider}:{model}") or {}
    cost = (tin or 0) / 1e6 * float(price.get("in", 0) or 0) + (tout or 0) / 1e6 * float(price.get("out", 0) or 0)
    db.execute("insert into llm_usage(ts,month,provider,model,role,in_tokens,out_tokens,cost_usd,latency_s,ok,error) values(?,?,?,?,?,?,?,?,?,?,?)",
               (time.time(), _month(), provider, model, role, tin or 0, tout or 0, round(cost, 6), round(latency, 2), int(ok), (error or "")[:300]))


def _check_budget(provider):
    if provider != "anthropic":
        return
    limit = int(load().get("anthropic_monthly_token_limit") or 0)
    if limit <= 0:
        raise BudgetExceeded("the Claude API is off: set a monthly token limit in Settings to use it")
    used = db.scalar("select coalesce(sum(in_tokens+out_tokens),0) from llm_usage where provider='anthropic' and month=?", (_month(),)) or 0
    if used >= limit:
        raise BudgetExceeded(f"Claude API monthly limit reached ({used:,} of {limit:,} tokens)")


# ---------------------------------------------------------------- provider calls


def _client(timeout):
    return httpx.Client(timeout=timeout)


def list_models(pid: str) -> list[str]:
    c = provider_cfg(pid)
    with _client(10) as h:
        if c["kind"] == "ollama":
            return [m["name"] for m in h.get(f"{c['base_url']}/api/tags").json().get("models", [])]
        if c["kind"] == "anthropic":
            r = h.get(f"{c['base_url']}/v1/models", headers={"x-api-key": c["key"], "anthropic-version": "2023-06-01"})
            r.raise_for_status()
            return [m["id"] for m in r.json().get("data", [])]
        if c["kind"] == "gemini":
            r = h.get(f"{c['base_url']}/models", params={"key": c["key"]})
            r.raise_for_status()
            return [m["name"].split("/")[-1] for m in r.json().get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
        r = h.get(f"{c['base_url']}/models", headers={"Authorization": f"Bearer {c['key']}"} if c["key"] else {})
        r.raise_for_status()
        return sorted(m["id"] for m in r.json().get("data", []))


def test_provider(pid: str) -> dict:
    t = time.time()
    try:
        models = list_models(pid)
        s = load()
        s["providers"].setdefault(pid, {})["models"] = models[:200]
        save(s)
        return {"ok": True, "models": models[:200], "seconds": round(time.time() - t, 2)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}", "seconds": round(time.time() - t, 2)}


def _split(spec: str) -> tuple[str, str]:
    if ":" not in spec:
        raise NoModel(f"model spec must be provider:model, got {spec!r}")
    pid, model = spec.split(":", 1)
    if pid not in PROVIDERS:
        raise NoModel(f"unknown provider {pid}")
    return pid, model


def _to_openai_tools(tools):
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t.get("parameters") or {"type": "object", "properties": {}}}} for t in tools or []]


def call(spec: str, messages: list[dict], system: str = "", tools: list[dict] | None = None, json_mode: bool = False,
         role: str = "", max_tokens: int | None = None, temperature: float | None = None) -> dict:
    """One model call. messages use {"role": "user"|"assistant"|"tool", "content": str, ...}. Returns
    {"text", "tool_calls": [{"id","name","arguments"}], "model", "provider", "usage"}. Tool results are passed back as
    {"role": "tool", "tool_call_id", "name", "content"} and assistant turns that called tools as
    {"role": "assistant", "content": "", "tool_calls": [...]}"""
    if spec == CLAUDE_SEAT:
        raise NoModel("Claude on your subscription answers through MCP, not through the router")
    if os.environ.get("VIX_NO_LLM"):
        raise NoModel("model calls are off in this sandbox")
    pid, model = _split(spec)
    c = provider_cfg(pid)
    if c["needs_key"] and not c["key"]:
        raise NoModel(f"{c['label']} has no key")
    _check_budget(pid)
    s = load()
    mt = max_tokens or s["max_tokens"]
    temp = s["temperature"] if temperature is None else temperature
    t0 = time.time()
    try:
        with _client(s["timeout_s"]) as h:
            if c["kind"] == "ollama":
                msgs = ([{"role": "system", "content": system}] if system else []) + [
                    {"role": m["role"], "content": m.get("content") or "", **({"tool_calls": [{"function": {"name": tc["name"], "arguments": tc["arguments"]}}
                                                                                for tc in m["tool_calls"]]} if m.get("tool_calls") else {}),
                     **({"tool_name": m["name"]} if m["role"] == "tool" else {})} for m in messages]
                body = {"model": model, "messages": msgs, "stream": False, "options": {"temperature": temp, "num_predict": mt}}
                if tools:
                    body["tools"] = _to_openai_tools(tools)
                if json_mode:
                    body["format"] = "json"
                d = h.post(f"{c['base_url']}/api/chat", json=body).raise_for_status().json()
                m = d.get("message", {})
                tcs = [{"id": f"t{i}", "name": x["function"]["name"], "arguments": x["function"].get("arguments") or {}}
                       for i, x in enumerate(m.get("tool_calls") or [])]
                out = {"text": m.get("content", ""), "tool_calls": tcs, "usage": (d.get("prompt_eval_count", 0), d.get("eval_count", 0))}
            elif c["kind"] == "anthropic":
                msgs = []
                for m in messages:
                    if m["role"] == "tool":
                        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}]})
                    elif m.get("tool_calls"):
                        msgs.append({"role": "assistant", "content": ([{"type": "text", "text": m["content"]}] if m.get("content") else []) +
                                     [{"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["arguments"]} for tc in m["tool_calls"]]})
                    else:
                        msgs.append({"role": m["role"], "content": m.get("content") or ""})
                body = {"model": model, "max_tokens": mt, "temperature": temp, "messages": msgs}
                if system:
                    body["system"] = system + ("\nReply with one JSON object only." if json_mode else "")
                elif json_mode:
                    body["system"] = "Reply with one JSON object only."
                if tools:
                    body["tools"] = [{"name": t["name"], "description": t.get("description", ""),
                                      "input_schema": t.get("parameters") or {"type": "object", "properties": {}}} for t in tools]
                d = h.post(f"{c['base_url']}/v1/messages", json=body, headers={"x-api-key": c["key"], "anthropic-version": "2023-06-01"}).raise_for_status().json()
                text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
                tcs = [{"id": b["id"], "name": b["name"], "arguments": b.get("input") or {}} for b in d.get("content", []) if b.get("type") == "tool_use"]
                u = d.get("usage", {})
                out = {"text": text, "tool_calls": tcs, "usage": (u.get("input_tokens", 0), u.get("output_tokens", 0))}
            elif c["kind"] == "gemini":
                contents = []
                for m in messages:
                    if m["role"] == "tool":
                        contents.append({"role": "user", "parts": [{"functionResponse": {"name": m["name"], "response": {"result": m["content"]}}}]})
                    elif m.get("tool_calls"):
                        contents.append({"role": "model", "parts": [{"functionCall": {"name": tc["name"], "args": tc["arguments"]}} for tc in m["tool_calls"]]})
                    else:
                        contents.append({"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m.get("content") or ""}]})
                body = {"contents": contents, "generationConfig": {"temperature": temp, "maxOutputTokens": mt,
                                                                   **({"responseMimeType": "application/json"} if json_mode and not tools else {})}}
                if system:
                    body["systemInstruction"] = {"parts": [{"text": system}]}
                if tools:
                    body["tools"] = [{"functionDeclarations": [{"name": t["name"], "description": t.get("description", ""),
                                                                "parameters": t.get("parameters") or {"type": "object", "properties": {}}} for t in tools]}]
                d = h.post(f"{c['base_url']}/models/{model}:generateContent", params={"key": c["key"]}, json=body).raise_for_status().json()
                parts = (d.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts)
                tcs = [{"id": f"g{i}", "name": p["functionCall"]["name"], "arguments": p["functionCall"].get("args") or {}}
                       for i, p in enumerate(parts) if "functionCall" in p]
                u = d.get("usageMetadata", {})
                out = {"text": text, "tool_calls": tcs, "usage": (u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0))}
            else:  # OpenAI-compatible
                msgs = ([{"role": "system", "content": system}] if system else [])
                for m in messages:
                    if m["role"] == "tool":
                        msgs.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
                    elif m.get("tool_calls"):
                        msgs.append({"role": "assistant", "content": m.get("content") or None,
                                     "tool_calls": [{"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}}
                                                    for tc in m["tool_calls"]]})
                    else:
                        msgs.append({"role": m["role"], "content": m.get("content") or ""})
                body = {"model": model, "messages": msgs, "temperature": temp, "max_tokens": mt}
                if tools:
                    body["tools"] = _to_openai_tools(tools)
                if json_mode and not tools:
                    body["response_format"] = {"type": "json_object"}
                hdr = {"Authorization": f"Bearer {c['key']}"} if c["key"] else {}
                if pid == "openrouter":
                    hdr.update({"HTTP-Referer": "http://localhost", "X-Title": "vix"})
                d = h.post(f"{c['base_url']}/chat/completions", json=body, headers=hdr).raise_for_status().json()
                m = d["choices"][0]["message"]
                tcs = []
                for x in m.get("tool_calls") or []:
                    a = x["function"].get("arguments") or "{}"
                    tcs.append({"id": x.get("id") or uuid.uuid4().hex[:8], "name": x["function"]["name"],
                                "arguments": json.loads(a) if isinstance(a, str) else a})
                u = d.get("usage", {})
                out = {"text": m.get("content") or "", "tool_calls": tcs, "usage": (u.get("prompt_tokens", 0), u.get("completion_tokens", 0))}
    except httpx.HTTPStatusError as e:
        _log(pid, model, role, 0, 0, time.time() - t0, False, f"HTTP {e.response.status_code}: {e.response.text[:200]}")
        raise
    except Exception as e:  # noqa: BLE001
        _log(pid, model, role, 0, 0, time.time() - t0, False, f"{type(e).__name__}: {e}")
        raise
    out["text"] = re.sub(r"<think>.*?</think>", "", out["text"] or "", flags=re.S).strip()
    _log(pid, model, role, out["usage"][0], out["usage"][1], time.time() - t0, True)
    return {**out, "model": model, "provider": pid, "latency_s": round(time.time() - t0, 2)}


def models_for(role: str) -> list[str]:
    """The model specs assigned to a role, then the default, then the first local model that is running."""
    if os.environ.get("VIX_NO_LLM"):
        return []
    s = load()
    out = [m for m in s["roles"].get(role, []) if m and m != CLAUDE_SEAT]
    if s["default"] and s["default"] not in out:
        out.append(s["default"])
    if not out:
        try:
            local = list_models("ollama")
            pick = next((m for m in SUGGESTED_LOCAL if m in local), None) or next((m for m in local if "embed" not in m), None)
            if pick:
                out.append(f"ollama:{pick}")
        except Exception:  # noqa: BLE001
            pass
    return out


def ask(role: str, messages: list[dict], **kw) -> dict:
    """Call the first model for the role that works; budget limits and outages fall through to the next one."""
    errors = []
    for spec in models_for(role):
        try:
            return call(spec, messages, role=role, **kw)
        except (BudgetExceeded, NoModel, httpx.HTTPError, KeyError, ValueError) as e:
            errors.append(f"{spec}: {e}")
    raise NoModel("no model available for " + role + (": " + "; ".join(errors) if errors else " (none configured and Ollama is not running)"))


def parse_json(text: str) -> dict:
    t = text.strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        raise ValueError("no JSON object in the reply")
    return json.loads(m.group(0))


def available(role: str) -> bool:
    return bool(models_for(role))


# ---------------------------------------------------------------- Ollama management


def ollama_status() -> dict:
    c = provider_cfg("ollama")
    try:
        with _client(4) as h:
            tags = h.get(f"{c['base_url']}/api/tags").json().get("models", [])
            ver = h.get(f"{c['base_url']}/api/version").json().get("version")
        models = [{"name": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 1),
                   "parameters": (m.get("details") or {}).get("parameter_size"), "quantization": (m.get("details") or {}).get("quantization_level"),
                   "family": (m.get("details") or {}).get("family")} for m in tags]
        return {"running": True, "version": ver, "url": c["base_url"], "models": models, "pulls": _pulls}
    except Exception as e:  # noqa: BLE001
        return {"running": False, "url": c["base_url"], "error": type(e).__name__, "models": [], "pulls": _pulls,
                "hint": "Install Ollama from ollama.com and open it, or run: ollama serve"}


def ollama_pull(name: str) -> dict:
    if not re.fullmatch(r"[\w.\-/:]+", name or ""):
        raise ValueError("bad model name")
    c = provider_cfg("ollama")
    _pulls[name] = {"status": "starting", "completed": 0, "total": 0}

    def run():
        try:
            with httpx.stream("POST", f"{c['base_url']}/api/pull", json={"name": name, "stream": True}, timeout=None) as r:
                for line in r.iter_lines():
                    if not line:
                        continue
                    d = json.loads(line)
                    _pulls[name] = {"status": d.get("status", ""), "completed": d.get("completed", 0), "total": d.get("total", 0)}
            _pulls[name]["status"] = "done"
        except Exception as e:  # noqa: BLE001
            _pulls[name] = {"status": f"failed: {e}", "completed": 0, "total": 0}
    threading.Thread(target=run, daemon=True).start()
    return {"pulling": name}


def ollama_delete(name: str) -> dict:
    c = provider_cfg("ollama")
    with _client(30) as h:
        h.request("DELETE", f"{c['base_url']}/api/delete", json={"name": name}).raise_for_status()
    return {"deleted": name}
