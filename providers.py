#!/usr/bin/env python3
"""Model providers for AGX: free cloud tiers and local Ollama in one registry.

Used by router.py (direct model calls), run.sh and meet.sh (the opencode
worker's model) and dashboard.py (the providers panel).

A tier is what the work needs:
  small   fast and cheap - classifying tasks, docs, short edits
  big     strongest reasoning - questions, planning, the opencode worker
  vision  understands images - screenshots, diagrams, UI reviews

Nothing here spends money. Cloud providers are used on their free tiers only
(add a key and that provider joins in) and Ollama runs on this machine.

Commands
  providers.py status [--json]   what is configured and what each tier uses
  providers.py pick <tier>       the provider/model a tier resolves to now
  providers.py test <provider>   one tiny call to check a key works
  providers.py opencode-model    best model for the opencode worker
  providers.py opencode-config   write state/opencode.json, print its path
  providers.py ollama-setup      create AGX model aliases with a usable context
"""
import json, os, re, subprocess, sys, threading, time, urllib.error, urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE, "state")
CONFIG = os.path.join(BASE, "config.env")
SETTINGS = os.path.join(BASE, "settings.json")
TIERS = ("small", "big", "vision")


def load_config():
    """Read API keys from config.env without executing it.

    The dashboard runs as a systemd service that never sources config.env, so
    the registry loads it itself. Variables already in the environment win.
    """
    try:
        lines = open(CONFIG, encoding="utf-8").read().splitlines()
    except OSError:
        return
    for line in lines:
        m = re.match(r"\s*(?:export\s+)?([A-Z][A-Z0-9_]*)=(['\"]?)(.*)\2\s*$", line)
        if m and m.group(3) and not os.environ.get(m.group(1)):
            os.environ[m.group(1)] = m.group(3)


load_config()


def _ollama_url():
    h = os.environ.get("OLLAMA_HOST", "").strip() or "127.0.0.1:11434"
    if "://" not in h:
        h = "http://" + h
    return h.replace("://0.0.0.0", "://127.0.0.1").rstrip("/")


OLLAMA = _ollama_url()

# Installed models AGX never picks on its own: abliterated or "uncensored"
# fine-tunes and tools sold for writing malware. Autonomous workers have shell
# access, so they run on mainstream models only.
OLLAMA_DENY = re.compile(r"wormgpt|abliterat|uncen|whiterabbit|notmythos", re.I)

# AGX aliases: the same weights with a context window agents can use. Ollama
# serves API calls with 4096 tokens by default, which silently truncates an
# agent's prompt. Only "agent" aliases are offered to the opencode worker: a
# 7B model cannot follow its tool protocol and loops until the timeout.
ALIASES = {
    "agx-coder": {"bases": ["qwen3-coder:30b", "gpt-oss:20b", "qwen3:30b", "qwen3:32b"],
                  "ctx": 32768, "agent": True},
    "agx-coder-fast": {"bases": ["qwen2.5-coder:7b", "qwen3:8b", "llama3.2:latest"],
                       "ctx": 16384, "agent": False},
    "agx-vision": {"bases": ["qwen3-vl:8b", "llama3.2-vision:11b", "gemma3:12b"],
                   "ctx": 16384, "agent": False},
}

