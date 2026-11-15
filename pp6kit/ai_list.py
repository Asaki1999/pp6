"""用一句话配置列表：显示哪些列、怎么排序、用哪些视图。

对应 JeecgBoot 的 onlreport（报表列配置）+ onlchart（智能推导 X/Y）。
AI 只决定「看什么、按什么排、用什么形式看」，XML 由 gen_tpl 渲染。
"""
from __future__ import annotations

import json

from . import fieldtypes
from .ai_bo import _extract_json, _fast_cfg
from .model import Bo, ListDef
from .nlq import llm

SYSTEM = """你是 PowerProject V6 的实施顾问，负责配置列表页。

根据业务对象字段，决定列表怎么展示。只输出 JSON，不要解释，不要 markdown。

JSON 结构：
{
  "columns": ["applyNo", "project", "amount", "status"],
  "sort": {"field": "createTime", "dir": "desc"},
  "views": ["grid", "board"],
  "boardGroup": "status",
  "titleField": "applyNo",
  "gantt": {"start": "startDate", "end": "dueDate", "progress": ""}
}

字段说明：
- columns：列表默认显示的列，4~7 个。挑最需要一眼看到的，不要把所有字段都塞进去
- sort：默认排序字段和方向（desc 最新在前）
- views：要生成哪些视图，从 grid / board / gantt / chart 里选
    · grid   表格，几乎总要有
    · board  看板，有状态/类型字段且需要按它分类时加
    · gantt  甘特，有开始+结束日期时加
    · chart  图表，有可统计的维度时加（注意：系统里图表的具体样式要在界面里另配，
              导出包里只带一个空视图，所以除非用户明确要，一般不加）
- boardGroup：看板按哪个字段分列（必须是 选项/关联/人员 类型的字段）
- titleField：卡片/看板上的标题字段（一般是名称或编号）
- gantt：甘特的开始、结束、进度字段（必须是 datetime / number）

硬性规则：
1. 所有字段编码必须来自给定清单，不能编造
2. columns 里的字段**不能**是大文本或附件（显示不下）
3. 没有合适的字段就省略对应配置，不要瞎填
4. 时间类字段名通常含 date/time/date/time/start/end/due
"""

# 这几类不适合放在列表列里
_NOT_LISTABLE = ("clob", "attachment")
# 能做看板分列的字段类型：只有「选项」。
#
# 为什么排除人员/部门/关联对象——
# 「按申请人分列」= 一个人一列，「按所属项目分列」= 一个项目一列，
# 数据一多就碎成几十上百列，看板彻底不能用。
# 系统里真正适合分列的是「状态 / 类型 / 级别 / 紧急程度」这类
# 取值有限的选项字段，也就是数据类型为 dic 的那些。
_BOARDABLE = ("dic",)


def _best_board_field(bo: Bo):
    """挑最像「状态」的字段做看板分列。

    gen_tpl 里也有一套打分，但这里要更严：
    人员字段（申请人、负责人）绝不能拿来分列，一行一个人会碎成上百列。
    """
    def score(f):
        """选项字段（dic）优先——它取值有限，天然适合分列。

        关联对象（object）要看具体是什么：`项目状态` 合适，`所属项目` 不合适
        （一个项目一列，项目多了照样碎）。所以名字语义先判，类型后判。
        """
        n = f.code.lower()
        if any(k in n for k in ("state", "status")):
            return 0
        if any(k in n for k in ("stage", "phase")):
            return 1
        # 走到这里一定是 dic（_BOARDABLE 只留了 dic）
        if any(k in n for k in ("level", "grade", "priority", "urgency", "type", "kind", "category")):
            return 2
        return 3

    pool = [f for f in bo.fields if f.type_new in _BOARDABLE]
    if not pool:
        return None
    return sorted(pool, key=lambda f: (score(f), f.sn))[0]


def _pick_columns(spec: dict, bo: Bo) -> tuple[list[str], list[str]]:
    notices: list[str] = []
    by_code = {f.code: f for f in bo.fields}
    out: list[str] = []
    for c in (spec.get("columns") or []):
        code = str(c).strip()
        f = by_code.get(code)
        if f is None:
            notices.append(f"显示列 {code} 不存在，已跳过")
            continue
        if f.type_new in _NOT_LISTABLE:
            notices.append(f"字段「{f.name}」是{'大文本' if f.type_new == 'clob' else '附件'}，不适合做列表列，已跳过")
            continue
        if code not in out:
            out.append(code)

    if not out:
        # AI 没给或全被刷掉 → 按「看起来最有信息量」兜底
        out = [f.code for f in sorted(bo.fields, key=lambda x: x.sn)
               if f.type_new not in _NOT_LISTABLE][:5]
        notices.append("AI 没给出可用的显示列，已按字段顺序取前 5 个")
    return out, notices


