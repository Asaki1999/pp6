#!/usr/bin/env python3
"""导出 PP6 Studio 的模型参数。

三部分内容：
  1. 生效中的模型通道 —— 读 ~/.pp6kit/nlq.json 的 llm.channels
  2. 内置平台预设     —— 读 pp6kit/nlq/providers.py 的 PROVIDERS
  3. 调用参数         —— 从 pp6kit/ai_*.py 与 nlq/llm.py 源码里抓 timeout 默认值
                         和 max_tokens 表达式（原文照录，不做二次推断）

用法：
    python tools/dump_llm_params.py                       # 终端摘要，Key 打码
    python tools/dump_llm_params.py --md out/参数.md --with-key
    python tools/dump_llm_params.py --json out/llm.json --with-key

--with-key 才会写出明文密钥；不加时一律打码。终端摘要永远打码，避免日志留痕。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pp6kit.nlq import providers as _providers  # noqa: E402

CONFIG = Path.home() / ".pp6kit" / "nlq.json"


def mask(key: str) -> str:
    """只露头 6 位与末 4 位，够辨认是哪把 Key，又不足以使用。"""
    key = str(key or "")
    if not key:
        return ""
    if len(key) <= 12:
        return "*" * len(key)
    return f"{key[:6]}...{key[-4:]}"


def load_channels() -> tuple[list[dict], dict]:
    """读本地 app 配置；返回 (通道列表, 原始 llm 段)。"""
    if not CONFIG.exists():
        return [], {}
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    llm = data.get("llm") or {}
    channels = list(llm.get("channels") or [])
    # 兼容旧格式：顶层直接写 base_url / api_key / model
    if not channels and llm.get("base_url"):
        channels = [{"base_url": llm.get("base_url"), "api_key": llm.get("api_key", ""),
                     "model": llm.get("model", ""), "enabled": True, "fallbacks": []}]
    return channels, llm


def _expr_at(text: str, start: int) -> str:
    """从 start 起取一个表达式，括号内不截断，遇到顶层逗号/换行/收尾括号停止。

    纯正则处理不了 `min(32000, _budget * 2)` 这种含逗号的表达式——按逗号切会
    只拿到 `min(32000`，所以这里做一层括号配平。
    """
    out: list[str] = []
    depth = 0
    i = start
    while i < len(text):
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:        # 到外层调用的右括号，结束
                break
            depth -= 1
        elif c in ",\n" and depth == 0:
            break
        out.append(c)
        i += 1
    return "".join(out).strip()


def scan_call_params() -> list[dict]:
    """从源码抽 timeout 默认值与 max_tokens 表达式。

    不做公式还原：直接把源码那几行照抄，避免我理解错了反而给出错的数字。
    """
    rows: list[dict] = []
    targets = sorted((ROOT / "pp6kit").glob("ai_*.py")) + [ROOT / "pp6kit" / "nlq" / "llm.py"]
    for path in targets:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(ROOT).as_posix()
        timeouts = re.findall(r"def\s+(?:\w+)\s*\([^)]*?timeout:\s*int\s*=\s*(\d+)", text, re.S)

        # max_tokens 多写在调用行内（..., timeout=timeout, max_tokens=4000)），不能锚行首。
        # 签名里的 `max_tokens: int | None = None` 因紧跟冒号不会被 `max_tokens\s*=` 匹配。
        inline: list[str] = []
        for m in re.finditer(r"max_tokens\s*=", text):
            expr = _expr_at(text, m.end())
            if expr and expr != "max_tokens":     # 过滤 `max_tokens=max_tokens` 这类纯透传
                inline.append(f"max_tokens = {expr}")

        assigns = re.findall(r"^\s*(\w*budget)\s*=\s*(.+?)\s*$", text, re.M)
        budgets = [f"{name} = {expr}" for name, expr in assigns] + inline
        rows.append({
            "file": rel,
            "timeouts": sorted(set(timeouts), key=int),
            "budgets": list(dict.fromkeys(budgets)),   # 去重保序
        })
    return rows


def build_markdown(channels: list[dict], with_key: bool) -> str:
    key_of = (lambda k: k) if with_key else mask
    lines: list[str] = []
    add = lines.append

    add("# PP6 Studio 模型参数清单")
    add("")
    add("> 由 `tools/dump_llm_params.py` 从本地配置与源码直接导出。")
    add("> 生效通道读自 `~/.pp6kit/nlq.json`，预设读自 `pp6kit/nlq/providers.py`，")
    add("> 调用参数为源码原文（未做二次推算）。")
    add("")

    add("## 一、当前生效的模型通道")
    add("")
    if not channels:
        add("本地 `~/.pp6kit/nlq.json` 里没有配置任何模型通道。")
    else:
        add(f"共 {len(channels)} 条，按优先级排列（第 1 条为主，其余仅在其不可用时启用）。")
        add("")
        add("| # | 平台标识 | 接口地址 base_url | 模型 model | 启用 | 备选模型 fallbacks | API Key |")
        add("|---|---|---|---|---|---|---|")
        for i, ch in enumerate(channels, 1):
            add("| {} | {} | `{}` | `{}` | {} | {} | {} |".format(
                i,
                ch.get("key") or "（自定义）",
                ch.get("base_url", ""),
                ch.get("model", ""),
                "是" if ch.get("enabled", True) else "否",
                "、".join(f"`{m}`" for m in (ch.get("fallbacks") or [])) or "（无）",
                f"`{key_of(ch.get('api_key',''))}`" if ch.get("api_key") else "（空）",
            ))
        add("")

        add("### 直接可用的 JSON（粘贴进另一台机器的 `~/.pp6kit/nlq.json` 的 `llm` 段）")
        add("")
        add("```json")
        payload = {"channels": [
            {k: v for k, v in ch.items() if k != "api_key"} | {"api_key": key_of(ch.get("api_key", ""))}
            for ch in channels
        ]}
        add(json.dumps(payload, ensure_ascii=False, indent=2))
        add("```")
        add("")

    add("## 二、内置平台预设")
    add("")
    add("app 界面「连接与模型」下拉框里的选项。只含公开接入信息，不含任何密钥。")
    add("")
    add("| 标识 | 名称 | 接口地址 | 默认模型 | 可选模型 | 免费政策 |")
    add("|---|---|---|---|---|---|")
    for p in _providers.PROVIDERS:
        models = "、".join(f"`{m}`" for m in p.models) if p.models else "（同默认）"
        add("| `{}` | {} | `{}` | `{}` | {} | {} |".format(
            p.key, p.name, p.base_url or "（自己填）", p.model or "（自己填）", models, p.free_note))
    add("")

    add("## 三、调用参数")
    add("")
    add("### 通用默认值")
    add("")
    add("| 参数 | 值 | 位置 |")
    add("|---|---|---|")
    add("| `temperature` | `0.0` | `pp6kit/nlq/llm.py` 的 `chat()` 签名默认值 |")
    add("| `timeout` | `60` 秒 | `pp6kit/nlq/llm.py` 的 `chat_any()` 签名默认值 |")
    add("| `retry_429` | `1` 次（间隔 1.5 秒） | `pp6kit/nlq/llm.py` 的 `chat_any()` 限流重试 |")
    add("")
    add("### 各功能模块的 timeout 与 max_tokens（源码原文）")
    add("")
    add("| 模块 | timeout 默认（秒） | max_tokens 表达式 |")
    add("|---|---|---|")
    for row in scan_call_params():
        t = "、".join(row["timeouts"]) or "—"
        b = "<br>".join(f"`{x}`" for x in row["budgets"]) or "—"
        add(f"| `{row['file']}` | {t} | {b} |")
    add("")
    add("表里显示 `—` 的模块表示**不自设预算**：`nlq/llm.py` 只把上层传入的 `max_tokens` "
        "原样透传给 HTTP 请求，由调用方决定。")
    add("")
    add("**为什么要按字段数算 max_tokens**：表单规格 48 个字段时固定 6000 token 会在第 16 个字段中间截断，"
        "`_extract_json` 只能从残骸里抢出十几个字段，表现为「一张表只生成了 18 个字段」。"
        "因此预算写成 `max(8000, min(32000, 4000 + 字段数 × 240))`，失败再把预算翻倍重试一次。")
    add("")

    add("### 降级顺序（failover）")
    add("")
    add("`pp6kit/nlq/llm.py` 的 `chat_any()` 四级降级：")
    add("")
    add("1. 同一模型被限流（HTTP 429）→ 等 1.5 秒重试一次")
    add("2. 仍然失败 → 换同平台的备选模型（按 `fallbacks` 顺序）")
    add("3. 模型未开通（401/403/404）→ 记入 `_NO_ACCESS`，后续直接跳过，不重复浪费一次请求")
    add("4. 同平台全部失败 → 换下一个平台通道")
    add("")
    add("智谱另有快模型链 `_FAST_CHAIN = (\"glm-4-flash\", \"glm-4-air\", \"glm-4.5-flash\", \"glm-4.7-flash\")`，")
    add("仅当 `base_url` 含 `bigmodel.cn` 时生效，其余平台原样使用已配置模型。")
    add("")
    return "\n".join(lines)


def build_json(channels: list[dict], with_key: bool) -> str:
    """机器可读版：channels 段可直接写入另一台机器的 nlq.json。"""
    key_of = (lambda k: k) if with_key else mask
    payload = {
        "_说明": "生效通道直接取自 ~/.pp6kit/nlq.json；presets 为 app 内置平台预设；"
                 "call_params 为源码中的 timeout 与 max_tokens 表达式原文",
        "channels": [
            {k: v for k, v in ch.items() if k != "api_key"} | {"api_key": key_of(ch.get("api_key", ""))}
            for ch in channels
        ],
        "presets": [
            {"key": p.key, "name": p.name, "base_url": p.base_url, "model": p.model,
             "models": list(p.models), "free_note": p.free_note,
             "apply_url": p.apply_url, "good_for": p.good_for}
            for p in _providers.PROVIDERS
        ],
        "call_params": scan_call_params(),
        "defaults": {"temperature": 0.0, "timeout": 60, "retry_429": 1,
                     "retry_delay_seconds": 1.5},
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main() -> int:
    ap = argparse.ArgumentParser(description="导出 PP6 Studio 模型参数")
    ap.add_argument("--md", help="Markdown 输出路径")
    ap.add_argument("--json", dest="json_path", help="JSON 输出路径")
    ap.add_argument("--with-key", action="store_true", help="写入明文 API Key（默认打码）")
    args = ap.parse_args()

    channels, _ = load_channels()
    if not channels:
        print(f"提示：{CONFIG} 里没有模型通道配置", file=sys.stderr)

    if args.md:
        Path(args.md).parent.mkdir(parents=True, exist_ok=True)
        Path(args.md).write_text(build_markdown(channels, args.with_key), encoding="utf-8")
        print(f"已写入 {args.md}")
    if args.json_path:
        Path(args.json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_path).write_text(build_json(channels, args.with_key), encoding="utf-8")
        print(f"已写入 {args.json_path}")

    # 终端摘要始终打码：避免密钥进日志
    print(f"\n生效通道 {len(channels)} 条：")
    for i, ch in enumerate(channels, 1):
        print(f"  {i}. {ch.get('base_url','')}  模型={ch.get('model','')}  "
              f"Key={mask(ch.get('api_key','')) or '（空）'}")
    print(f"内置预设 {len(_providers.PROVIDERS)} 家："
          + "、".join(p.name for p in _providers.PROVIDERS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