# Each tier lists model patterns, best first. Patterns are regular expressions
# matched against the provider's live model list; when several models match one
# pattern the newest version wins, so glm-5.3 beats glm-5.2 without an edit.
PROVIDERS = {
    "nvidia": {
        "label": "NVIDIA NIM",
        "url": "https://integrate.api.nvidia.com/v1",
        "env": "NVIDIA_API_KEY", "prefix": "nvapi-",
        "signup": "https://build.nvidia.com",
        "blurb": "GLM-5.x, Kimi K3, Qwen3 Coder 480B on free credits",
        "tiers": {
            "big": [r"^z-ai/glm-5(\.\d+)?$", r"^moonshotai/kimi-k\d",
                    r"^nvidia/nemotron-3-ultra-", r"^qwen/qwen3-coder-480b",
                    r"^qwen/qwen3\.5-397b", r"^deepseek-ai/deepseek-v4-pro-\d+$",
                    r"^minimaxai/minimax-m\d", r"^mistralai/mistral-large-3",
                    r"^nvidia/nemotron-3-super-"],
            "small": [r"^z-ai/glm-5(\.\d+)?-flash$", r"^nvidia/nemotron-3-super-",
                      r"^deepseek-ai/deepseek-v4(\.\d+)?-flash$",
                      r"^nvidia/nemotron-3\.5-lightning", r"^qwen/qwen3-next-80b",
                      r"^qwen/qwen2\.5-coder-32b", r"^openai/gpt-oss-20b$"],
            "vision": [r"^z-ai/glm-5(\.\d+)?-flash$", r"^moonshotai/kimi-k3",
                       r"^qwen/qwen3\.5-397b", r"^deepseek-ai/deepseek-v4\.1-flash$",
                       r"^minimaxai/minimax-m3$", r"^google/gemma-4-31b-it$",
                       r"^nvidia/nemotron-nano-12b-v2-vl$"],
        },
    },
    "openrouter": {
        "label": "OpenRouter",
        "url": "https://openrouter.ai/api/v1",
        "env": "OPENROUTER_API_KEY", "prefix": "sk-or-",
        "signup": "https://openrouter.ai/keys",
        "blurb": "dozens of :free models, 50 requests/day without credits",
        "free_only": True,
        "tiers": {
            # Free Inkling only serves the agent apps OpenRouter lists, so it is
            # left out: every other caller gets a 403.
            "big": [r"nemotron-3-ultra.*:free$", r"^poolside/laguna-s-.*:free$",
                    r"nemotron-3-super.*:free$"],
            "small": [r"nemotron-3\.5-lightning:free$", r"^cohere/north-mini-code:free$",
                      r"^poolside/laguna-xs-.*:free$", r"^google/gemma-4-26b.*:free$"],
            "vision": [r"^google/gemma-4-31b-it:free$", r"nemotron-3-nano-omni.*:free$"],
        },
    },
    "github": {
        "label": "GitHub Models",
        "url": "https://models.github.ai/inference",
        "env": "GITHUB_TOKEN", "prefix": "",
        "signup": "https://github.com/settings/personal-access-tokens/new",
        "blurb": "free GPT-4.1, DeepSeek, Llama and more with a GitHub token (models:read)",
        # GitHub's catalog lives on a different path, so list the stable free
        # ids here. A renamed one just rests and routing falls back.
        "static": ["openai/gpt-4.1", "openai/gpt-4o", "openai/gpt-4.1-mini",
                   "openai/gpt-4o-mini", "deepseek/DeepSeek-R1-0528", "deepseek/DeepSeek-V3-0324",
                   "meta/Llama-3.3-70B-Instruct", "meta/Llama-4-Maverick-17B-128E-Instruct-FP8",
                   "mistral-ai/Mistral-Large-2411", "microsoft/Phi-4"],
        "tiers": {
            "big": [r"gpt-4\.1$", r"deepseek-r1", r"llama-4-maverick", r"deepseek-v3",
                    r"gpt-4o$", r"llama-3\.3-70b", r"mistral-large"],
            "small": [r"gpt-4\.1-mini$", r"gpt-4o-mini$", r"phi-4$"],
            "vision": [r"gpt-4o$", r"gpt-4\.1$", r"llama-4-maverick"],
        },
    },
    "huggingface": {
        "label": "Hugging Face",
        "url": "https://router.huggingface.co/v1",
        "env": "HF_TOKEN", "prefix": "hf_",
        "signup": "https://huggingface.co/settings/tokens",
        "blurb": "OpenAI-compatible router to 100+ models; a small free monthly credit",
        "tiers": {
            "big": [r"zai-org/GLM-5\.\d+$", r"moonshotai/Kimi-K\d", r"deepseek-ai/DeepSeek-V\d(?!.*Flash)",
                    r"Qwen/Qwen3\.\d+-\d+B$", r"meta-llama/Llama-3\.3-70B"],
            "small": [r"zai-org/GLM-5\.\d+-Flash", r"deepseek-ai/DeepSeek-V\d.*-Flash",
                      r"meta-llama/Llama-3\.1-8B", r"google/gemma-4-31"],
            "vision": [r"Qwen/Qwen3\.\d+-\d+B$", r"google/gemma-4-31", r"Qwen/Qwen2\.5-VL"],
        },
    },
    "chatanywhere": {
        "label": "ChatAnywhere (GPT)",
        "url": "https://api.chatanywhere.tech/v1",
        "env": "CHATANYWHERE_API_KEY", "prefix": "sk-",
        "signup": "https://github.com/chatanywhere/GPT_API_free",
        "blurb": "free GPT-4o-mini and GPT-3.5 proxy; register a token on their GitHub",
        "static": ["gpt-4o-mini", "gpt-4.1-mini", "gpt-3.5-turbo", "deepseek-r1", "deepseek-v3"],
        "tiers": {
            "big": [r"^gpt-4\.1-mini$", r"^deepseek-r1$", r"^deepseek-v3$", r"^gpt-4o-mini$"],
            "small": [r"^gpt-4o-mini$", r"^gpt-3\.5-turbo$"],
            "vision": [r"^gpt-4o-mini$"],
        },
    },
    "ovh": {
        "label": "OVHcloud (free)",
        "url": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1",
        "env": "", "prefix": "",
        "signup": "https://www.ovhcloud.com/en/public-cloud/ai-endpoints/",
        "blurb": "no key needed: Qwen 397B, Llama 70B, gpt-oss and a vision model, EU-hosted but slow",
        "anon": True,
        "tiers": {
            "big": [r"Qwen3\.5-397B", r"Qwen3-Coder-30B", r"gpt-oss-120b",
                    r"Qwen3\.8-27B", r"Meta-Llama-3_3-70B"],
            "small": [r"Qwen3\.5-9B", r"Mistral-Small-3", r"gpt-oss-20b", r"Mistral-Nemo"],
            "vision": [r"Qwen2\.5-VL-72B", r"Qwen3\.8-27B", r"Qwen3\.6-27B"],
        },
    },
    "gemini": {
        "label": "Google Gemini",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "env": "GEMINI_API_KEY", "prefix": "AIza",
        "signup": "https://aistudio.google.com/apikey",
        "blurb": "Gemini Flash free tier, strong with images",
        "tiers": {
            "big": [r"^gemini-flash-latest$", r"^gemini-\d+(\.\d+)?-flash$"],
            "small": [r"^gemini-flash-lite-latest$", r"^gemini-\d+(\.\d+)?-flash-lite$"],
            "vision": [r"^gemini-flash-latest$", r"^gemini-\d+(\.\d+)?-flash$"],
        },
    },
    "zai": {
        "label": "Z.ai (GLM)",
        "url": "https://api.z.ai/api/paas/v4",
        "env": "ZHIPU_API_KEY", "prefix": "",
        "signup": "https://z.ai",
        "blurb": "GLM direct; the -flash models are free",
        # Z.ai has no model listing endpoint, and only these are free.
        "static": ["glm-4.7-flash", "glm-4.5-flash", "glm-4.6v-flash"],
        "tiers": {
            "big": [r"^glm-4\.\d+-flash$"],
            "small": [r"^glm-4\.\d+-flash$"],
            "vision": [r"^glm-4\.\d+v-flash$"],
        },
    },
    "groq": {
        "label": "Groq",
        "url": "https://api.groq.com/openai/v1",
        "env": "GROQ_API_KEY", "prefix": "gsk_",
        "signup": "https://console.groq.com/keys",
        "blurb": "fastest replies, free tier with per-minute limits",
        "tiers": {
            "big": [r"^openai/gpt-oss-120b$", r"^qwen/qwen3\.\d+-27b$",
                    r"^llama-3\.3-70b-versatile$"],
            "small": [r"^openai/gpt-oss-20b$", r"^llama-3\.1-8b-instant$"],
            "vision": [r"^qwen/qwen3\.\d+-27b$"],
        },
    },
    "cerebras": {
        "label": "Cerebras",
        "url": "https://api.cerebras.ai/v1",
        "env": "CEREBRAS_API_KEY", "prefix": "csk-",
        "signup": "https://cloud.cerebras.ai",
        "blurb": "very fast gpt-oss-120b, free daily token allowance",
        "tiers": {
            "big": [r"^gpt-oss-120b$", r"^qwen-3\.\d+-27b$"],
            "small": [r"^gpt-oss-120b$"],
            "vision": [r"^qwen-3\.\d+-27b$"],
        },
    },
    "mistral": {
        "label": "Mistral",
        "url": "https://api.mistral.ai/v1",
        "env": "MISTRAL_API_KEY", "prefix": "",
        "signup": "https://console.mistral.ai/api-keys",
        "blurb": "Devstral coding models on the free experiment tier",
        "tiers": {
            "big": [r"^devstral-latest$", r"^devstral-medium-latest$",
                    r"^mistral-medium-latest$"],
            "small": [r"^devstral-small-latest$", r"^mistral-small-latest$"],
            "vision": [r"^mistral-medium-latest$", r"^pixtral-large-latest$"],
        },
    },
    "ollama": {
        "label": "Ollama (local)",
        "url": OLLAMA + "/v1",
        "env": "", "prefix": "",
        "signup": "https://ollama.com/download",
        "blurb": "runs on this machine: private, no key, no rate limit",
        "local": True,
        "tiers": {
            "big": [r"^agx-coder$", r"^qwen3-coder:30b$", r"^gpt-oss:20b$",
                    r"^qwen3:3[02]b$", r"^deepseek-r1:32b$"],
            "small": [r"^agx-coder-fast$", r"^qwen2\.5-coder:7b$", r"^qwen3:8b$",
                      r"^llama3\.2$"],
            "vision": [r"^agx-vision$", r"^qwen3-vl:8b$", r"^llama3\.2-vision:11b$",
                       r"^nemotron3:33b$"],
        },
    },
}

