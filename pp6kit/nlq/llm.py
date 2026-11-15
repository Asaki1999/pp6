"""OpenAI 兼容的大模型客户端（DeepSeek / 智谱 / 硅基流动 / 豆包 / 任何兼容网关）。

只用标准库 urllib，不给桌面包增加依赖。
支持多平台：每家一个 Key，按顺序尝试，遇到限流或失败自动切下一家。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request


class LlmError(Exception):
    pass


class RateLimited(LlmError):
    """被限流（429），应该切到下一家。"""


class NoAccess(LlmError):
    """这个模型没开通 / 无权访问（401/403）。

    和「Key 本身无效」必须分开：实测智谱账号只开通了部分模型，
    拿一个没开通的模型去试会返回 1220「您无权访问」，如果把它当成
    「Key 无效」而中断整条降级链，后面明明能用的模型就全被跳过了。
    """


def _is_model_level_denied(body: str) -> bool:
    """401 里区分「Key 有问题」还是「这个模型没开通」。"""
    text = (body or "").lower()
    return any(k in text for k in (
        "1220", "无权访问", "无权", "no permission", "not authorized",
        "model not found", "does not exist", "unavailable model",
    ))


def _auth_hint(api_key: str, body: str) -> str:
    """401/403 时给出可操作的原因，而不是干巴巴的「无效」。

    实测踩过的坑：智谱的 Key 是 `id.secret` 两段（含点号，60+ 位），
    用户从控制台复制时容易只拿到前 32 位的 id，后半段丢了，
    结果报 401，界面提示「Key 无效」，用户以为是没填进去。
    """
    k = (api_key or "").strip()
    if len(k) == 32 and all(c in "0123456789abcdef" for c in k.lower()):
        return ("这个 Key 只有 32 位且没有点号——像是**只复制了前半段**。"
                "智谱的 Key 是 `id.secret` 两段（中间有个点），请回控制台重新完整复制。")
    if not k:
        return "还没有填 API Key。在「连接与模型」里粘贴后点保存。"
    if any(c.isspace() for c in k):
        return "Key 里含有空格或换行，请重新复制粘贴（不要带前后空白）。"
    return f"Key 被平台拒绝（401）。可能是复制不完整、已被删除，或该模型未开通。平台返回：{body[:80]}"


def _endpoint(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise LlmError("未配置 API 地址")
    if base.endswith("/chat/completions"):
        return base
    # 各家 base_url 结尾不同，只有真正以版本号结尾才直接追加 /chat/completions
    #   智谱  .../api/paas/v4  → .../api/paas/v4/chat/completions
    #   火山  .../api/v3       → .../api/v3/chat/completions
    #   其余  .../v1           → .../v1/chat/completions
    if base.endswith("/v1") or re.search(r"/v\d+$", base):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def chat(base_url: str, api_key: str, model: str, messages: list[dict],
         temperature: float = 0.0, timeout: int = 60,
         max_tokens: int | None = None) -> str:
    body_payload: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    # 不传时模型会一直写到自然结束，生成类任务能拖到 100 秒；
    # 给个上限既快又省钱（SQL/JSON 这类结构化的东西用不了那么多 token）
    if max_tokens:
        body_payload["max_tokens"] = max_tokens
    payload = json.dumps(body_payload).encode("utf-8")
    req = urllib.request.Request(
        _endpoint(base_url),
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")[:400]
        if exc.code == 429:
            raise RateLimited(f"限流 429：{body[:100]}") from exc
        if exc.code in (401, 403):
            # 只是这个模型没开通 → 换个模型继续，别当成 Key 坏了
            if _is_model_level_denied(body):
                raise NoAccess(f"模型 {model} 未开通：{body[:100]}") from exc
            raise LlmError(_auth_hint(api_key, body)) from exc
        if exc.code == 404:
            raise NoAccess(f"模型 {model} 不存在或已下线：{body[:100]}") from exc
        raise LlmError(f"接口返回 {exc.code}：{body}") from exc
    except Exception as exc:
        raise LlmError(f"调用失败：{exc}") from exc

    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise LlmError(f"返回格式异常：{str(data)[:300]}") from exc


# ---------------------------------------------------------------- 多平台

def active_channels(cfg: dict) -> list[dict]:
    """取出「可用」的渠道，按配置顺序（即优先级）。

    cfg 支持两种结构：
        新：{"channels": [{"base_url","api_key","model","enabled"}...]}
        旧：{"base_url","api_key","model"}            ← 自动兼容
    """
    out = []
    for ch in (cfg.get("channels") or []):
        if ch.get("api_key") and ch.get("base_url") and ch.get("enabled", True):
            out.append(ch)
    if not out and cfg.get("api_key") and cfg.get("base_url"):
        out.append({"base_url": cfg["base_url"], "api_key": cfg["api_key"],
                    "model": cfg.get("model", "")})
    return out


# 记下「账号没开通」的模型，下次直接跳过——
# 否则每次提问都要先吃一个 429、再吃一个 401，白等好几秒。
_NO_ACCESS: set[str] = set()


def _chan_key(ch: dict) -> str:
    return f"{(ch.get('base_url') or '').rstrip('/')}|{(ch.get('api_key') or '')[-8:]}"


def chat_any(cfg: dict, messages: list[dict], timeout: int = 60,
             retry_429: int = 1, max_tokens: int | None = None) -> tuple[str, dict]:
    """依次尝试各渠道，返回 (回答, 实际用的渠道)。

    四级降级：
        1. 同一模型限流 → 等 1.5s 重试一次
        2. 还不行 → 换到同平台的其它模型（4.7 挂了降到 4.6 / 4.5 / 4-flash）
        3. 模型没开通（401/403/404）→ 记下来，直接跳下一个模型
        4. 同平台全挂 → 换下一个平台
    免费额度 429 是常态，加上各账号开通的模型不一致，不这样兜用户就只能看到报错。
    """
    channels = active_channels(cfg)
    if not channels:
        raise LlmError(
            "未配置大模型 API Key。可在「连接与模型」里选一个免费平台"
            "（智谱 GLM-4.7-Flash 免费），粘贴 Key 即可。"
        )

    errors: list[str] = []
    denied: list[str] = []
    for ch in channels:
        # 同一平台内：先试配置的模型，再依次试备选模型
        for model in _model_chain(ch):
            if f"{_chan_key(ch)}|{model}" in _NO_ACCESS:
                continue
            label = model or ch.get("base_url", "")
            for attempt in range(retry_429 + 1):
                try:
                    text = chat(ch["base_url"], ch["api_key"], model,
                                messages, timeout=timeout, max_tokens=max_tokens)
                    if not (text or "").strip():
                        # 推理模型（deepseek-flash 实测）偶尔把 max_tokens 全烧在
                        # reasoning 上，content 返回空串（2026-11-12：三次两空）。
                        # 空回答对任何调用方都没用，静默重试一次最划算。
                        text = chat(ch["base_url"], ch["api_key"], model,
                                    messages, timeout=timeout, max_tokens=max_tokens)
                    if model != ch.get("model"):
                        ch = dict(ch, model=model)     # 记录实际用的模型
                    return text, ch
                except RateLimited as exc:
                    if attempt < retry_429:
                        time.sleep(1.5)
                        continue
                    errors.append(f"{label}：限流")
                    break
                except NoAccess as exc:
                    _NO_ACCESS.add(f"{_chan_key(ch)}|{model}")
                    denied.append(label)
                    errors.append(f"{label}：未开通")
                    break
                except LlmError as exc:
                    errors.append(f"{label}：{exc}")
                    break
    if denied and len(denied) == len(errors):
        raise LlmError(
            "这个账号没开通任何可用模型 —— " + "；".join(errors[:4])
            + "。请到平台控制台开通模型，或在「模型」里改成已开通的（如 glm-4.5-flash）。"
        )
    raise LlmError("所有渠道都失败了 —— " + "；".join(errors[:4]))


def _model_chain(ch: dict) -> list[str]:
    """这个渠道该按什么顺序试哪些模型。

    配置里如果有 fallbacks（同平台备选模型），就一起排进去；
    没有就只试配置的那一个。
    """
    primary = (ch.get("model") or "").strip()
    chain = [primary] if primary else []
    for m in (ch.get("fallbacks") or []):
        m = (m or "").strip()
        if m and m not in chain:
            chain.append(m)
    return chain or [""]
