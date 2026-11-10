"""One place for every Claude call, so cost and usage are measured per agent.
    from .llm import ask
    text = ask(system, user, agent="technical_fixer", site_id="kalosa", max_tokens=2000)
Needs ANTHROPIC_API_KEY. Model: env SEO_MODEL (default claude-haiku-5-5); pass model= to override for a hard task.
Every call is logged to llm_usage (never blocks the work if logging fails). LLM_DISABLED=1 makes ask() raise LLMUnavailable so callers use their no-LLM fallback."""
import os, json, time
from . import sb

class LLMUnavailable(RuntimeError): pass

# USD per 1M tokens (input, output). Rough, editable by env LLM_PRICES='{"model":[in,out]}'. Used only for the cost estimate.
PRICES = {"claude-haiku-5-5": (1.0, 5.0), "claude-sonnet-5-5": (3.0, 15.0), "claude-opus-5-5": (5.0, 25.0)}

def _cost(model, tin, tout):
    p = dict(PRICES); 
    try: p.update(json.loads(os.environ.get("LLM_PRICES") or "{}"))
    except ValueError: pass
    a, b = p.get(model, (3.0, 15.0)); return round((tin * a + tout * b) / 1e6, 6)

def ask(system, user, *, agent, site_id=None, max_tokens=2000, model=None, temperature=None):
    if os.environ.get("LLM_DISABLED") == "1" or not os.environ.get("ANTHROPIC_API_KEY"): raise LLMUnavailable("no ANTHROPIC_API_KEY or LLM_DISABLED=1")
    try: import anthropic
    except ImportError as e: raise LLMUnavailable("anthropic package missing") from e
    model = model or os.environ.get("SEO_MODEL", "claude-haiku-5-5"); t0 = time.time()
    kw = {"temperature": temperature} if temperature is not None else {}
    r = anthropic.Anthropic().messages.create(model=model, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": user if isinstance(user, str) else json.dumps(user)}], **kw)
    text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
    try:
        u = r.usage; sb.insert("llm_usage", [{"agent": agent, "site_id": site_id, "model": model, "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                                              "cost_usd": _cost(model, u.input_tokens, u.output_tokens), "seconds": round(time.time() - t0, 2), "stop_reason": r.stop_reason}])
    except Exception as e: print("llm usage not logged:", str(e)[:100])
    return text

def ask_json(system, user, **kw):
    """Ask for a JSON reply and parse it (object or array). Raises ValueError if it cannot be read."""
    t = ask(system, user, **kw); i = min([x for x in (t.find("{"), t.find("[")) if x >= 0] or [-1])
    if i < 0: raise ValueError("no JSON in reply: " + t[:200])
    end = max(t.rfind("}"), t.rfind("]")); return json.loads(t[i:end + 1])