# Provider order per tier. Big work goes to the strongest free models first,
# small work to the fastest, and images stay on this machine when Ollama can
# see them.
ORDER = {
    "big": ["nvidia", "openrouter", "github", "gemini", "huggingface", "zai", "mistral",
            "chatanywhere", "groq", "cerebras", "ovh", "ollama"],
    "small": ["groq", "cerebras", "nvidia", "gemini", "github", "openrouter", "huggingface",
              "mistral", "zai", "chatanywhere", "ovh", "ollama"],
    "vision": ["ollama", "nvidia", "gemini", "github", "huggingface", "ovh", "zai",
               "openrouter", "groq", "mistral"],
}

# What are you using the model for? Each kind of work prefers different models
# than the plain small/big/vision tiers. A task lists model-name patterns in
# order of fitness, matched across every ready provider; the first live match
# wins, and the tier is the fallback when none match. `order` overrides which
# providers are tried first (cyber work goes local first, so nothing about a
# target leaves the machine).
RECOMMEND = {
    "code": {
        "label": "Code", "tier": "big",
        "blurb": "Writing, refactoring and fixing code",
        "prefer": [r"qwen3?-?coder(?!.*fast)", r"north-mini-code", r"devstral",
                   r"glm-5(?!.*flash)", r"gpt-4\.1$", r"laguna-s", r"kimi-k\d",
                   r"nemotron-3-ultra", r"llama-4-maverick", r"gpt-oss", r"agx-coder(?!-fast)"],
    },
    "reason": {
        "label": "Reasoning", "tier": "big",
        "blurb": "Hard problems, maths and multi-step planning",
        "prefer": [r"deepseek-r\d", r"nemotron-3-ultra", r"glm-5(?!.*flash)",
                   r"gpt-4\.1$", r"kimi-k\d", r"deepseek-v\d(?!.*flash)", r"qwen3:3\d",
                   r"nemotron-3-super"],
    },
    "vision": {
        "label": "Images", "tier": "vision",
        "blurb": "Reading screenshots, diagrams and photos",
        "prefer": [],                       # the vision tier already ranks these
    },
    "cyber": {
        "label": "Cyber / CTF", "tier": "big",
        "order": ["ollama", "nvidia", "openrouter", "github", "gemini", "zai", "groq", "cerebras", "mistral"],
        "blurb": "Security analysis, reverse engineering and CTF, kept local first "
                 "so nothing about a target leaves this machine",
        "prefer": [r"agx-coder(?!-fast)", r"deepseek-r\d", r"qwen3-coder",
                   r"glm-5(?!.*flash)", r"nemotron-3-ultra", r"kimi-k\d", r"deepseek-v\d(?!.*flash)"],
    },
    "quick": {
        "label": "Quick", "tier": "small",
        "blurb": "Short answers, classifying and quick edits",
        "prefer": [],                       # the small tier is already the fast pick
    },
    "write": {
        "label": "Writing", "tier": "big",
        "blurb": "Docs, summaries and plain-language explanations",
        "prefer": [r"gpt-4\.1$", r"glm-5(?!.*flash)", r"kimi-k\d", r"nemotron-3-ultra",
                   r"gpt-4o$", r"gpt-oss", r"llama3"],
    },
}


