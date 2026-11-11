"""免费大模型的平台预设：用户只需要粘贴一个 Key，地址和模型名都自动填好。

为什么这么做
------------
1. 硬编码 Key 会被反编译、被刷光额度、可能封号 —— 不能这么干
2. 让用户自己查 base_url 和 model 名太麻烦，多数人卡在这一步
3. 免费额度单家有限（429 很常见），多接几家自动切换才稳

这里只放**公开的接入信息**（地址 + 模型名），不含任何密钥。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Provider:
    key: str                      # 内部标识
    name: str                     # 界面显示名
    base_url: str
    model: str                    # 默认模型
    free_note: str                # 免费政策说明（写清额度与限制，不夸大）
    apply_url: str                # 去哪申请
    good_for: str = ""            # 适合什么
    models: tuple[str, ...] = ()  # 该平台可选模型（第一个通常是默认）
    extra: dict = field(default_factory=dict)


# 按「推荐顺序」排列：越靠前越稳定，failover 时优先用
PROVIDERS: tuple[Provider, ...] = (
    Provider(
        key="zhipu",
        name="智谱 GLM（推荐）",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        model="glm-4.7-flash",
        free_note="GLM-4.7-Flash 免费（约 30M tokens/Key，131K 上下文），能力比 4-flash 强",
        apply_url="https://open.bigmodel.cn",
        good_for="长期常驻、稳定优先",
        # 顺序即降级顺序。4.7 是免费主力，但高峰期常 429；
        # 4.6 不少账号没开通（401/1220），所以放在最后，别挡在前面白等。
        # 实测 4.5-flash / 4-flash 开通率高且响应快（<1.5s），作为首级兜底。
        models=("glm-4.7-flash", "glm-4.5-flash", "glm-4-flash",
                "glm-4-plus", "glm-4-air", "glm-4.6-flash"),
    ),
    Provider(
        key="siliconflow",
        name="硅基流动 SiliconFlow",
        base_url="https://api.siliconflow.cn/v1",
        model="Qwen/Qwen3-8B",
        free_note="新用户送 2000 万 tokens；部分小模型永久免费",
        apply_url="https://cloud.siliconflow.cn",
        good_for="备用通道、模型选择多",
        models=("Qwen/Qwen3-8B", "Qwen/Qwen2.5-7B-Instruct",
                "THUDM/GLM-4-9B-0414", "deepseek-ai/DeepSeek-V3"),
    ),
    Provider(
        key="volces",
        name="火山引擎 豆包",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        model="doubao-lite-32k",
        free_note="豆包 Lite 永久免费，每日 200 万 token。**需先在控制台手动开通模型**",
        apply_url="https://console.volcengine.com/ark",
        good_for="日配额大、高频调用",
        models=("doubao-lite-32k", "doubao-lite-128k", "doubao-pro-32k"),
    ),
    Provider(
        key="deepseek",
        name="DeepSeek 官方",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        free_note="新用户赠少量 tokens（30 天）；之后按量付费，但价格极低（百万 token 几毛钱）",
        apply_url="https://platform.deepseek.com",
        good_for="付费但便宜，写 SQL 能力强",
    ),
    Provider(
        key="custom",
        name="自定义（任意 OpenAI 兼容）",
        base_url="",
        model="",
        free_note="任何兼容 /v1/chat/completions 的服务都能接：本地 Ollama、公司内网网关（如 newapi / one-api）等",
        apply_url="",
        good_for="本地模型 / 自建网关",
        # 这里**故意留空**：留空时前端会显示自由输入框，用户能填自己网关上的模型名。
        # 曾经写死 ("qwen2.5:7b", "llama3.1:8b", "deepseek-r1:7b")（Ollama 常见名），
        # 结果下拉把输入框藏了，接自建 newapi 网关时只能选到 qwen2.5:7b，
        # 网关没这个模型 → 报 model_not_found，用户以为"API 连不上"。
        models=(),
    ),
)

BY_KEY = {p.key: p for p in PROVIDERS}


def get(key: str) -> Provider | None:
    return BY_KEY.get(key)


def detect(base_url: str) -> Provider | None:
    """根据已有 base_url 反推是哪家（用于界面回显）。"""
    url = (base_url or "").rstrip("/")
    if not url:
        return None
    for p in PROVIDERS:
        if p.base_url and url.startswith(p.base_url.rstrip("/")):
            return p
    return None


def as_list() -> list[dict]:
    """给前端用的精简结构。"""
    return [
        {
            "key": p.key,
            "name": p.name,
            "base_url": p.base_url,
            "model": p.model,
            "models": list(p.models) or ([p.model] if p.model else []),            "free_note": p.free_note,
            "apply_url": p.apply_url,
            "good_for": p.good_for,
            "free": p.key != "deepseek",      # deepseek 是低价付费，不算"免费"
        }
        for p in PROVIDERS
    ]
