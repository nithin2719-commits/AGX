#!/usr/bin/env python3
"""Backend for the dashboard's Workspace tab.

Chat with the free models, ask about an image, manage API keys and generate
images. Model choice and fallback all go through providers.py; this module
only adds what the browser needs on top of it.

API keys are written to config.env (mode 600) and are never sent back to the
browser: the key panel only learns whether a key is set.

Image generation is offered only for a provider that has produced a real
image in a test on this machine. Ollama refuses image models over its API,
so it is never offered.
"""
import base64, binascii, json, os, re, time, urllib.error, urllib.request

import providers
from providers import PROVIDERS, TIERS, ProviderError

CONFIG = providers.CONFIG
KEY_RE = re.compile(r"^[A-Za-z0-9._~+/=:-]{8,400}$")
IMAGE_RE = re.compile(r"^data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/=]+)$")
MAX_IMAGE = 8 * 1024 * 1024
SYSTEM = "You are a helpful, precise assistant. Answer concisely."
# The same default question router.py see asks.
SEE_DEFAULT = ("Describe what this image shows. If it is a user interface, list concrete "
               "problems with layout, spacing, contrast, alignment and readability.")

# Providers whose free tier may serve image generation, and how to call them.
# Nothing here is shown as working until test_image() gets a real image back.
IMAGE_GEN = {
    "nvidia": {"how": "nvidia",
               "models": ["black-forest-labs/flux.1-schnell", "black-forest-labs/flux.1-dev",
                          "stabilityai/stable-diffusion-3-medium"]},
    "gemini": {"how": "openai", "patterns": [r"^imagen-[\d.]+-(fast-)?generate"]},
}
IMAGE_NEVER = {"ollama": "Ollama refuses image models over its API on this machine."}