# The opencode worker's model, best first, matched against `opencode models`.
# Keyed providers lead because they are stronger; opencode's own free models
# need no key at all, and the local alias is the last resort.
OPENCODE_PREFS = [
    r"^nvidia/z-ai/glm-5(\.\d+)?$",
    r"^nvidia/moonshotai/kimi-k\d$",
    r"^nvidia/qwen/qwen3-coder-480b",
    r"^nvidia/qwen/qwen3\.5-397b",
    r"^zai/glm-4\.\d+-flash$",
    r"^openrouter/nvidia/nemotron-3-ultra.*:free$",
    r"^openrouter/poolside/laguna-s-.*:free$",
    r"^opencode/big-pickle$",
    r"^opencode/glm-5(\.\d+)?-free$",
    r"^opencode/nemotron-3-ultra-free$",
    r"^opencode/deepseek-v\d.*-free$",
    r"^opencode/laguna-s-.*-free$",
    r"^opencode/north-mini-code-free$",
    r"^opencode/.*-free$",
    r"^ollama/agx-coder$",
]


class ProviderError(Exception):
    """A provider could not serve this call; the caller tries the next one."""


class RateLimited(ProviderError):
    def __init__(self, msg, retry_after=120):
        super().__init__(msg)
        self.retry_after = retry_after


# ------------------------------------------------------------------ state
def _state(name):
    os.makedirs(STATE, exist_ok=True)
    return os.path.join(STATE, name)


def _load(name, default):
    try:
        return json.load(open(_state(name), encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save(name, data):
    tmp = _state(name) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, _state(name))


def cooling(prov):
    """Seconds left on a provider's rest after a 429 or a rejected key."""
    left = _load("provider-rest.json", {}).get(prov, 0) - time.time()
    return max(0, int(left))


def cool(prov, seconds):
    d = _load("provider-rest.json", {})
    d[prov] = time.time() + seconds
    _save("provider-rest.json", d)


# A provider's model list says what it offers, not what answers. A model that
# returns 404 is listed but not served; one that sends nothing back is queued
# out on the free tier. Either one rests so the next model is tried instead,
# and comes back on its own when the rest runs out.
DEAD_REST = 24 * 3600
STUCK_REST = 30 * 60
BUSY_REST = 5 * 60            # the model's own host is rate limiting


def resting(prov, model):
    """Seconds left on a model's rest after it failed to answer."""
    left = _load("model-rest.json", {}).get(f"{prov}/{model}", 0) - time.time()
    return max(0, int(left))


_REST_LOCK = threading.Lock()


def rest_model(prov, model, seconds):
    with _REST_LOCK:          # parallel probes fail together
        now = time.time()
        d = {k: v for k, v in _load("model-rest.json", {}).items() if v > now}
        d[f"{prov}/{model}"] = now + seconds
        try:
            _save("model-rest.json", d)
        except OSError:
            pass              # another process is writing it; the next failure retries


def natkey(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s.lower())]


def key_for(prov):
    env = PROVIDERS[prov]["env"]
    return os.environ.get(env, "").strip() if env else ""


def key_hint(prov):
    k = key_for(prov)
    return f"{k[:6]}…{k[-4:]}" if len(k) > 12 else ("set" if k else "")


# ------------------------------------------------------------------ http
def _request(url, payload=None, key="", stream=False):
    data = json.dumps(payload).encode() if payload is not None else None
    # Cloudflare-fronted APIs (Groq among them) answer Python's default
    # User-Agent with 403 "error code: 1010" before looking at the key.
    headers = {"Content-Type": "application/json", "User-Agent": "AGX/1.0",
               "HTTP-Referer": "https://localhost/agx", "X-Title": "AGX"}
    if stream:
        headers["Accept"] = "text/event-stream"
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return urllib.request.Request(url, data=data, headers=headers)


def _http_error(e):
    body = e.read().decode(errors="replace")[:300]
    if e.code == 429:
        try:
            wait = int(e.headers.get("Retry-After", "120"))
        except ValueError:
            wait = 120
        return RateLimited(f"rate limited: {body}", wait)
    return ProviderError(f"HTTP {e.code}: {body}")