def _norm_views(spec: dict, bo: Bo) -> tuple[list[str], dict]:
    """校验视图，缺条件的降级并说明原因。"""
    by_code = {f.code: f for f in bo.fields}
    raw = [str(v).strip() for v in (spec.get("views") or ["grid"])]
    extra: dict = {}
    views: list[str] = []

    if "grid" not in raw:
        raw.insert(0, "grid")           # 表格是基本盘，没有它列表没法用

    for v in raw:
        if v == "board":
            g = str(spec.get("boardGroup") or "").strip()
            f = by_code.get(g)
            if f is None or f.type_new not in _BOARDABLE:
                # AI 选的不能分列 → 按语义自己挑一个。
                # 挑不到就干脆不出看板：拿"所属项目"硬分列会碎成几十上百列，
                # 比没有看板更糟。
                f = _best_board_field(bo)
                if f is None:
                    extra.setdefault(
                        "boardSkip",
                        "没有适合分列的选项/状态字段，看板视图已跳过（需要先加一个「选项」类型的字段）")
                    continue
            extra["boardGroup"] = f.code
            extra.setdefault("boardNote", f"看板按「{f.name}」分列")
            views.append("board")
        elif v == "gantt":
            g = spec.get("gantt") or {}
            s = by_code.get(str(g.get("start") or ""))
            e = by_code.get(str(g.get("end") or ""))
            if s is None or s.type_new != "datetime" or e is None or e.type_new != "datetime":
                dates = [x for x in bo.fields if x.type_new == "datetime"]
                if len(dates) < 2:
                    extra.setdefault("ganttSkip", "字段里没有两个日期，甘特视图已跳过")
                    continue
                s, e = dates[0], dates[1]
            extra["gantt"] = {"start": s.code, "end": e.code,
                              "progress": str(g.get("progress") or "")}
            views.append("gantt")
        elif v == "chart":
            # 系统导出包里 chart 只有空壳，具体图表在界面里配，
            # 所以这里只保证视图存在
            extra.setdefault("chartNote", "图表视图已建好，具体样式需在系统界面里配置")
            views.append("chart")
        elif v == "grid" and "grid" not in views:
            views.append("grid")

    return views or ["grid"], extra


def build_list(spec: dict, bo: Bo) -> tuple[ListDef, list[str]]:
    columns, notices = _pick_columns(spec, bo)
    views, extra = _norm_views(spec, bo)

    sort = spec.get("sort") or {}
    sort_field = str(sort.get("field") or "").strip()
    if sort_field and bo.field_by_code(sort_field) is None:
        notices.append(f"排序字段 {sort_field} 不存在，改用默认排序")
        sort_field = ""
    if not sort_field:
        # 有创建时间就按它倒序，这是列表最自然的默认
        f = next((x for x in bo.fields if x.code in ("createTime", "create_time")), None)
        sort_field = f.code if f else ""

    title = str(spec.get("titleField") or "").strip()
    if title and bo.field_by_code(title) is None:
        title = ""

    # key/name 优先用传进来的：微调（refine）会显式传「原来的 key」，
    # 目的是**原地更新**已有的列表模板。早先这里写死成 f"{bo.code}_list"，
    # 对象一改编码，列表 key 就跟着变，导入时是「新建第二张列表」而不是更新，
    # 用户会看到两套一模一样、名字还不一样的列表（2026-11-12 用户反馈同源问题）。
    key = str(spec.get("key") or "").strip() or f"{bo.code}_list"
    name = str(spec.get("name") or "").strip() or f"{bo.name}列表"

    lst = ListDef(
        key=key,
        name=name,
        bo_code=bo.code,
        views=views,
        columns=columns,
        sort_field=sort_field,
        sort_dir=("asc" if str(sort.get("dir") or "").lower() == "asc" else "desc"),
        title_field=title,
    )

    # 看板分组 / 甘特字段：AI 选了就用，没选留给 gen_tpl 自动挑
    g = extra.get("boardGroup")
    if "board" in views and g and bo.field_by_code(g):
        lst.board_group = g
    gan = extra.get("gantt")
    if gan:
        lst.gantt_start = gan["start"]
        lst.gantt_end = gan["end"]
        lst.gantt_progress = gan.get("progress", "")
    for k in ("boardNote", "ganttSkip", "chartNote"):
        if extra.get(k):
            notices.append(extra[k])
    return lst, notices


def _compact(bo: Bo) -> list[dict]:
    return [
        {"code": f.code, "name": f.name, "type": fieldtypes.labels().get(f.type_new, f.type_new)}
        for f in bo.fields
    ]


def generate(bo: Bo, cfg: dict, timeout: int = 90) -> dict:
    """给一个业务对象配置列表。"""
    import time

    if not llm.active_channels(cfg):
        raise llm.LlmError("还没配置大模型 API Key")

    payload = {"对象名称": bo.name, "对象编码": bo.code, "字段": _compact(bo)}
    t0 = time.time()
    text, used = llm.chat_any(_fast_cfg(cfg), [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ], timeout=timeout, max_tokens=800)

    try:
        spec = _extract_json(text)
    except json.JSONDecodeError as exc:
        raise llm.LlmError(f"模型输出不是合法 JSON（{exc.msg}）") from exc

    lst, notices = build_list(spec, bo)
    return {
        "list": lst,
        "notices": notices,
        "model": used.get("model", ""),
        "seconds": round(time.time() - t0, 2),
    }


def to_dict(lst: ListDef, bo: Bo) -> dict:
    """给前端预览，结构对齐 /api/parse 的 lists。"""
    names = []
    for c in lst.columns:
        f = bo.field_by_code(c)
        names.append({"code": c, "label": f.name if f else c})
    return {
        "key": lst.key,
        "name": lst.name,
        "boCode": lst.bo_code,
        "views": lst.views,
        "columns": names,
        "sort": f"{lst.sort_field} {lst.sort_dir}".strip(),
        "titleField": lst.title_field,
        "boardGroup": lst.board_group,
        # 级联详情字段：前端下拉选完存这里，出包时原样带回
        "detailField": lst.detail_field or "",
        "gantt": ({"start": lst.gantt_start, "end": lst.gantt_end}
                  if lst.gantt_start else None),
    }