def _load(name):
    try:
        return json.load(open(providers._state(name), encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ------------------------------------------------------------------ overview
def state():
    """Everything the Workspace tab shows. Keys are reported as set or not set,
    never as values."""
    tests, images = _load("key-tests.json"), _load("image-tests.json")
    rows = []
    for prov, cfg in PROVIDERS.items():
        rows.append({
            "id": prov, "label": cfg["label"], "blurb": cfg["blurb"],
            "signup": cfg["signup"], "env": cfg["env"], "local": bool(cfg.get("local")),
            "prefix": cfg["prefix"], "has_key": bool(providers.key_for(prov)),
            "ready": providers.ready(prov), "cooling": providers.cooling(prov),
            "test": tests.get(prov),
            "image": ("never" if prov in IMAGE_NEVER else
                      "verified" if (images.get(prov) or {}).get("ok") else
                      "untested" if prov in IMAGE_GEN else ""),
            "image_note": IMAGE_NEVER.get(prov) or (images.get(prov) or {}).get("msg", ""),
        })
    tiers = {}
    for t in TIERS:
        p, m = providers.pick(t)
        tiers[t] = {"provider": p or "", "model": m or ""}
    return {"ok": True, "providers": rows, "tiers": tiers, "models": choices()}


def _matches(prov, tier, avail, caps=None):
    """Models of one provider that fit a tier, best pattern first, newest first."""
    out = []
    for pat in PROVIDERS[prov]["tiers"][tier]:
        hits = [m for m in avail if re.search(pat, m, re.I)]
        if caps is not None:
            need = "vision" if tier == "vision" else "completion"
            hits = [m for m in hits if need in caps.get(m, [])]
        out += [m for m in sorted(hits, key=providers.natkey, reverse=True)[:3] if m not in out]
    return out


def choices():
    """Models you can pick by name in the chat: for every ready provider, the
    models its tier patterns match (so a new glm-5.x shows up on its own)."""
    out = []
    for prov, cfg in PROVIDERS.items():
        if not providers.ready(prov):
            continue
        try:
            avail = providers.models(prov)
        except ProviderError:
            continue
        caps = providers.ollama_models() if cfg.get("local") else None
        seen = {}
        for tier in TIERS:
            for m in _matches(prov, tier, avail, caps):
                seen.setdefault(m, []).append(tier)
        out += [{"provider": prov, "label": cfg["label"], "model": m, "tiers": t}
                for m, t in seen.items()]
    return out


# ------------------------------------------------------------------ chat
def _clean_messages(messages):
    if not isinstance(messages, list) or not messages:
        raise ValueError("no message to send")
    out, total = [], 0
    for m in messages[-16:]:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            raise ValueError("bad message")
        text = str(m.get("content") or "")[:20000]
        total += len(text)
        out.append({"role": m["role"], "content": text})
    if total > 60000:
        raise ValueError("conversation too long - clear it and start again")
    if out[-1]["role"] != "user":
        raise ValueError("the last message must be yours")
    return out


def _image_part(data_url):
    m = IMAGE_RE.match(data_url or "")
    if not m:
        raise ValueError("attach a PNG, JPEG, WebP or GIF image")
    try:
        raw = base64.b64decode(m.group(2), validate=True)
    except binascii.Error:
        raise ValueError("the image is not valid base64")
    if len(raw) > MAX_IMAGE:
        raise ValueError("image too large (8 MB max)")
    return {"type": "image_url", "image_url": {"url": data_url}}


def _picked(value, image):
    """A model chosen by name as (provider, model), or (None, reason)."""
    prov, _, model = value.partition("|")
    if prov not in PROVIDERS or not model:
        return None, "unknown model"
    if not providers.ready(prov):
        return None, f"{PROVIDERS[prov]['label']} is not ready - check its key"
    try:
        avail = providers.models(prov)
    except ProviderError as e:
        return None, f"{prov}: {e}"
    if model not in avail:
        return None, f"{prov} no longer offers {model} - pick another"
    if image:
        caps = providers.ollama_models() if PROVIDERS[prov].get("local") else None
        if model not in _matches(prov, "vision", avail, caps):
            return None, "vision"
    return prov, model


def chat(body):
    tier = body.get("tier", "big")
    if tier not in TIERS:
        return {"ok": False, "msg": "unknown tier"}
    try:
        msgs = _clean_messages(body.get("messages"))
        image = body.get("image")
        if image:
            # An image goes to the vision tier, like router.py see, unless the
            # model picked by name can see images itself.
            tier = "vision"
            last = msgs[-1]
            last["content"] = [{"type": "text", "text": last["content"].strip() or SEE_DEFAULT},
                               _image_part(image)]
    except ValueError as e:
        return {"ok": False, "msg": str(e)}
    msgs = [{"role": "system", "content": SYSTEM}] + msgs
    note = ""
    if body.get("model"):
        prov, model = _picked(str(body["model"]), image)
        if prov:
            try:
                return {"ok": True, "reply": providers.call(prov, model, msgs) or "(empty reply)",
                        "model": f"{prov}/{model}", "tier": "picked by name"}
            except providers.RateLimited as e:
                providers.cool(prov, min(max(e.retry_after, 30), 3600))
                return {"ok": False, "msg": f"{prov}/{model} is rate limited - try Auto"}
            except ProviderError as e:
                return {"ok": False, "msg": f"{prov}/{model}: {str(e)[:300]}"}
        if model != "vision":
            return {"ok": False, "msg": model}
        note = "the picked model cannot see images, so the vision tier answered"
    try:
        text, model = providers.chat(msgs, tier=tier)
    except ProviderError as e:
        return {"ok": False, "msg": f"{e} - add a key in the panel, or start Ollama"}
    return {"ok": True, "reply": text or "(empty reply)", "model": model,
            "tier": f"{tier} tier" + (f"; {note}" if note else "")}


# ------------------------------------------------------------------ keys
def save_key(body):
    prov = body.get("provider", "")
    cfg = PROVIDERS.get(prov)
    if not cfg or not cfg["env"]:
        return {"ok": False, "msg": "this provider does not take a key"}
    key = (body.get("key") or "").strip()
    if not KEY_RE.match(key):
        return {"ok": False, "msg": "that does not look like an API key"}
    if cfg["prefix"] and not key.startswith(cfg["prefix"]):
        return {"ok": False, "msg": f"{cfg['label']} keys start with {cfg['prefix']}"}
    var = cfg["env"]
    try:
        lines = open(CONFIG, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        lines = []
    # Replace any earlier line for this variable, commented or not, as
    # set-key.sh does. KEY_RE keeps quotes, $ and spaces out, so the line
    # stays safe for the scripts that source config.env.
    old = re.compile(rf"^\s*#?\s*(export\s+)?{var}=")
    lines = [l for l in lines if not old.match(l)] + [f'export {var}="{key}"']
    tmp = CONFIG + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG)
    os.environ[var] = key
    providers.cool(prov, 0)        # a rejected old key must not bench the new one
    return {"ok": True, "msg": f"saved {var} to config.env - press TEST to check it"}


def test_key(body):
    """One tiny call, and the model this provider would serve for each tier."""
    prov = body.get("provider", "")
    if prov not in PROVIDERS:
        return {"ok": False, "msg": "unknown provider"}
    cfg = PROVIDERS[prov]
    res = {"ok": False, "tiers": {}, "at": time.strftime("%Y-%m-%d %H:%M")}
    if not providers.ready(prov):
        res["msg"] = ("Ollama is not running" if cfg.get("local")
                      else f"no key saved for {cfg['label']}")
    else:
        try:
            avail = providers.models(prov, refresh=True)
            res["tiers"] = {t: providers.resolve(prov, t, avail) or "" for t in TIERS}
            model = res["tiers"]["small"] or res["tiers"]["big"]
            if not model:
                res["msg"] = f"the key works ({len(avail)} models) but no preferred model matched"
            else:
                t0 = time.time()
                out = providers.call(prov, model, [{"role": "user", "content": "Reply with exactly: OK"}],
                                     timeout=180)
                res.update(ok=True, msg=f"{model} replied {out[:40]!r} in {time.time() - t0:.1f}s "
                                        f"({len(avail)} models available)")
        except ProviderError as e:
            res["msg"] = f"key rejected or provider down: {str(e)[:200]}"
    tests = _load("key-tests.json")
    tests[prov] = res
    providers._save("key-tests.json", tests)
    return dict(res, msg=f"{cfg['label']}: {res['msg']}")


# ------------------------------------------------------------------ images
def _post_json(url, payload, key, timeout=180):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
        "Content-Type": "application/json", "Accept": "application/json",
        # Cloudflare-fronted APIs refuse Python's default User-Agent.
        "User-Agent": "AGX/1.0", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise ProviderError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")
    except Exception as e:
        raise ProviderError(f"unreachable: {e}")


def _decode_image(resp):
    """Pull one image out of the reply shapes these APIs use, and refuse
    anything that does not decode to a real PNG, JPEG or WebP file."""
    b64 = ""
    if isinstance(resp, dict):
        arts = resp.get("artifacts") or []
        data = resp.get("data") or []
        if arts and isinstance(arts[0], dict):
            b64 = arts[0].get("base64", "")
        elif data and isinstance(data[0], dict):
            b64 = data[0].get("b64_json", "")
        else:
            b64 = resp.get("image", "")
    try:
        raw = base64.b64decode(b64 or "", validate=True)
    except binascii.Error:
        raw = b""
    if raw.startswith(b"\x89PNG"):
        mime = "image/png"
    elif raw.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise ProviderError("the reply did not contain an image")
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def _image_models(prov):
    spec = IMAGE_GEN[prov]
    if "models" in spec:
        return list(spec["models"])
    avail = providers.models(prov)
    return sorted({m for m in avail for pat in spec["patterns"] if re.search(pat, m)},
                  key=providers.natkey, reverse=True)


def _generate(prov, model, prompt):
    key = providers.key_for(prov)
    if IMAGE_GEN[prov]["how"] == "nvidia":
        if "stable-diffusion" in model:
            payload = {"prompt": prompt, "cfg_scale": 5, "aspect_ratio": "1:1",
                       "seed": 0, "steps": 40, "negative_prompt": ""}
        else:
            payload = {"prompt": prompt, "width": 1024, "height": 1024, "seed": 0,
                       "steps": 4 if "schnell" in model else 30}
        resp = _post_json("https://ai.api.nvidia.com/v1/genai/" + model, payload, key)
    else:
        resp = _post_json(PROVIDERS[prov]["url"] + "/images/generations",
                          {"model": model, "prompt": prompt, "n": 1,
                           "response_format": "b64_json"}, key)
    return _decode_image(resp)


def test_image(body):
    prov = body.get("provider", "")
    if prov in IMAGE_NEVER:
        return {"ok": False, "msg": IMAGE_NEVER[prov]}
    if prov not in IMAGE_GEN:
        return {"ok": False, "msg": "this provider has no free image model"}
    if not providers.key_for(prov):
        return {"ok": False, "msg": f"save a {PROVIDERS[prov]['label']} key first"}
    errors, res = [], None
    try:
        candidates = _image_models(prov)
    except ProviderError as e:
        candidates, errors = [], [str(e)[:160]]
    for model in candidates:
        try:
            img = _generate(prov, model, "a small red cube on a plain white table, studio light")
            res = {"ok": True, "model": model, "msg": f"{model} returned an image",
                   "image": img, "at": time.strftime("%Y-%m-%d %H:%M")}
            break
        except ProviderError as e:
            errors.append(f"{model.split('/')[-1]}: {str(e)[:160]}")
    if res is None:
        res = {"ok": False, "msg": "; ".join(errors) or "no image model offered for this key",
               "at": time.strftime("%Y-%m-%d %H:%M")}
    saved = _load("image-tests.json")
    saved[prov] = {k: v for k, v in res.items() if k != "image"}
    providers._save("image-tests.json", saved)
    return res


def make_image(body):
    prov = body.get("provider", "")
    prompt = (body.get("prompt") or "").strip()[:2000]
    if not prompt:
        return {"ok": False, "msg": "describe the image first"}
    ok = _load("image-tests.json").get(prov) or {}
    if not ok.get("ok") or prov not in IMAGE_GEN:
        return {"ok": False, "msg": "that provider has not passed an image test"}
    try:
        return {"ok": True, "image": _generate(prov, ok["model"], prompt),
                "model": f"{prov}/{ok['model']}", "msg": "image ready"}
    except ProviderError as e:
        return {"ok": False, "msg": f"{prov}: {e}"}


ACTIONS = {"ws_state": lambda b: state(), "ws_chat": chat, "ws_key_save": save_key,
           "ws_key_test": test_key, "ws_image_test": test_image, "ws_image": make_image}