def _http(url, payload=None, key="", timeout=180):
    try:
        with urllib.request.urlopen(_request(url, payload, key), timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise _http_error(e)
    except Exception as e:  # timeouts, DNS, refused connections
        raise ProviderError(f"unreachable: {e}")


def _stream_iter(url, payload, key, idle):
    """POST a chat request with stream=True and yield answer text as it arrives.

    The timeout applies to each wait for data, not to the whole reply: a model
    that never starts fails after `idle` seconds, while a long answer that keeps
    arriving is never cut off. Reasoning deltas are dropped. <think> blocks are
    stripped only when the reply is collected (not mid-stream)."""
    started, t0 = False, time.time()
    try:
        with urllib.request.urlopen(_request(url, dict(payload, stream=True), key, True),
                                    timeout=idle) as r:
            if "event-stream" not in r.headers.get("Content-Type", ""):
                yield _text(json.loads(r.read().decode())["choices"][0]["message"]["content"])
                return
            for raw in r:
                # OpenRouter sends ": PROCESSING" keep-alives while a model is
                # queued; they keep the socket busy but are not an answer.
                if not started and time.time() - t0 > idle:
                    raise ProviderError(f"timed out: no answer started in {idle}s")
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if chunk.get("error"):
                    err = chunk["error"]
                    code = err.get("code") if isinstance(err, dict) else None
                    raise ProviderError(f"HTTP {code} in stream: {json.dumps(err)[:200]}")
                for ch in chunk.get("choices") or []:
                    delta = ch.get("delta") or {}
                    piece = delta.get("content") or ""
                    started = started or bool(piece) or any(
                        delta.get(k) for k in ("reasoning", "reasoning_content"))
                    if piece:
                        yield piece
    except urllib.error.HTTPError as e:
        raise _http_error(e)
    except ProviderError:
        raise
    except (KeyError, IndexError, TypeError) as e:
        raise ProviderError(f"unexpected reply: {e}")
    except Exception as e:  # timeouts, DNS, refused connections
        raise ProviderError(f"unreachable: {e}")


def _stream(url, payload, key, idle):
    """Collect a streamed reply into one string (think-blocks stripped)."""
    return _text("".join(_stream_iter(url, payload, key, idle)))


# ------------------------------------------------------------------ ollama
def ollama_up():
    try:
        _http(OLLAMA + "/api/version", timeout=3)
        return True
    except ProviderError:
        return False


def ollama_models():
    """Installed models as {name: capabilities}, minus the deny list."""
    try:
        tags = _http(OLLAMA + "/api/tags", timeout=5).get("models", [])
    except ProviderError:
        return {}
    out = {}
    for m in tags:
        name = m.get("name", "")
        if not name or OLLAMA_DENY.search(name):
            continue
        caps = m.get("capabilities")
        if caps is None:      # older servers do not report capabilities
            try:
                caps = _http(OLLAMA + "/api/show", {"model": name}, timeout=10).get(
                    "capabilities", ["completion"])
            except ProviderError:
                caps = ["completion"]
        out[re.sub(r":latest$", "", name)] = caps
    return out


def ollama_setup(verbose=True):
    """Create the AGX aliases: same weights, a context window agents can use."""
    have = ollama_models()
    made = []
    for alias, spec in ALIASES.items():
        bases, ctx = spec["bases"], spec["ctx"]
        base = next((b for b in bases if re.sub(r":latest$", "", b) in have), None)
        if not base:
            if verbose:
                print(f"  {alias}: skipped, none of {', '.join(bases)} is installed")
            continue
        try:
            _http(OLLAMA + "/api/create",
                  {"model": alias, "from": base, "parameters": {"num_ctx": ctx},
                   "stream": False}, timeout=300)
        except ProviderError:
            # Older servers only accept a Modelfile through the CLI.
            mf = f"FROM {base}\nPARAMETER num_ctx {ctx}\n"
            r = subprocess.run(["ollama", "create", alias, "-f", "-"], input=mf,
                               capture_output=True, text=True)
            if r.returncode != 0:
                if verbose:
                    print(f"  {alias}: failed - {r.stderr.strip()[:200]}")
                continue
        made.append(alias)
        if verbose:
            print(f"  {alias} <- {base}  (context {ctx})")
    return made


# ------------------------------------------------------------------ catalog
def ready(prov):
    cfg = PROVIDERS[prov]
    if cfg.get("local"):
        return ollama_up()
    if cfg.get("anon"):           # a free endpoint that takes no key
        return True
    return bool(key_for(prov))


def models(prov, refresh=False):
    """Model ids a provider serves for free right now (cached for six hours)."""
    cfg = PROVIDERS[prov]
    if cfg.get("local"):
        return list(ollama_models())
    if "static" in cfg:
        return list(cfg["static"])
    cache = _load(f"models-{prov}.json", {})
    if not refresh and cache.get("t", 0) > time.time() - 6 * 3600 and cache.get("ids"):
        return cache["ids"]
    data = _http(cfg["url"] + "/models", key=key_for(prov), timeout=30).get("data", [])
    ids = []
    for m in data:
        mid = re.sub(r"^models/", "", m.get("id", ""))
        if not mid:
            continue
        if cfg.get("free_only"):
            p = m.get("pricing") or {}
            try:
                if float(p.get("prompt", 1)) or float(p.get("completion", 1)):
                    continue
            except (TypeError, ValueError):
                continue
        ids.append(mid)
    _save(f"models-{prov}.json", {"t": time.time(), "ids": ids})
    return ids


def resolve(prov, tier, avail=None):
    """The best model of one provider for a tier, or None."""
    if avail is None:
        try:
            avail = models(prov)
        except ProviderError:
            return None
    caps = ollama_models() if PROVIDERS[prov].get("local") else None
    for pat in PROVIDERS[prov]["tiers"][tier]:
        hits = [m for m in avail if re.search(pat, m, re.I)]
        if caps is not None:
            need = "vision" if tier == "vision" else "completion"
            hits = [m for m in hits if need in caps.get(m, [])]
        hits = [m for m in hits if not resting(prov, m)]
        if hits:
            return sorted(hits, key=natkey, reverse=True)[0]
    return None


def pick(tier, exclude=()):
    """(provider, model) for a tier: first ready provider that can serve it."""
    forced_p = os.environ.get("ROUTER_PROVIDER", "").strip().lower()
    forced_m = os.environ.get("ROUTER_MODEL", "").strip()
    if forced_p in PROVIDERS and forced_m and forced_p not in exclude:
        return forced_p, forced_m
    for prov in ORDER[tier]:
        if prov in exclude or (forced_p and prov != forced_p):
            continue
        if not ready(prov) or cooling(prov):
            continue
        m = resolve(prov, tier)
        if m:
            return prov, m
    return None, None


def _where(prov):
    return "on this machine" if PROVIDERS[prov].get("local") else f"via {PROVIDERS[prov]['label']}"


def recommend(task):
    """The model to use for a kind of work, as (provider, model, reason).

    Matches the task's preferred model names across every ready provider in
    order, skipping ones that are resting, and falls back to the task's tier
    when nothing matches. reason is a short sentence for the person."""
    spec = RECOMMEND.get(task)
    if not spec:
        return None, None, ""
    tier = spec["tier"]
    order = spec.get("order") or ORDER[tier]
    for prov in order:
        if prov not in PROVIDERS or not ready(prov) or cooling(prov):
            continue
        try:
            avail = models(prov)
        except ProviderError:
            continue
        caps = ollama_models() if PROVIDERS[prov].get("local") else None
        need = "vision" if tier == "vision" else "completion"
        for pat in spec["prefer"]:
            hits = [m for m in avail if re.search(pat, m, re.I)
                    and not resting(prov, m)
                    and (caps is None or need in caps.get(m, []))]
            if hits:
                model = sorted(hits, key=natkey, reverse=True)[0]
                return prov, model, f"{spec['blurb']}. Runs {_where(prov)}."
    prov, model = pick(tier)
    if not prov:
        return None, None, f"No model is free for {spec['label'].lower()} right now."
    return prov, model, f"{spec['blurb']}. Runs {_where(prov)}."


# ------------------------------------------------------------------ calls
def _text(content):
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    content = content or ""
    return re.sub(r"<think>.*?</think>\s*", "", content, flags=re.S).strip()


def _ollama_chat(model, messages, temperature, timeout):
    # The native API lets each request set its context window; the
    # OpenAI-compatible one is stuck at the server default of 4096 tokens.
    msgs = []
    for m in messages:
        c = m["content"]
        if isinstance(c, list):
            mm = {"role": m["role"],
                  "content": "".join(p.get("text", "") for p in c if p.get("type") == "text")}
            imgs = [p["image_url"]["url"].split(",", 1)[-1]
                    for p in c if p.get("type") == "image_url"]
            if imgs:
                mm["images"] = imgs
            msgs.append(mm)
        else:
            msgs.append({"role": m["role"], "content": c})
    r = _http(OLLAMA + "/api/chat",
              {"model": model, "messages": msgs, "stream": False,
               "options": {"temperature": temperature, "num_ctx": 16384}},
              timeout=timeout)
    return _text((r.get("message") or {}).get("content"))


def call(prov, model, messages, temperature=0.2, timeout=None):
    """One chat call. For cloud providers `timeout` is the longest wait for the
    next piece of the streamed reply. A model that is not served (404) or never
    starts answering (timeout, 5xx) rests, so the next pick skips it."""
    if PROVIDERS[prov].get("local"):
        return _ollama_chat(model, messages, temperature, timeout or 900)
    try:
        return _stream(PROVIDERS[prov]["url"] + "/chat/completions",
                       {"model": model, "messages": messages, "temperature": temperature},
                       key_for(prov), timeout or 90)
    except RateLimited as e:
        # "Provider returned error" is the model's own host limiting it, not
        # this account: rest the model and let the provider's others answer.
        if "Provider returned error" in str(e) or "HTTP 429 in stream" in str(e):
            rest_model(prov, model, BUSY_REST)
            raise ProviderError(f"model busy upstream: {str(e)[:160]}")
        raise
    except ProviderError as e:
        s = str(e)
        if re.search(r"HTTP (404|410)\b", s) or (re.search(r"HTTP 403\b", s) and model in s):
            rest_model(prov, model, DEAD_REST)      # listed but not served to us
        elif re.search(r"timed out|HTTP 5\d\d\b", s):
            rest_model(prov, model, STUCK_REST)
        elif re.search(r"HTTP 429 in stream", s):
            rest_model(prov, model, BUSY_REST)
        raise


def _ollama_stream_iter(model, messages, temperature, timeout):
    """Yield text as a local Ollama model generates it (native streaming API)."""
    msgs = []
    for m in messages:
        c = m["content"]
        if isinstance(c, list):
            mm = {"role": m["role"],
                  "content": "".join(p.get("text", "") for p in c if p.get("type") == "text")}
            imgs = [p["image_url"]["url"].split(",", 1)[-1]
                    for p in c if p.get("type") == "image_url"]
            if imgs:
                mm["images"] = imgs
            msgs.append(mm)
        else:
            msgs.append({"role": m["role"], "content": c})
    body = {"model": model, "messages": msgs, "stream": True,
            "options": {"temperature": temperature, "num_ctx": 16384}}
    try:
        with urllib.request.urlopen(_request(OLLAMA + "/api/chat", body), timeout=timeout) as r:
            for raw in r:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                piece = (d.get("message") or {}).get("content") or ""
                if piece:
                    yield piece
                if d.get("done"):
                    break
    except urllib.error.HTTPError as e:
        raise _http_error(e)
    except Exception as e:
        raise ProviderError(f"unreachable: {e}")


def call_stream(prov, model, messages, temperature=0.2, timeout=None):
    """Yield a reply token by token, resting the model on failure like call()."""
    try:
        if PROVIDERS[prov].get("local"):
            yield from _ollama_stream_iter(model, messages, temperature, timeout or 900)
        else:
            yield from _stream_iter(PROVIDERS[prov]["url"] + "/chat/completions",
                                    {"model": model, "messages": messages,
                                     "temperature": temperature}, key_for(prov), timeout or 90)
    except RateLimited as e:
        if "Provider returned error" in str(e) or "HTTP 429 in stream" in str(e):
            rest_model(prov, model, BUSY_REST)
            raise ProviderError(f"model busy upstream: {str(e)[:160]}")
        raise
    except ProviderError as e:
        s = str(e)
        if re.search(r"HTTP (404|410)\b", s) or (re.search(r"HTTP 403\b", s) and model in s):
            rest_model(prov, model, DEAD_REST)
        elif re.search(r"timed out|HTTP 5\d\d\b", s):
            rest_model(prov, model, STUCK_REST)
        elif re.search(r"HTTP 429 in stream", s):
            rest_model(prov, model, BUSY_REST)
        raise


def chat_stream(messages, tier="small", prefer=None, temperature=0.2):
    """Stream a reply from the best free model. Yields ('model', 'prov/model')
    once the answer starts, then ('delta', text) pieces, then ('done', '') or
    ('error', message). A model that fails before its first token falls through
    to the next; once text has started, an error ends the stream."""
    seq, tried, errors = [], [], []
    if prefer and prefer[0] in PROVIDERS and ready(prefer[0]):
        seq.append(tuple(prefer))
    for _ in range(8):
        if seq:
            prov, model = seq.pop(0)
        else:
            prov, model = pick(tier, exclude=tried)
            if not prov:
                break
            tried.append(prov)
        started = False
        try:
            for piece in call_stream(prov, model, messages, temperature):
                if not started:
                    started = True
                    yield "model", f"{prov}/{model}"
                yield "delta", piece
            if started:
                yield "done", ""
                return
            errors.append(f"{prov}/{model}: empty")
        except RateLimited as e:
            cool(prov, min(max(e.retry_after, 30), 3600))
            errors.append(f"{prov}: rate limited")
        except ProviderError as e:
            if started:                       # already streaming: stop here
                yield "error", f"the reply was cut off: {str(e)[:120]}"
                return
            errors.append(f"{prov}/{model}: {str(e)[:100]}")
            if re.search(r"HTTP 40[123]\b", str(e)):
                cool(prov, 3600)
    yield "error", "no model could answer (" + ("; ".join(errors[-3:]) or "none free") + ")"


def chat(messages, tier="small", temperature=0.2):
    """Run a chat on the best free model for the tier. A model that fails to
    answer rests and the same provider's next model is tried; a provider that
    is rate limited, rejects the key or is down is skipped for the next one.
    Returns (text, "provider/model")."""
    tried, errors, strikes = [], [], {}
    for _ in range(12):
        prov, model = pick(tier, exclude=tried)
        if not prov:
            break
        try:
            return call(prov, model, messages, temperature), f"{prov}/{model}"
        except RateLimited as e:
            cool(prov, min(max(e.retry_after, 30), 3600))
            errors.append(f"{prov}: rate limited")
            tried.append(prov)
        except ProviderError as e:
            errors.append(f"{prov}/{model}: {str(e)[:120]}")
            if resting(prov, model):
                continue                  # this model only: try the provider's next one
            if re.search(r"HTTP 40[123]\b", str(e)):
                cool(prov, 3600)          # rejected key or no credit: rest an hour
                tried.append(prov)
            else:
                # A dropped connection is retried once before the provider
                # is given up on for this call.
                strikes[prov] = strikes.get(prov, 0) + 1
                if strikes[prov] > 1:
                    tried.append(prov)
    detail = "; ".join(errors) or "no provider is configured for this tier"
    raise ProviderError(f"no {tier} model available ({detail})")


# ------------------------------------------------------------------ opencode
def opencode_bin():
    for p in (os.environ.get("OPENCODE_BIN", ""),
              os.path.expanduser("~/.opencode/bin/opencode")):
        if p and os.access(p, os.X_OK):
            return p
    from shutil import which
    return which("opencode") or ""


def opencode_models(refresh=False):
    """Models the opencode CLI can use with the keys present (cached 10 min,
    and refreshed whenever the keys or the generated config change)."""
    exe = opencode_bin()
    if not exe:
        return []
    cfg = opencode_config()
    sig = (",".join(sorted(p for p in PROVIDERS if key_for(p)))
           + f":{int(os.path.getmtime(cfg))}")
    cache = _load("opencode-models.json", {})
    if (not refresh and cache.get("sig") == sig
            and cache.get("t", 0) > time.time() - 600 and cache.get("ids")):
        return cache["ids"]
    env = dict(os.environ, OPENCODE_CONFIG=cfg)
    try:
        out = subprocess.run([exe, "models"], capture_output=True, text=True,
                             timeout=90, stdin=subprocess.DEVNULL, env=env).stdout
    except (OSError, subprocess.TimeoutExpired):
        return cache.get("ids", [])
    ids = [l.strip() for l in out.splitlines() if re.match(r"^[\w.-]+/\S+$", l.strip())]
    if ids:
        _save("opencode-models.json", {"t": time.time(), "sig": sig, "ids": ids})
    return ids


def settings():
    try:
        return json.load(open(SETTINGS, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def opencode_pick():
    """The dashboard's choice if it is still available, else the best one."""
    avail = opencode_models()
    chosen = (settings().get("model_opencode") or "").strip()
    if chosen and chosen in avail:
        return chosen
    if chosen and avail:
        print(f"model_opencode '{chosen}' is not available, choosing one",
              file=sys.stderr)
    for pat in OPENCODE_PREFS:
        hits = [m for m in avail if re.search(pat, m)]
        if hits:
            return sorted(hits, key=natkey, reverse=True)[0]
    return ""


def opencode_config():
    """Write the opencode worker's config and return its path.

    Permissions come from opencode.base.json (destructive commands are denied
    even under --auto). The Ollama provider lists only the agent aliases: plain
    models would run with a truncated 4096-token context.
    """
    try:
        cfg = json.load(open(os.path.join(BASE, "opencode.base.json"), encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {"$schema": "https://opencode.ai/config.json"}
    have = ollama_models() if ollama_up() else {}
    local = {a: {"name": f"{a} (local)", "limit": {"context": s["ctx"], "output": 8192}}
             for a, s in ALIASES.items()
             if s["agent"] and a in have and "tools" in have[a]}
    if local:
        cfg.setdefault("provider", {})["ollama"] = {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Ollama (local)",
            "options": {"baseURL": OLLAMA + "/v1"},
            "models": local,
        }
    path = _state("opencode.json")
    new = json.dumps(cfg, indent=2)
    try:
        same = open(path, encoding="utf-8").read() == new
    except OSError:
        same = False
    if not same:
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            f.write(new)
        os.replace(path + ".tmp", path)
    return path


# ------------------------------------------------------------------ status
def status():
    rows = []
    for prov, cfg in PROVIDERS.items():
        r = {"id": prov, "label": cfg["label"], "env": cfg["env"],
             "signup": cfg["signup"], "blurb": cfg["blurb"],
             "local": bool(cfg.get("local")), "ready": ready(prov),
             "key": key_hint(prov), "cooling": cooling(prov), "models": {}, "error": ""}
        if r["ready"]:
            try:
                avail = models(prov)
                r["count"] = len(avail)
                for t in TIERS:
                    r["models"][t] = resolve(prov, t, avail) or ""
            except ProviderError as e:
                r["error"] = str(e)[:160]
        if cfg.get("local"):
            have = ollama_models() if r["ready"] else {}
            r["aliases"] = [a for a in ALIASES if a in have]
            r["missing_aliases"] = [a for a, s in ALIASES.items()
                                    if a not in have and any(
                                        re.sub(r":latest$", "", b) in have
                                        for b in s["bases"])]
        rows.append(r)
    tiers = {}
    for t in TIERS:
        p, m = pick(t)
        tiers[t] = {"provider": p or "", "model": m or ""}
    oc = {"installed": bool(opencode_bin()), "model": "", "count": 0, "free": 0}
    if oc["installed"]:
        ids = opencode_models()
        oc.update(model=opencode_pick(), count=len(ids),
                  free=sum(1 for m in ids if m.startswith("opencode/")),
                  available=ids)
    return {"providers": rows, "tiers": tiers, "opencode": oc}


def print_status():
    s = status()
    print("Providers")
    for r in s["providers"]:
        mark = "ready " if r["ready"] else "  -   "
        extra = r["key"] or ("" if r["local"] else f"add {r['env']}  ({r['signup']})")
        if r["cooling"]:
            extra += f"  resting {r['cooling']}s"
        if r["error"]:
            extra += f"  ERROR {r['error'][:80]}"
        print(f"  {mark} {r['label']:16} {extra}")
        if r["ready"]:
            for t in TIERS:
                if r["models"].get(t):
                    print(f"           {t:6} {r['models'][t]}")
        if r.get("missing_aliases"):
            print(f"           run: providers.py ollama-setup  (creates "
                  f"{', '.join(r['missing_aliases'])})")
    print("\nTiers right now")
    for t, v in s["tiers"].items():
        print(f"  {t:6} {v['provider'] + '/' + v['model'] if v['provider'] else 'nothing available'}")
    oc = s["opencode"]
    print("\nopencode worker")
    if not oc["installed"]:
        print("  not installed: curl -fsSL https://opencode.ai/install | bash")
    else:
        print(f"  model {oc['model'] or 'none available'}   "
              f"({oc['count']} models, {oc['free']} free without a key)")


def test(prov):
    if prov not in PROVIDERS:
        sys.exit(f"unknown provider: {prov} (one of {', '.join(PROVIDERS)})")
    if not ready(prov):
        sys.exit(f"{prov}: not ready - " + ("is Ollama running?" if PROVIDERS[prov].get("local")
                                             else f"{PROVIDERS[prov]['env']} is not set"))
    try:
        avail = models(prov, refresh=True)
    except ProviderError as e:
        sys.exit(f"{prov}: key rejected or provider down - {e}")
    model = resolve(prov, "small", avail) or resolve(prov, "big", avail)
    if not model:
        sys.exit(f"{prov}: the key works ({len(avail)} models) but no preferred model matched")
    t = time.time()
    try:
        out = call(prov, model, [{"role": "user", "content": "Reply with exactly: OK"}])
    except ProviderError as e:
        sys.exit(f"{prov}/{model}: call failed - {e}")
    print(f"{prov}/{model}: replied {out[:40]!r} in {time.time() - t:.1f}s "
          f"({len(avail)} models available)")


if __name__ == "__main__":
    a = sys.argv[1:]
    cmd = a[0] if a else ""
    if cmd == "status":
        if "--json" in a:
            print(json.dumps(status()))
        else:
            print_status()
    elif cmd == "pick" and len(a) > 1 and a[1] in TIERS:
        p, m = pick(a[1])
        print(f"{p}/{m}" if p else "")
    elif cmd == "test" and len(a) > 1:
        test(a[1])
    elif cmd == "opencode-model":
        print(opencode_pick())
    elif cmd == "opencode-config":
        print(opencode_config())
    elif cmd == "ollama-setup":
        if not ollama_up():
            sys.exit(f"Ollama is not running at {OLLAMA}")
        print("Creating AGX aliases (they share weights with the base model):")
        ollama_setup()
        opencode_config()
    else:
        sys.exit(__doc__)
