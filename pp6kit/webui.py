"""本地 Web 界面：拖入 Excel -> 预览结构 -> 生成 zip。

只用标准库，不引入 Web 框架；文件以原始字节 POST 上来（不走 multipart），
服务端保持无状态：解析和出包各自独立，前端拿着同一个 File 对象再发一次即可。
"""
from __future__ import annotations

import base64
import copy
import io
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import threading
import time
import traceback
import webbrowser
import zipfile
from datetime import datetime
from urllib.parse import parse_qs, quote, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import ai_bo, ai_form, ai_list, ai_sheet, ai_wf, blueprint, fieldtypes, objects
from . import patch as bo_patch
from .builder import build, ensure_default_workflow, ensure_project_links, package_name
from .model import (
    FORM_BUNDLE,
    FORM_CREATE,
    FORM_DETAIL,
    EMPTY_OPERATORS,
    HIDE_OPERATORS,
    OP_BY_LABEL,
    WF_KIND_BY_LABEL,
    WF_ONGOING,
    WF_START,
    WF_STATE_BY_KIND,
    Bo,
    BoField,
    DataSet,
    FilterRule,
    Form,
    FormField,
    HideRule,
    ListDef,
    Project,
)
from .gen_form import Snippets, _plan_groups, _plan_rows, _span_of
from .inspect import inspect_path
from . import modules as mod_def
from . import spec
from .model import object_name_of, value_source_key, value_source_key_notes
from .reader import read_workbook
from .template import build_workbook, demo_project
from .nlq import engine as nlq_engine
from .nlq import semantic as nlq_semantic
from .nlq import local as nlq_local
from .nlq import guard as nlq_guard
from .nlq import hybrid as nlq_hybrid
from .nlq import introspect as nlq_introspect
from .nlq import llm as nlq_llm
from .nlq import providers as nlq_providers
from .nlq import store as nlq_store
from .resources import snippets_path, static_dir, work_dir
from .validate import validate
from . import environments, diagnostics
from .nlq.explain import decorate as explain_result

STATIC = static_dir()
MAX_UPLOAD = 30 * 1024 * 1024


def _codes(headers) -> list[str] | None:
    """模块选择走请求头（文件体还是裸字节），逗号分隔。"""
    raw = (headers.get("X-Modules") or "").strip()
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return [x for x in raw.split(",") if x.strip()]
    return None


def _relations_header(headers) -> str | None:
    """用户确认的关系决策走请求头 X-Relations（JSON 稀疏覆盖）。"""
    raw = (headers.get("X-Relations") or "").strip()
    return raw or None


def _projlink_header(headers) -> str | None:
    """「关联项目」的对齐决策走请求头 X-Project-Link（单一决策头，架构 R5）。

    parse（分析）/ build / confirmed-excel 三个入口共用同一个头：以前 PRD 写的是
    X-Align，和 X-Relations 各一套会造成「两个头都带、谁优先」的口径问题。

    前端会把整段 JSON encodeURIComponent（target.name 是中文，浏览器禁止
    header 里出现非 ISO-8859-1 字符，2026-11-26 实测报错）；这里统一解码。
    对旧的未编码头，unquote 基本是无操作（编码规范里没有 % 字符），兼容。
    """
    raw = (headers.get("X-Project-Link") or "").strip()
    if not raw:
        return None
    if "%" in raw:
        from urllib.parse import unquote
        try:
            raw = unquote(raw)
        except Exception:
            pass
    return raw


def _workflow_all(headers) -> bool:
    """X-Workflow-All：流程页一个「流程驱动=是」都没有时，按固定三步纳入全部对象。"""
    raw = (headers.get("X-Workflow-All") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _object_scope(headers) -> str:
    """X-Object-Scope：同步包默认只出普通对象，流程对象走「流程模式」工作区。

    - plain（默认）：只出普通对象
    - workflow：只出流程对象（「导出流程对象包」按钮用）
    - all：两类都出（兼容旧客户端；不传就无法区分是哪个页面在请求，
      所以默认给 plain —— 这是产品行为，不是技术默认）
    """
    raw = (headers.get("X-Object-Scope") or "").strip().lower()
    return raw if raw in ("plain", "workflow", "all") else "plain"


def _tmp_file(name: str) -> Path:
    """临时文件名带上 pid + 随机后缀，**不能用固定名**。

    踩过的坑（2026-11-13，两条回归流水线同时跑）：``work_dir()`` 是固定路径
    ``$TMPDIR/pp6kit-work``，里面再写固定名 ``.build.xlsx`` / ``.upload.xlsx``，
    而每个调用都在 ``finally`` 里删它 —— A 的清理会删掉 B 正在读的文件，
    表现为随机几条用例报 ``No such file or directory`` / ``Truncated file header``。
    同样的坑在真实场景里是「桌面壳两个实例同时出包」「两个标签页同时解析」。

    后缀用 pid + 8 位随机，同一进程内并发（ThreadingHTTPServer）也不会撞。
    """
    import os
    import uuid

    stem, suffix = Path(name).stem, Path(name).suffix
    return work_dir() / f"{stem}-{os.getpid()}-{uuid.uuid4().hex[:8]}{suffix}"


def _tmp_dir(name: str) -> Path:
    """临时**目录**同样要带 pid + 随机后缀，理由与 ``_tmp_file`` 相同。

    这里比单文件更危险：固定目录 + ``rmtree`` 会让两个实例互相删掉对方刚生成的
    成果。实测（2026-11-13，冻结包开两个实例并发导出流程）：
    实例 A 生成完正要读文件，实例 B 的 rmtree 把它删了 → FileNotFoundError；
    或者 B 的 mkdir 撞上 A 刚建好的目录 → FileExistsError。
    两者都会让用户拿到 400，而不是一份 XML。

    调用方负责在用完后 ``shutil.rmtree(..., ignore_errors=True)`` 收尾。
    """
    import os
    import uuid

    path = work_dir() / f"{name}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _snippets() -> dict:
    path = snippets_path()
    if not path.exists():
        raise RuntimeError(f"缺少骨架文件 {path}，请先执行 bin/pp6 capture <样例目录>")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 数据整理


def _field_payload(bo) -> list[dict]:
    """对象卡片上的字段清单。顺序 = 预览里看到的顺序。

    「XX选择」链接字段（项目选择 / 合同选择）排第一：用户口径是这个字段要
    置顶，进表单后也是先选它、再由联动带出其它字段。只改**预览顺序**，不动
    ``sn``——sn 决定的是物理列顺序，存量包一改就等于要求线上 ALTER。
    """
    out = []
    for f in sorted(bo.fields, key=lambda x: (0 if getattr(x, "is_link", False) else 1, x.sn)):
        p = f.resolved()
        item = {
            "code": f.code,
            "name": f.name,
            "type": f.type_new,
            "fieldName": p["fieldName"],
            "length": p["length"],
            "precision": p["precision"],
            "index": p["isindex"] == "Y",
            "objectType": f.object_type,
        }
        # 表格字段（子表）：把列一并给出去，预览里能显示「N 列」并逐列核对，
        # 不然用户只能等导入后才发现列名写错了。
        if f.type_new == "table":
            item["columns"] = [
                {"name": c.name,
                 "type": c.input_type,
                 "typeLabel": fieldtypes.COLUMN_LABELS.get(c.input_type, c.input_type),
                 "widthPercent": c.width_percent,
                 "precision": c.precision,
                 "required": bool(c.required),
                 "choices": list(c.choices or [])}
                for c in (f.table_columns or [])
            ]
        out.append(item)
    return out


def _form_payload(form, bo) -> dict:
    groups = _plan_groups(form)
    name_to_field = {f.code: f for f in (bo.fields if bo else [])}
    out_groups = []
    for title, rows in groups:
        out_rows = []
        for group in rows:
            cells = []
            alone = len(group) == 1
            for ff in group:
                f = name_to_field.get(ff.name)
                cells.append({
                    "name": ff.name,
                    "label": ff.label,
                    "type": ff.field_type,
                    "required": ff.required,
                    "readonly": ff.readonly,
                    "valueSource": ff.value_source,
                    # 宽度口径必须和出包一致（gen_form._span_of），否则预览和产物不一样
                    "span": _span_of(ff, form, alone=alone),
                    "fieldType": f.type_new if f else "",
                    "missing": f is None,
                })
            out_rows.append(cells)
        out_groups.append({"title": title, "rows": out_rows,
                           "count": sum(len(r) for r in rows)})

    return {
        "key": form.key,
        "name": form.name,
        "boCode": form.bo_code,
        "boName": bo.name if bo else "",
        "layout": form.layout,
        "layoutStyle": form.layout_style,
        "width": form.resolved_width(),
        "kind": form.kind,
        "labelWidth": form.resolved_label_width(),
        "autoHidden": form.auto_hidden,
        "fieldCount": len(form.fields),
        # 布局 V1.0：分区 -> 行 -> 单元格
        "groups": out_groups,
        "rows": [row for g in out_groups for row in g["rows"]],
        "hidden": ["id", "version"] if form.auto_hidden else [],
    }


def mod_payload(proj) -> list[dict]:
    """模块清单：每个模块有几条、分别叫什么、系统里点哪儿导入。

    order 是导入顺序；blocked 表示该模块在系统里必须先有前置模块，
    页面据此把没勾前置的模块灰掉，避免用户先导表单报「对象不存在」。
    """
    counts = {
        "bo": len(proj.bos),
        "form": len(proj.forms),
        "list": len(proj.lists),
        # 三个都出自 proj.datasets，按 ds_type 分流（判据只在
        # modules.module_of_dataset 一处，别在这里另写一遍 if）：
        # 值来源单独一项，数据集不再把它算进去。
        "dataset": sum(1 for d in proj.datasets
                       if mod_def.module_of_dataset(d.ds_type) == "dataset"),
        "value": sum(1 for d in proj.datasets
                     if mod_def.module_of_dataset(d.ds_type) == "value"),
        "filter": sum(1 for d in proj.datasets
                      if mod_def.module_of_dataset(d.ds_type) == "filter"),
        "workflow": len(proj.workflows),
    }
    detail: dict[str, list[str]] = {
        "bo": [f"{b.name}（{b.code}）" for b in proj.bos],
        "form": [f"{f.name}" for f in proj.forms],
        "list": [f"{x.name}" for x in proj.lists],
        "dataset": [f"{d.name}" for d in proj.datasets
                    if mod_def.module_of_dataset(d.ds_type) == "dataset"],
        "value": [f"{d.name}" for d in proj.datasets
                  if mod_def.module_of_dataset(d.ds_type) == "value"],
        "filter": [f"{d.name}" for d in proj.datasets
                   if mod_def.module_of_dataset(d.ds_type) == "filter"],
        "workflow": [w.name for w in proj.workflows],
    }
    out = []
    for m in mod_def.MODULES:
        out.append({
            "code": m.code,
            "label": m.label,
            "folder": m.folder,
            "menu": m.menu,
            "order": m.order,
            "note": m.note,
            "zipped": m.zipped,
            "count": counts.get(m.code, 0),
            "items": detail.get(m.code, []),
            # 表单/列表在没有业务对象时导不进去；值来源/数据集/筛选器按表名取数，不强制
            "needs": ["bo"] if m.code in ("form", "list") else [],
        })
    return out


def _wf_payload(proj) -> list[dict]:
    """流程预览载荷：给左侧「流程」工作区用。

    为什么单独给一份而不是让前端读 XML：流程的节点/流转是**表格里的业务语义**
    （谁审、什么条件、多久办结），前端拿 XML 还得再解一遍双层 JSON。
    这里只挑人要看的那几列，字段名与 `Workflow/WfNode/WfEdge` 保持一一对应，
    以后加列不用改前端结构。

    任何异常都不许冒泡：流程详情挂了不该让整个 Excel 解析失败，
    所以调用方用 try/except 兜住（见 parse_excel）。
    """
    out: list[dict] = []
    kinds = {v: k for k, v in WF_KIND_BY_LABEL.items()}      # 英文 State → 中文标签
    for wf in proj.workflows:
        bo = proj.bo(wf.bo_code)

        def _field_label(code: str) -> str:
            f = bo.field_by_code(code) if bo else None
            return (f.name if f else "") or code

        nodes = []
        for i, n in enumerate(wf.nodes, start=1):
            nodes.append({
                "id": n.id or i,
                "name": n.name,
                "kind": kinds.get(n.kind, n.kind),
                # 处理人为空时留空串，前端显示「—」；不在这里编「（未填）」，
                # 免得把「真的没配」和「配了一个空名字」混成一句话
                "handler": "、".join(n.handler_names),
                "handlerType": n.handler_type,
                "formKey": n.form_key,
                "state": n.business_state or WF_STATE_BY_KIND.get(n.kind, ""),
                "goBack": "、".join(n.go_back),
                "days": n.expect_days if n.kind == WF_ONGOING else None,
            })
        edges = []
        # 用生成器同一套规则算流转：11_流程流转 留空时它会把节点自动串成直线，
        # 预览必须显示**将要生成的样子**，而不是表里的原始行数（那会是 0 条，
        # 用户会以为流程断了）。所以这里复用 _resolve_edges，不另写一套推导。
        try:
            from .gen_workflow import _resolve_edges
            resolved = _resolve_edges(wf, wf.nodes, lambda _msg: None)
        except Exception:                        # noqa: BLE001
            resolved = []
        for e in resolved:
            rules = e.get("rules") or []
            if rules:
                field, op, value, label = rules[0]
                cond = f"{_field_label(field)} {op or '等于'} {label or value}"
                if len(rules) > 1:
                    cond += f" 等 {len(rules)} 个条件"
            else:
                cond = "无条件"
            edges.append({
                "src": e["src"].name, "dst": e["dst"].name,
                "name": e["name"] or ("提交" if e["src"].kind == WF_START else "同意"),
                "condition": cond,
                "order": e.get("order", 0),
            })
        out.append({
            "name": wf.name,
            "boCode": wf.bo_code,
            "boName": bo.name if bo else "",
            "active": wf.is_active,
            # 自动兜底的「开始→审批→通过」，前端加个标记，别让人以为是自己配的
            "auto": wf.auto,
            "nodeCount": len(wf.nodes),
            # 算的是「将要生成」的流转数，不是表里的行数（空表会自动串线，见上）
            "edgeCount": len(edges),
            "nodes": nodes,
            "edges": edges,
        })
    return out


def parse_excel(data: bytes, projlink_header: str | None = None) -> dict:
    """解析一份填写表，返回预览用的 view model。

    projlink_header：X-Project-Link 请求头。解析页每个对象卡片上的「关联项目」
    开关就是靠它生效的（勾选 → 前端带这个头重新 POST /api/parse）。None = 只按
    Excel 的「关联项目」列走，与加开关之前逐字节相同。
    """
    tmp = _tmp_file(".upload.xlsx")
    tmp.write_bytes(data)
    try:
        proj, read_errors, stats = read_workbook(tmp)
    finally:
        tmp.unlink(missing_ok=True)

    # 与 build 保持一致：解析阶段就展开「标准全套」，预览看到的即最终产出
    from . import blueprint
    from .model import table_name_for
    from .validate import Issue

    # 自动补齐的提示（编码/表名是按名称推出来的、对象编码被归一到唯一对象）
    # 必须一起返回：用户填了名字就出包，但得知道「编码不是你填的那个」，
    # 否则导入后对不上自己以为的表名，只能去库里翻。
    notices = list(proj.auto_notes)
    for bo in proj.bos:
        if not bo.tbl_name:
            bo.tbl_name = table_name_for(bo.code)
    # ---- 顺序很重要，四条约束同时成立（主理人 2026-11-14 复审）：
    #   ① project_align 必须在 apply **之前**算：apply 会把字段改名，而前端的
    #      决策头（X-Project-Link）用的正是**对齐前**的编码；
    #   ② relationships 也在 apply 之前算：对齐会把「项目状态」变成指向
    #      project_state 的 object 字段，跑到后面会被 R2 规则当成
    #      「新对象该挂到 project_state 下」的假建议；
    #   ③ apply 必须在 validate / fieldcheck / 渲染之前：预览的字段编码要与
    #      出包产物一致（预览=产物），否则对象卡片还是 projectState、页签却说
    #      会变成 stateId，两边对不上；
    #   ④ apply 在 ensure_project_links 之前：链接字段由 builder 补，
    #      apply 只负责「对齐 + 改名 + 重写引用」。
    from . import projlink as _projlink

    # 解析页每张对象卡片上的「关联项目」开关（决策头里的 linked）必须在 analyze
    # **之前**落到模型：analyze 是按 bo.project_link 决定「哪些对象进对齐清单」的，
    # 晚一步的话刚勾上的对象在页签里根本不会出现（用户会以为开关没生效）。
    # 这里刻意不传 project 做 code 校验：Excel 里的对象可能刚被改名/删掉，一个过期
    # code 不该让整次解析 400 —— apply_linked 会把它降级成一条「已忽略」提示。
    decisions = None
    target = _projlink.project_target()
    if projlink_header:
        decisions = _projlink.parse_decisions(projlink_header)
        # 目标对象解析放在 apply_linked 之前：它可能连库核对名称/抽快照，
        # 解析不了要让整个请求明确报错，不能静默退回「项目」去对齐错对象。
        target = _resolve_link_target(decisions)
        proj.link_target = target
        notices.extend(_projlink.apply_linked(proj, decisions, target))
    else:
        proj.link_target = target

    try:
        project_align = _projlink.analyze(proj, decisions=decisions, target=target)
    except Exception as exc:                     # noqa: BLE001
        project_align = {"status": "unavailable", "notice": f"项目对齐分析失败：{exc}",
                         "snapshotSource": "unavailable", "snapshotSourceLabel": "无",
                         "warnings": [], "summary": {}, "items": []}

    # 所属关系识别：只给建议，默认不建立，用户确认后才生效。
    # 识别失败绝不影响解析结果本身。
    from . import relations as _relations
    try:
        relationships = _relations.analyze(proj)
    except Exception:
        relationships = {"status": "unavailable", "notice": "关系识别失败",
                         "parentOptions": [], "items": []}

    # 预览应用**全部**对齐项（精确同名 + 相似，2026-11-14 用户口径：勾了
    # 「关联项目」就照项目定义原样覆盖，不再逐条问）；决策头里的 align 只剩
    # 向后兼容的「显式 false = 不对齐」。差异明细由上面的 analyze 算好后原样
    # 返回给前端（projectAlign.diffs），弹窗只是事后对照，不影响出包。
    align_errors: list[Issue] = []

    def _align_error(message: str) -> None:
        """记一条对齐错误，同一件事只记一次（apply 与 ensure_project_links 都会报）。"""
        for got in align_errors:
            if got.message == message or message.endswith(got.message):
                return
        align_errors.append(Issue("error", "项目对齐", message))

    def _report_align_failure(message: str) -> None:
        """把对齐阶段的 ValueError 降级成校验错误，同一件事只报一条。

        「对象 project 不能关联自己」这类问题 validate 里有更具体的说法
        （「不能勾『关联项目』：自己关联自己没有意义」），这里就不再重复占一行，
        否则用户看到两条一模一样意思的错误，不知道该改哪个（主理人复验）。
        """
        if "不能关联自己" in message:
            return
        _align_error(message)

    try:
        # 带 decisions：与出包走同一个函数，预览里看到的编码/长度就是包里那份。
        # 开关只改 project_link（已在上面 apply_linked 落过），这里不会重复加字段。
        _projlink.apply(proj, decisions, target=proj.link_target or target)
    except ValueError as exc:
        # 对齐失败不能把整个解析变成 500——用户还要看别的校验结果去修表
        _report_align_failure(str(exc))

    notices.extend(blueprint.expand(proj))
    # 预览必须与出包一致（本函数开头那句「预览看到的即最终产出」）：
    # 链接字段与标准全套表单里的「所属项目」控件只在 ensure_project_links 里补，
    # 预览不补的话用户看不到它会多出一个字段。
    try:
        notices.extend(ensure_project_links(proj))
    except ValueError as exc:
        # 链接字段补不出来（自关联 / project_id_ 被占）同样降级成校验错误，
        # 页面照常给出其余结果，用户改完表再上传
        _report_align_failure(str(exc))
    # 与 build() 同一处逻辑：不填流程的对象自动补「开始→审批→通过」，
    # 否则预览页（左侧「流程」工作区）会显示「没有流程」，跟出包结果不一致。
    notices.extend(
        f"自动生成默认流程：{line}" for line in ensure_default_workflow(proj)
    )
    # 预览=产物：出包时每个对象都会多一份「自身的值来源」（见
    # blueprint.ensure_self_value_sources）。预览不建的话，勾选树里「值来源」
    # 永远是 0 项，用户以为这个模块没有内容。
    notices.extend(blueprint.ensure_self_value_sources(proj))
    stats = {
        "业务对象": len(proj.bos),
        "字段": sum(len(b.fields) for b in proj.bos),
        "表单": len(proj.forms),
        "表单控件": sum(len(f.fields) for f in proj.forms),
        "列表": len(proj.lists),
        # 选择器单独一项（实例表是 6.1_值来源）：「数据集」把它们算进去的话，
        # 用户对着「数据集 3」去 6_数据集 数只会数到 1 行。
        # 键用「选择器」而不是「值来源」：模块叫选择器，一行实例才叫「xx值来源」
        # （用户 2026-11-20 口径，逐字对齐勾选树的标签）。
        "数据集": sum(1 for d in proj.datasets
                      if mod_def.module_of_dataset(d.ds_type) == "dataset"),
        "选择器": sum(1 for d in proj.datasets
                      if mod_def.module_of_dataset(d.ds_type) == "value"),
        "筛选器": sum(1 for d in proj.datasets
                      if mod_def.module_of_dataset(d.ds_type) == "filter"),
        "流程": len(proj.workflows),
    }
    issues = ([Issue("error", "读取", e) for e in read_errors] + align_errors
              + validate(proj))

    # 字段体检：跨对象/跨表单找疑似重复、类型冲突、候选值不一致。
    # 单独一栏，不进 issues —— 它不影响出包，只是帮人核对。
    from . import fieldcheck
    field_findings = fieldcheck.check(proj)

    # 流程预览：坏了也不许拖垮整包解析，降级成空数组 + 一条提示
    try:
        workflows = _wf_payload(proj)
    except Exception as exc:                     # noqa: BLE001
        workflows = []
        notices.append(f"流程预览生成失败：{exc}")

    return {
        "ok": True,
        # 本次对齐的目标对象（项目 / 用户手输的其它业务对象）：前端据此换文案、
        # 决定要不要继续追问「上传对象 zip」。
        "linkTarget": (proj.link_target.to_dict()
                       if getattr(proj, "link_target", None) is not None else None),
        # 是不是本工具的填写模板（看页签名，不看有没有填内容）：
        # 「模板但空着」要在界面上提示去 1_业务对象 填；「不是模板」交给 AI 识别。
        "looksTemplate": any(name in proj.sheet_names for name in spec.COLUMNS),
        "stats": stats,
        "modules": mod_payload(proj),
        "workflows": workflows,
        "fieldChecks": fieldcheck.to_payload(field_findings),
        "fieldCheckWarn": sum(1 for f in field_findings if f.level == "warn"),
        "settings": {
            "tenantId": proj.tenant_id,
            "filePrefix": proj.file_prefix,
            "moduleId": proj.module_id,
            "typeId": proj.type_id,
            "outDir": proj.out_dir,
        },
        "objects": [
            {
                "code": bo.code,
                "name": bo.name,
                "tbl": bo.tbl_name,
                "boType": bo.bo_type,
                "moduleId": bo.module_id,
                "typeId": bo.type_id,
                "withLog": bo.with_log,
                # 前端要区分「这一条在同步包里」还是「在流程对象包里」：
                # 流程模式的对象不随同步包出，界面得说清楚它去哪了
                "flowDriven": bool(bo.flow_driven),
                # 勾了「关联项目」的对象卡片上给一个 chip，一眼看出它会多出
                # 「所属项目」字段并被对齐
                "projectLink": bool(bo.project_link),
                "fields": _field_payload(bo),
            }
            for bo in proj.bos
        ],
        "forms": [_form_payload(f, proj.bo(f.bo_code)) for f in proj.forms],
        "lists": [
            {
                "key": lst.key,
                "name": lst.name,
                "boCode": lst.bo_code,
                "views": lst.views,
                "columns": [
                    (bo.field_by_code(c).name if (bo := proj.bo(lst.bo_code)) and bo.field_by_code(c) else c)
                    for c in lst.columns
                ],
                "sort": f"{lst.sort_field} {lst.sort_dir}".strip(),
                "tree": lst.tree,
            }
            for lst in proj.lists
        ],
        "datasets": [
            # module：这份数据源归哪个模块（dataset / value / filter）。
            # 前端拿它分色/分组，别在 JS 里再按 type 字符串判一遍——
            # 判据只有 modules.module_of_dataset 一处。
            {"key": ds.key, "name": ds.name, "table": ds.from_table,
             "type": ds.ds_type, "module": mod_def.module_of_dataset(ds.ds_type),
             "auto": bool(ds.auto_generated),
             "results": ds.result_text()}
            for ds in proj.datasets
        ],
        "issues": [
            {"level": i.level, "where": i.where, "message": i.message} for i in issues
        ],
        "notes": notices,
        "relationships": relationships,
        # 项目字段对齐（view model 契约见架构 §2.4，前端零业务规则）
        "projectAlign": project_align,
        "errorCount": sum(1 for i in issues if i.level == "error"),
        "warnCount": sum(1 for i in issues if i.level == "warn"),
    }


def build_zip(data: bytes, codes: list[str] | None = None,
              relations_header: str | None = None,
              object_scope: str = "all",
              projlink_header: str | None = None,
              workflow_all: bool = False) -> tuple[bytes, str]:
    """codes：要出包的模块；None/空 = 全部。
    relations_header：X-Relations 请求头（稀疏决策）。None = 不覆盖，按 Excel 原样。
    projlink_header：X-Project-Link 请求头（关联项目/对齐决策）。None = 只按自动判定。
    object_scope：出哪一类业务对象（按「选择模式」分）：
        all      全部，兼容旧调用（CLI / 回归）
        plain    只出普通对象——同步包用这个
        workflow 只出流程对象（连带它的表单/列表/数据集）——「流程模式」工作区用这个

    流程对象在系统里是流程模式（bo XML ``mode="workflow"``），要和流程定义
    配套导入，所以从同步包里分出去单独出一包。
    """
    tmp_xlsx = _tmp_file(".build.xlsx")
    tmp_xlsx.write_bytes(data)
    work = work_dir() / f".run-{datetime.now().strftime('%H%M%S%f')}"
    try:
        proj, read_errors, stats = read_workbook(tmp_xlsx)
        from .validate import Issue

        # 用户确认的关系：解析请求头 → 合并进 Project → 再校验、出包。
        # parse_decisions 非法会抛 ValueError（路由层转 400），不静默忽略。
        from . import relations as _relations
        rel_notices: list[str] = []
        if relations_header:
            decisions = _relations.parse_decisions(relations_header)
            if decisions:
                rel_notices = _relations.apply_confirmations(proj, decisions)

        # 关联项目：对齐必须发生在 validate 之前（对齐改名后若还留着旧编码，
        # validate 会报一堆「控件引用不存在的字段」假错）、slice_by_mode 之前
        # （切片是浅拷贝，Bo 实例共享）、build 之前（生成器读最终模型）。
        from . import projlink as _projlink
        align_report: dict = {}
        target = _projlink.project_target()
        proj.link_target = target
        if projlink_header is not None or any(
                getattr(b, "project_link", False) for b in proj.bos):
            align_decisions = _projlink.parse_decisions(projlink_header, proj)
            target = _resolve_link_target(align_decisions)
            proj.link_target = target
            align_report = _projlink.apply(proj, align_decisions, target=target)

        # 流程页没有勾「流程驱动」时，后端按固定三步把全部对象纳入流程对象包；
        # 必须在 validate 之前改，后面的流程字段/字典/校验才按流程对象走。
        scope = (object_scope or "all").strip().lower()
        # 流程模式发的是 X-Object-Scope=all + X-Workflow-All=1：
        # 必须先把对象标成流程对象，再按 all 出包；只在 scope=workflow 时标
        # 会得到“zip 里还是普通对象”的假流程包。
        if workflow_all:
            for bo in proj.bos:
                bo.flow_driven = True

        issues = [Issue("error", "读取", e) for e in read_errors] + validate(proj)
        errors = [i for i in issues if i.level == "error"]
        if errors:
            raise ValueError("存在校验错误，已中止：\n" + "\n".join(str(e) for e in errors[:20]))

        # 校验按**整份 Excel** 走（用户看到的问题要是完整的），切分只影响出包。
        if scope in ("plain", "workflow"):
            from .builder import slice_by_mode
            proj = slice_by_mode(proj, scope == "workflow")
            if not proj.bos:
                if scope == "workflow":
                    raise ValueError("这份 Excel 里没有可纳入流程对象包的业务对象："
                                     "先到「1_业务对象」填一行")
                raise ValueError("这份 Excel 里的业务对象全是流程模式，同步包里没有可出的"
                                 "内容：流程对象请到左侧「流程模式」工作区导出")

        res = build(proj, _snippets(), work, codes=codes, db_align=True)
        if rel_notices:
            res.notices.extend(rel_notices)
        for line in align_report.get("notices") or []:
            # 快照不可用这类「降级说明」即使一条都没对齐也要让用户看到
            if line not in res.notices:
                res.notices.append(line)
        if not res.deliverables:
            raise ValueError("选中的模块没有可生成的内容")

        # 只勾了「流程」时，deliverables 里只有一个 .xml：再往下走会把它丢进
        # zipfile.ZipFile(src) → BadZipFile，前端只看到「File is not a zip file」。
        # 这里提前拦住并说清楚该怎么拿（流程是裸 XML，系统的流程导入是另一个入口）。
        if not any(d.module == "all" or mod_def.of(d.module).zipped
                   for d in res.deliverables):
            raise ValueError("流程模块不再单独导出：请到「流程模式」导出流程对象包；"
                             "本工具不生成流程 XML")

        lines = [
            "PowerProject 同步包生成报告",
            f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "读取概况: " + "  ".join(f"{k}={v}" for k, v in stats.items() if v),
            "",
        ]
        if align_report and not align_report.get("noop", True):
            _s = align_report.get("summary") or {}
            # 一行汇总（PRD P1-2 要求 zip comment 里能一眼看到做过对齐）
            _title = "项目对齐" if target.is_project else f"{target.name}对齐"
            lines.insert(4, f"{_title}: 新增 {_s.get('linkFields', 0)} / "
                            f"对齐 {_s.get('aligned', 0)} / 跳过 {_s.get('skipped', 0)}")
        if scope == "workflow":
            # 这一包是从「流程模式」工作区导出的：只有流程模式的对象，
            # 普通对象在同步包里。两边导入入口不同，写清楚免得上错菜单。
            lines += [
                "【这是流程对象包】",
                "  只含流程模式的对象（mode=workflow）及其表单 / 列表 / 数据集。",
                "  导入：管理 › 业务对象 › 导入（和普通对象同一个入口，但建议先导普通包，",
                "        流程对象的父子关系要能找到已存在的父对象）。",
                "  流程固定为 开始 → 审核 → 通过；本工具不生成流程图 / 流程 XML。",
                "",
                "  ⚠ 本包不含普通业务对象——那部分走左侧「普通模式」出包。",
                "",
            ]
        lines += [
            "【怎么导入】",
            "  下载的这个 zip 直接上传即可（结构同系统导出的 config-sync-*.zip）：",
            "      bo/001-xxx.xml            表结构",
            "      form/002-xxx.xml          表单",
            "      data_template/005-xxx.xml 列表",
            "  服务端按 bo/ form/ data_template/ 目录识别业务类型，一次全导。",
            "",
            "  ⚠ 不要把里面的文件拆出来单独传、也不要再套一层文件夹——",
            "     服务端认的是这一层的目录名，动了就报「缺少业务类型目录」。",
            "",
            "【包内清单】",
        ]
        mods = [d for d in res.deliverables if d.module != "all"]
        for i, d in enumerate(mods, 1):
            lines.append(f"  {i}. {d.label:<12} {len(d.items)} 项   →  {d.menu}")
            for a in d.items:
                # 流程对象在系统里是「流程模式」(mode="workflow")，普通对象是
                # 普通模式。报告里标出来，导出后不用解包也能确认这个对象带上
                # 审批入口、流程 XML 能挂上它。
                mark = ""
                if d.module == "bo":
                    bo = proj.bo(a.key)
                    if bo is not None and bo.flow_driven:
                        mark = "  (流程模式)"
                lines.append(f"       · {a.kind} {a.key}{mark}")
        if any(d.module == "workflow" for d in res.deliverables):
            lines.append("")
            lines.append("  ⚠ 流程固定为 开始 → 审核 → 通过；本工具不再生成流程 XML。")
        if res.notices:
            lines += ["", "提示:"] + [f"  - {n}" for n in dict.fromkeys(res.notices)]
        if align_report and not align_report.get("noop", True):
            # 独立小节：用户要能从报告里逐条看到「哪个字段对齐成了什么、哪些跳过了」
            lines += ["", ("项目对齐:" if target.is_project else f"{target.name}对齐:")] + [
                f"  {x}" for x in _projlink.report_lines(align_report, target)[1:]]
        warns = [i for i in issues if i.level == "warn"]
        if warns:
            lines += ["", "警告:"] + [f"  - {i}" for i in warns]
        report = "\n".join(lines)

        # 交付包结构：必须和系统自己导出的同步包**同构**。
        #
        # 线上实测踩了两个坑（2026-10-28，两次报错分别暴露一个限制）：
        #
        # 坑1  外层套中文目录 `业务对象/001-业务对象.zip`
        #      → ConfigSyncBizType.fromKey 把目录名当同步类型，抛
        #        「不支持的同步对象类型: 业务对象」
        # 坑2  外层平铺多个 zip（001-业务对象.zip / 001-表单.zip ...）
        #      → 报「同步包文件缺少业务类型目录: 001-业务对象.zip」
        #        服务端把每个 zip 的名字当成业务类型目录去找，找不到就失败
        #
        # 而系统自己导出、并且在下表里显示「成功」的包长这样（config-sync-*.zip）：
        #     bo/001-test_case_base_category.xml
        #     bo/002-sjzcscd.xml
        #     form/004-project_create_start.xml
        #     data_template/005-customer_list.xml
        #   —— 单层 zip，里面直接是裸 XML，按英文目录 bo/ form/ data_template/
        #      区分业务类型，序号跨目录连续。
        #
        # 所以这里**只输出这一个同构的包**，不再混放各模块 zip。
        # 报告文字写进 zip 注释：包内只允许出现 bo/ form/ data_template/ 这三类
        # 目录，多放一个 txt 都可能被服务端判成非法条目。
        allp = next((d for d in res.deliverables if d.module == "all"), None)
        src = allp.path if allp else (next((d.path for d in res.deliverables), None))
        if src is None:
            raise ValueError("没有可交付的包")
        # 重写一遍以挂上注释（直接拷字节没法加 comment）。
        # 2026-11-11 逆向系统「发布 ZIP」产物确认：原生包的 comment 格式是
        # `CONFIG_SYNC_NOTE:<发布说明>`（发布工作区里填的说明原文）。
        # 我们的生成报告挂同样的前缀——导入端若解析 comment 能读到报告，
        # 不解析也无害，格式与系统原生一致。
        buf = io.BytesIO()
        with zipfile.ZipFile(src) as zin, \
                zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                zout.writestr(info, zin.read(info.filename))
            zout.comment = ("CONFIG_SYNC_NOTE:" + report).encode("utf-8")
        # 下载文件名要能看出是干什么的：{业务名}-导入包-{日期}.zip
        # （以前是 config-sync-20261110-221234.zip，落地到下载目录完全认不出）
        stamp = datetime.now().strftime("%Y%m%d")
        name = package_name(res.biz_label or "导入包",
                            "流程对象" if scope == "workflow" else "导入包", stamp)
        return buf.getvalue(), name
    finally:
        tmp_xlsx.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)


def confirmed_excel(data: bytes, relations_header: str | None = None,
                    name: str = "", projlink_header: str | None = None
                    ) -> tuple[bytes, str]:
    """把「确认版 Excel」返回给用户：原始 Excel + 已确认的关系/对齐写回。

    不原地覆盖用户文件——确认版始终作为**新** xlsx 返回，
    下次打开这份才能继承决定；未确认的自动建议不写入。
    """
    tmp = _tmp_file(".confirm.xlsx")
    tmp.write_bytes(data)
    try:
        proj, read_errors, _stats = read_workbook(tmp)
        from .validate import Issue
        from . import relations as _relations

        rel_notices: list[str] = []
        if relations_header:
            decisions = _relations.parse_decisions(relations_header)
            if decisions:
                rel_notices = _relations.apply_confirmations(proj, decisions)

        # 与 build_zip 完全同构：先对齐（写模型），再补链接字段，最后校验。
        # 确认版里必须有这条字段行/控件行，否则用户下次打开看不到「所属项目」，
        # 再上传又会被当成「没关联项目」跑一遍。
        from . import projlink as _projlink
        target = _projlink.project_target()
        proj.link_target = target
        if projlink_header is not None or any(
                getattr(b, "project_link", False) for b in proj.bos):
            align_decisions = _projlink.parse_decisions(projlink_header, proj)
            target = _resolve_link_target(align_decisions)
            proj.link_target = target
            _projlink.apply(proj, align_decisions, target=target)
        for note in ensure_project_links(proj):
            if note not in rel_notices:
                rel_notices.append(note)
        # 确认版 Excel 是用户的下一份底稿：每个对象的「自身的值来源」也要
        # 在 6.1_值来源 里看得见（自动=是），否则他打开后改的是一份缺行，
        # 再上传回来又得重新生成一遍。
        for note in blueprint.ensure_self_value_sources(proj):
            if note not in rel_notices:
                rel_notices.append(note)

        issues = [Issue("error", "读取", e) for e in read_errors] + validate(proj)
        errors = [i for i in issues if i.level == "error"]
        if errors:
            raise ValueError("存在校验错误，已中止：\n" + "\n".join(str(e) for e in errors[:20]))

        wb = build_workbook(proj)
        buf = io.BytesIO()
        wb.save(buf)
        base = (name or "").strip()
        if base.lower().endswith(".xlsx"):
            base = base[:-5]
        label = (f"{base}_已确认.xlsx" if base else
                 f"{proj.bos[0].name if proj.bos else 'PP6'}_已确认.xlsx")
        return buf.getvalue(), label
    finally:
        tmp.unlink(missing_ok=True)


def inspect_zip(data: bytes) -> tuple[bytes, str]:
    tmp = _tmp_file(".upload-inspect.zip")
    tmp.write_bytes(data)
    try:
        proj, notes = inspect_path(tmp)
        wb = build_workbook(proj)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue(), f"反解-{tmp.stem}.xlsx"
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------- 存量包修订
# 「反解」是把包拆成 Excel 再重造一份；这里走的是另一条路：**在原包上打补丁**。
# 上传一次 zip 拿 token，之后预览/出包/导 Excel 都用 token 复用，
# 免得每次操作都把几 MB 的包重新 POST 一遍。

_PATCH_DIR = work_dir() / "patch-cache"
_PATCH_KEEP = 8


def _patch_store(data: bytes) -> str:
    import hashlib

    _PATCH_DIR.mkdir(parents=True, exist_ok=True)
    token = hashlib.sha1(data).hexdigest()[:20]
    path = _PATCH_DIR / f"{token}.zip"
    if not path.exists():
        path.write_bytes(data)
    # 只留最近几份，长跑的桌面壳不该把临时目录越撑越大
    cached = sorted(_PATCH_DIR.glob("*.zip"),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    for old in cached[_PATCH_KEEP:]:
        old.unlink(missing_ok=True)
    return token


def _patch_load(token: str) -> bytes:
    import re

    if not re.fullmatch(r"[0-9a-f]{20}", str(token or "")):
        raise ValueError("原包缓存标识无效，请重新上传 zip")
    path = _PATCH_DIR / f"{token}.zip"
    if not path.is_file():
        raise ValueError("原包缓存已过期，请重新上传 zip")
    return path.read_bytes()


def _patch_spec(payload: dict) -> tuple[bytes, dict]:
    spec = dict(payload or {})
    return _patch_load(spec.get("token")), spec


def patch_inspect(data: bytes) -> dict:
    snap = bo_patch.inspect_package(data)
    snap["token"] = _patch_store(data)
    return snap


def patch_fields(data: bytes) -> dict:
    return bo_patch.fields_from_excel(data)


def patch_dry_run(payload: dict) -> dict:
    raw, spec = _patch_spec(payload)
    return bo_patch.dry_run(raw, spec)


def patch_build(payload: dict) -> tuple[bytes, str]:
    raw, spec = _patch_spec(payload)
    body, name, _report = bo_patch.build_patch(raw, spec)
    return body, name


def patch_workbook(payload: dict) -> tuple[bytes, str]:
    raw, spec = _patch_spec(payload)
    return bo_patch.patched_workbook(raw, spec)


# ---------------------------------------------------------------- 按模块导入

# 系统导出包按模块区分：bo/ form/ data_template/ …；用户上传时经常只有一个模块，
# 也可能拿整个目录压成一个包，所以两种都要认。
_KIND_TO_MODULE = {
    "bo": "bo",
    "form": "form",
    "data_template": "data_template",   # 需要再细分列表/数据集/筛选器
    "workflow": "workflow",
}
_PROJ_CACHE: dict[str, object] = {}


def _cache_proj(data: bytes):
    """按内容哈希缓存解析结果：先 /api/parse 再 /api/import 时不用重复解析。"""
    import hashlib

    key = hashlib.sha1(data).hexdigest()
    hit = _PROJ_CACHE.get(key)
    if hit is not None:
        return hit
    tmp = _tmp_file(".cache.xlsx")
    tmp.write_bytes(data)
    try:
        proj, read_errors, _ = read_workbook(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    from . import blueprint
    from .model import table_name_for

    for bo in proj.bos:
        if not bo.tbl_name:
            bo.tbl_name = table_name_for(bo.code)
    blueprint.expand(proj)
    _PROJ_CACHE.clear()          # 只留最新一份，避免长跑进程吃内存
    _PROJ_CACHE[key] = proj
    return proj


def import_module(data: bytes, code: str,
                  projlink_header: str | None = None,
                  workflow_all: bool = False) -> tuple[bytes, str]:
    """只生成某一个模块的独立 zip（页面上的「导入」按钮）。

    与 build_zip 的区别：不打包汇总，直接给系统能吃的那个单包。
    projlink_header：X-Project-Link（关联项目 / 对齐决策）。这条路径在界面上比
    「下载导入包」更显眼（每个模块行上都有一个「导入」），漏了它就是
    「勾了『关联项目』→ 点导入 → 包里还是没有 projectId」。
    """
    if code not in mod_def.BY_CODE:
        raise ValueError(f"未知模块 {code}")
    if not mod_def.of(code).zipped:
        # 流程不按模块出 zip：固定三步，在结果页单独下载 XML。
        raise ValueError(f"「{mod_def.label(code)}」不按模块导出："
                         f"流程 XML 请用结果页的「下载流程 XML」")
    # **必须深拷贝**：_cache_proj() 是跨请求共享的同一个 Project，apply 会改字段
    # 定义、改名、重写引用——直接改缓存的话，下一次「没带头」的请求也会带着
    # projectId（用户会看到开关关不掉，且顺序不同结果不同，极难排查）。
    # 副本上对齐是安全的：_cache_proj 已经 blueprint.expand 过，build() 内部再
    # expand 一次是幂等的，ensure_project_links 也只在副本上加字段。
    proj = copy.deepcopy(_cache_proj(data))
    if workflow_all:
        for bo in proj.bos:
            bo.flow_driven = True
    if projlink_header:
        from . import projlink as _projlink

        decisions = _projlink.parse_decisions(projlink_header)
        target = _resolve_link_target(decisions)
        proj.link_target = target
        _projlink.apply(proj, decisions, target=target)
    work = _tmp_dir("import-build")
    try:
        # demote_unavailable_links=False：这条路是「按模块逐个上传」的排查用法
        # （例：先导 data_template 的值来源，再导 form）。点「表单」这一行的
        # 「导入」时包里当然没有值来源，但用户在另一个包里会导——提前把
        # linkdata 降级成对象选择器，等于把联动/过滤整条链砍掉。
        res = build(proj, _snippets(), work, codes=[code], db_align=True,
                    demote_unavailable_links=False)
        if not res.deliverables:
            raise ValueError(f"「{mod_def.label(code)}」没有可导入的内容")
        d = res.deliverables[0]
        return d.path.read_bytes(), d.filename
    finally:
        shutil.rmtree(work, ignore_errors=True)


def workflow_files(data: bytes, workflow_all: bool = False) -> tuple[bytes, str, str]:
    """流程定义是裸 XML：一条流程回一个 .xml，多条回一个 zip（内含 {流程名}.xml）。

    返回 (body, filename, content_type)。

    为什么不复用 ``import_module``：那个接口的语义是「单模块单 zip」，
    而 9_流程 是多行表，多流程时它只会静默给出第一条（见架构文档 D3 方案 B 的否决理由）。
    """
    # 流程模式下所有对象都按固定三步出流程；deepcopy 避免污染缓存。
    from copy import deepcopy
    proj = deepcopy(_cache_proj(data)) if workflow_all else _cache_proj(data)
    if workflow_all:
        for bo in proj.bos:
            bo.flow_driven = True
        # 流程模式固定就是 开始 → 审核 → 通过：不读 9/10/11 里用户配的复杂流程，
        # 统一重建默认三步，避免导出的 XML 与页面承诺不一致。
        proj.workflows = []
    # 与 build/parse 同一套兜底：没勾流程驱动也有默认三步。
    # _cache_proj 有缓存，ensure_default_workflow 幂等（按 bo_code 判重），不会越调越多。
    ensure_default_workflow(proj)
    if not proj.workflows:
        raise ValueError("这份 Excel 里没有业务对象：先到「1_业务对象」填一行")

    # 只把**流程层面**的错误当阻断条件：用户可能只想先拿流程 XML，
    # 表单/列表那半边的问题不该拦住这个下载入口。
    errors = [i for i in validate(proj)
              if i.level == "error" and str(i.where).startswith("流程")]
    if errors:
        raise ValueError("流程定义有错误，已中止：\n"
                         + "\n".join(str(e) for e in errors[:20]))

    out = _tmp_dir("workflow-out")
    try:
        res = build(proj, _snippets(), out, codes=["workflow"], db_align=True)
        files = [d for d in res.deliverables if d.module == "workflow"]
        if not files:
            raise ValueError("没有生成出流程定义，检查 9_流程 与 10_流程节点 是否填了")
        # 先把字节读进内存再清理目录 —— 出包目录用完即删，不给并发留窗口
        blobs = [(d.filename, d.path.read_bytes()) for d in files]
        label = res.biz_label
    finally:
        shutil.rmtree(out, ignore_errors=True)

    if len(blobs) == 1:
        return blobs[0][1], blobs[0][0], "application/xml; charset=utf-8"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for filename, content in blobs:
            z.writestr(filename, content)
    stamp = datetime.now().strftime("%Y%m%d")
    name = package_name(label or "导入包", f"流程{len(blobs)}条", stamp)
    return buf.getvalue(), name, "application/zip"


# ---------------------------------------------------------------- 自然语言查询

_SCHEMA_CACHE: dict[str, object] = {}


def _db_conf() -> dict:
    return nlq_store.load().get("db", {})


def _llm_conf() -> dict:
    return nlq_store.load_llm()


def _connect():
    cfg = _db_conf()
    if not cfg.get("host"):
        raise ValueError("还没有配置数据库连接")
    return nlq_introspect.connect(
        cfg["host"], cfg.get("port") or 3306, cfg.get("user", ""),
        cfg.get("password", ""), cfg.get("database", ""),
    )


def _schema():
    cfg = _db_conf()
    key = f"{cfg.get('host')}:{cfg.get('port')}/{cfg.get('database')}"
    pack = _SCHEMA_CACHE.get(key)
    if pack is None:
        conn = _connect()
        try:
            pack = nlq_introspect.build_schema(conn, cfg["database"])
            # 全量标注：小表（如 pp_project 32 行）也可能有外键，按行数取前 N 会漏
            nlq_introspect.annotate_liveness(conn, pack, list(pack.tables))
            # 列画像：去重数/非空数/枚举值（状态码翻译、默认展示列都依赖它）
            nlq_introspect.annotate_columns(conn, pack)
        finally:
            conn.close()
        _SCHEMA_CACHE[key] = pack
    return pack


def nlq_config() -> dict:
    db = dict(_db_conf())
    llm = _llm_conf()

    db["password_set"] = bool(db.get("password"))
    db.pop("password", None)          # 不回传密码

    # 渠道列表：Key 只回传「有没有」，绝不回传明文
    channels = []
    for ch in (llm.get("channels") or []):
        item = dict(ch)
        item["api_key_set"] = bool(item.get("api_key"))
        item.pop("api_key", None)
        channels.append(item)

    return {
        "db": db,
        "llm": {"channels": channels,
                "ready": bool(nlq_llm.active_channels(llm))},
        # 全局指令：每次 AI 请求都会注入。空串 = 没配。
        "instructions": nlq_store.load_instructions(),
        # 内置免费平台预设：前端拿它渲染下拉框，用户选完自动填地址和模型
        "providers": nlq_providers.as_list(),
        "examples": len(nlq_store.all_examples()),
        # 只回传路径，不回传 Key；让用户知道「已记住」是本机文件，不是安装包自带
        "config_path": "~/.pp6kit/nlq.json",
    }


# ---------------------------------------------------------------- 项目字段快照
# 「关联项目」要对齐 pp_project 的字段定义。三档来源（projlink.load_snapshot）：
#   ① 目标库快照 ~/.pp6kit/project_fields.json
#   ② 随包 assets/project_fields.json
#   ③ 用户上传的项目对象 zip（save_snapshot_upload → token）
# 客户端没连库时 ① 没有、② 可能过期，所以界面上要提示用户传 ③。
#
# 2026-11-18 起还支持「其它业务对象」：target={name,code} 由决策头带上来，
# 连库时按名称精确解析（并缓存该对象字段快照），没连库时用上传 zip 的 token。


def _resolve_link_target(decisions: dict | None):
    """把 X-Project-Link.target 解析成 TargetObject（缺省 = 项目）。"""
    from . import projlink as _pl

    target = (decisions or {}).get("target") or {}
    return _pl.resolve_target(
        target.get("name") or "",
        target.get("code") or "",
        (decisions or {}).get("snapshot") or "")


# ---------------------------------------------------------------- AI 路径的「XX选择」
#
# 标准 Excel 那条路靠 parse_excel → builder.ensure_project_links 补「项目选择」；
# AI 这条路（一句话生成 / 需求文档解析 / 确认版 Excel）**完全不经过 build**，
# 所以必须在这里补，而且是**两处都补**：
#   ① 生成预览（ai_generate_bo / ai_from_sheet / ai_refine）：用户当场看的就是它，
#      少了这个字段 = 用户报的「勾了关联项目，生成的字段里没有项目选择」；
#   ② 导出的 Excel（ai_apply_bo）：用户拿这份回头出包时，1_业务对象 的
#      「关联项目」必须已经是「是」，否则 ensure_project_links 连门都不进，
#      字段永久丢失（线上就是没有「项目选择」，也没有数据库列）。
# 只补①：预览好看，出包还是没字段；只补②：用户先看到的预览里没有，以为没生效。
# 决策头格式与 parse/build 同一套（X-Project-Link），不新造格式。


def _ai_link_target(projlink_header: str | None):
    """AI 路径的目标解析：返回 ``(target, decisions)``，没带头时是 ``(None, None)``。

    决策头非法（不是 JSON / version≠1）在这里就抛 ValueError —— 与 /api/parse
    同口径，让路由回 400，而不是静默当成「没勾关联项目」（那正是用户投诉的形态：
    开关亮了、结果没变化）。
    """
    if not projlink_header:
        return None, None
    from .projlink import parse_decisions as _pl_decisions

    decisions = _pl_decisions(projlink_header)
    if decisions is None:
        return None, None
    return _resolve_link_target(decisions), decisions


def _ai_should_link(decisions: dict | None, code: str) -> bool:
    """这个（刚生成的）对象要不要强制补「XX选择」。语义见 projlink 里那份实现。"""
    from .projlink import decisions_link_object as _pl_link_object

    return _pl_link_object(decisions, code)


# ---------------------------------------------------------------- 对话克隆业务对象
# 用户上传过一份对象包（字段快照在 target_fields/<code>.json），想让 AI 生成一个
# 「跟它完全一样」的新对象。让 AI 抄几十个字段必丢细节，这里程序化克隆：
# 字段编码/名称/类型/长度/候选值/级联/子表列原样复制，AI 只负责起新名字。

_CLONE_WORDS = ("一样", "相同", "同样", "一模一样", "照着", "照搬", "复制", "克隆", "副本")


def _clone_intent(desc: str, target) -> bool:
    """描述点名了关联目标、且带着「照它来」的意思，才算克隆意图。

    target 可以是 TargetObject，也可以就是对象名字符串（调用处两种都有）。"""
    name = target if isinstance(target, str) else getattr(target, "name", "")
    name = (name or "").strip()
    if not name or len(name) < 2 or name not in desc:
        return False
    return any(w in desc for w in _CLONE_WORDS)


def _clone_fields_spec(snap) -> list[dict]:
    """快照字段 → build_bo 的字段 spec。

    编码原样保留（不同对象之间字段编码可以重名，系统按对象隔离）；
    dic 字段**不带** objectType——字典 key 是 <对象编码>_<字段编码>，
    新对象的编码变了，key 必须重新生成，带了旧值导出就是失效引用。
    """
    out = []
    for f in snap.fields:
        item = {"code": f.code, "name": f.name, "type": f.type_new}
        if f.length:
            item["length"] = f.length
        if f.type_new == "number" and f.precision is not None:
            item["precision"] = f.precision
        if f.type_new == "object" and f.object_type:
            item["objectType"] = f.object_type
        # 候选值：私有 selectOptions 优先，其次全局字典 items 的显示名
        values = list(f.choices or [])
        if not values and f.dict and f.dict.items:
            values = [i.name for i in f.dict.items]
        if values:
            item["values"] = values
        if f.choice_groups:
            item["choiceGroups"] = {k: list(v) for k, v in f.choice_groups.items()}
        if f.cascade_parent:
            item["cascadeParent"] = f.cascade_parent
        if (f.options or {}).get("isMultiple"):
            item["isMultiple"] = True
        if f.table_columns:
            item["columns"] = [dict(c) for c in f.table_columns]
        out.append(item)
    return out


def _clone_missing_values_hint(snap) -> str:
    """快照是旧结构（没存私有候选值）时提醒重新上传，别让人以为字段本来就没值。"""
    dic_total = sum(1 for f in snap.fields if f.type_new == "dic")
    if not dic_total:
        return ""
    dic_with = sum(1 for f in snap.fields if f.type_new == "dic"
                   and (f.choices or (f.dict and f.dict.items)))
    if dic_with >= dic_total:
        return ""
    return ("这份对象包是旧版本解析的，选项候选值没存进快照；"
            "请在「关联」处重新上传这份对象 zip 再克隆一次，候选值就齐了")


def _clone_name(desc: str, snap, cfg) -> tuple[str, str, list[str]]:
    """给克隆对象起新名。AI 起名失败回退「原名副本 / <code>_copy」。"""
    old_name = str((snap.project or {}).get("name") or "")
    old_code = str((snap.project or {}).get("code") or "")
    fallback_name = (old_name + "副本") if old_name else "克隆对象"
    fallback_code = (old_code + "_copy") if old_code else "clone_copy"
    try:
        from .nlq import llm
        text, _used = llm.chat_any(ai_bo._fast_cfg(cfg), [
            {"role": "system", "content": "你只输出 JSON，不解释。"},
            {"role": "user", "content":
                "用户要参照已有业务对象克隆出一个新的业务对象。\n"
                f"原对象：{old_name}（编码 {old_code}）\n"
                f"用户说明：{desc}\n\n"
                "给新对象起名：中文名体现用户说明里的用途或与原对象的关系，"
                "除非用户明确说了，不要就叫「XX副本」；编码用语义化小写英文"
                "（可带下划线，不用拼音），20 字符内。"
                '只输出 JSON：{"name": "...", "code": "..."}'},
        ], timeout=60, max_tokens=2000)
        spec = ai_bo._extract_json(text)
        name = str(spec.get("name") or "").strip() or fallback_name
        code = str(spec.get("code") or "").strip() or fallback_code
        return name[:60], code[:40], []
    except Exception as exc:
        return fallback_name, fallback_code, [
            f"没起成新名字（{str(exc)[:60]}），已用默认名，可在预览里改"]


def _clone_bo_result(snap, target, desc: str, cfg: dict) -> dict:
    """从快照程序化克隆对象。返回与 ai_bo.generate 同构的 dict（bo/notices/model/seconds）。"""
    import time as _t
    t0 = _t.time()
    notices: list[str] = []
    new_name, new_code, name_notes = _clone_name(desc, snap, cfg)
    notices.extend(name_notes)
    spec = {"name": new_name, "code": new_code, "fields": _clone_fields_spec(snap)}
    bo, build_notes = ai_bo.build_bo(spec, known_objects=objects.ensure(),
                                     strict_objects=True, prefer_code=True,
                                     trust_codes=True)
    notices.extend(build_notes)
    old_name = str((snap.project or {}).get("name") or target.name)
    notices.insert(0, f"已照「{old_name}」克隆出 {len(bo.fields)} 个字段的新对象"
                      f"「{bo.name}」（{bo.code}），表单和列表接着自动配")
    hint = _clone_missing_values_hint(snap)
    if hint:
        notices.append(hint)
    return {"bo": bo, "notices": notices, "model": "clone",
            "seconds": round(_t.time() - t0, 2)}


def _attach_ai_link(bo, target, decisions: dict | None,
                    notices: list[str] | None = None,
                    name: str = "") -> "BoField | None":
    """给 AI 生成的对象补/复用「XX选择」链接字段，返回那个字段（不需要补时 None）。

    复用 ``projlink.ensure_link_field`` —— 幂等规则、报错文案、sn 口径与
    builder.ensure_project_links 共用同一份实现，两条路不会各说一套。
    """
    from . import projlink as _pl

    if target is None or not _ai_should_link(decisions, bo.code):
        return None
    field, _created, note = _pl.ensure_link_field(bo, target, name=name)
    if notices is not None:
        notices.append(note)
    return field


def _prepend_link_control(form_payload: dict | None, field) -> bool:
    """把「XX选择」控件放到 AI 设计的新建表单**第一行**（已有则挪位，不重建）。

    为什么不交给 AI 表单设计器：它会把这个字段排到自认为合适的分组里，而用户
    口径是「先选项目、再由联动带出其它字段」，顺序反了联动就不成立。控件类型也
    只能是 ``linkdata``（挂值来源 + 联动 + 过滤），不能是普通下拉。

    具体改写（fields + rows 两份都要动）在 ``ai_form.prepend_control``：预览页签与
    导出的「4_表单控件」读的是同一份 payload，必须一次改全。
    没带表单布局（form_payload 为空）时不用管：ai_apply_bo 会退回「标准全套」，
    builder.ensure_project_links → _ensure_link_control 会补控件并置顶。
    """
    if not form_payload or not field:
        return False
    return ai_form.prepend_control(
        form_payload, field.code, field.name, field.object_type or "")


def object_field_options(code: str) -> dict:
    """目标对象的字段清单，给「值来源」面板勾选返回列 / 过滤列。

    三级来源与出包完全一致（本包 > 目标库 > 快照 > 离线），所以界面里能选的
    列就是包里能生成的列——两边各查一次、口径不同的话，用户勾了「专业」却
    生成不出来，属于最难解释的那种问题。
    """
    from . import links as _links

    code = str(code or "").strip()
    if not code:
        return {"ok": False, "error": "缺少对象编码", "code": "", "fields": []}
    plan = _links.LinkPlan(None)
    target = plan.target(code)
    if target is None:
        return {"ok": True, "code": code, "label": code, "source": "none", "fields": []}

    fields = []
    for f in target.fields:
        if f.type_new == "table":
            continue                    # 子表不能当返回列
        fields.append({
            "code": f.code,
            "name": f.label,
            "column": f.column,
            "type": f.type_new,
            "objectType": f.object_type,
            # system 的判定必须与生成侧**同一份**（links.LinkField.is_system）：
            # 面板拿它决定「默认勾不勾」，而「返回全部业务字段」= 非系统列。
            # 两边口径不同的话，面板把 create_time_/process_* 勾上、产物里却没有，
            # 用户会觉得「勾了不生效」。
            "system": f.is_system,
            "key": f.column == target.id_column,
        })
    # 主键永远排第一：面板里它是要置灰必选的，放最前面最不容易被忽略
    fields.sort(key=lambda x: (not x["key"], x["system"], x["code"]))
    return {"ok": True, "code": code, "label": target.label,
            "table": target.table, "source": target.source, "fields": fields}


def link_target_status(name: str = "", code: str = "") -> dict:
    """界面「核对业务对象名称」用：库精确匹配 + 快照可用性。"""
    from . import projlink as _pl

    try:
        return _pl.target_status(name, code)
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "error": str(exc), "dbConfigured": _pl.db_is_configured(),
                "name": name, "matches": [], "resolved": False, "target": None,
                "fieldCount": 0, "status": "unavailable", "warnings": []}


def link_target_upload(data: bytes, name: str = "", code: str = "") -> dict:
    """上传「其它业务对象」的 zip，解析成该对象的字段池。"""
    from . import projlink as _pl

    # 上传的 zip 本身就是字段池的权威来源：按用户输入的名称/编码从包里认对象，
    # 不再查库（库里没这个对象、或用户就是想用包里的版本时也要能继续）。
    target = _pl.resolve_target_from_zip(data, name, code)
    res = _pl.save_snapshot_upload(data, target)
    res["ok"] = True
    return res


def project_snapshot_status() -> dict:
    """当前「项目字段」从哪来、要不要让用户传项目包。"""
    from . import projlink as _pl

    cfg = _db_conf()
    db_configured = bool(
        str(cfg.get("host") or "").strip()
        and str(cfg.get("user") or "").strip()
        and str(cfg.get("database") or "").strip()
    )
    try:
        snap = _pl.load_snapshot()
    except Exception as exc:                       # noqa: BLE001
        return {"ok": True, "dbConfigured": db_configured,
                "source": "unavailable", "sourceLabel": "无",
                "fieldCount": 0, "generatedAt": "", "project": {},
                "warnings": [f"读取项目快照失败：{exc}"]}
    return {
        "ok": True,
        "dbConfigured": db_configured,
        "source": snap.source,
        "sourceLabel": _pl._SOURCE_LABEL.get(snap.source, snap.source),
        "fieldCount": len(snap.fields),
        "generatedAt": snap.generated_at or "",
        "project": snap.project or {},
        "status": snap.status,
        "warnings": list(snap.warnings or [])[:3],
    }


def project_snapshot_upload(data: bytes) -> dict:
    """上传一份项目对象包，存成第③档快照。"""
    from . import projlink as _pl

    res = _pl.save_snapshot_upload(data)
    res["token"] = res.get("token", "")
    return res


def nlq_save_config(payload: dict) -> dict:
    db = payload.get("db") or {}
    if db:
        current = _db_conf()
        if not db.get("password"):
            db["password"] = current.get("password", "")
        nlq_store.save_db(db)
    llm = payload.get("llm") or {}
    warnings = []
    if "instructions" in payload:
        nlq_store.save_instructions(payload.get("instructions") or "")
    if llm:
        nlq_store.save_llm(llm)     # save_llm 内部处理空 Key 沿用
        # 保存时就检查 Key 形态，别等测试连接才发现复制不全
        for ch in (nlq_store.load_llm().get("channels") or []):
            k = (ch.get("api_key") or "").strip()
            if k and ch.get("key") == "zhipu" and len(k) == 32 and "." not in k:
                warnings.append(
                    "智谱的 Key 是 `id.末尾一段`（中间有点，60 位左右），"
                    "你现在这个只有 32 位，可能只复制了前半段")
    _SCHEMA_CACHE.clear()
    return {"ok": True, "warnings": warnings}


def nlq_test_llm(payload: dict) -> dict:
    """测一下某个渠道通不通：发一句最短的请求，回显实际模型和耗时。

    三种入参：
        带 channel      → 测这条临时渠道（还没保存的新 Key）
        带 base_url     → 同上，旧格式
        都不带 / 没 Key → 用**已保存**的配置测
    第三种是必须的：保存成功后前端会把输入框清空（「留空则不修改」），
    这时点测试如果不回落到已存配置，就会一直提示「还没填 API Key」。
    """
    import time as _t
    cfg = payload.get("llm") or _llm_conf()
    saved = _llm_conf()

    # 允许前端传「还没保存」的临时渠道来测试
    if payload.get("channel"):
        ch = dict(payload["channel"])
        # 借用已保存配置里的东西：Key（没重新粘贴时）和 fallbacks（前端不传）
        for old in (saved.get("channels") or []):
            same = (old.get("base_url") == ch.get("base_url")
                    or old.get("key") == ch.get("key"))
            if not same:
                continue
            if not (ch.get("api_key") or "").strip() and old.get("api_key"):
                ch["api_key"] = old["api_key"]
            if not ch.get("fallbacks") and old.get("fallbacks"):
                # 否则测试时 4.7 一限流就报错，以为「连不上」
                ch["fallbacks"] = old["fallbacks"]
            break
        cfg = {"channels": [ch]}
    elif payload.get("base_url"):
        cfg = {"channels": [{"base_url": payload["base_url"],
                             "api_key": payload.get("api_key", ""),
                             "model": payload.get("model", "")}]}

    if not nlq_llm.active_channels(cfg):
        # 说清楚缺什么，别只丢一句「没填」
        has_saved = bool(nlq_llm.active_channels(saved))
        return {"ok": False, "error": (
            "还没填 API Key。" if not has_saved
            else "这次没传 Key，而且已保存的配置里也没有可用的 Key，请在输入框里重新粘贴一次"
        )}

    t0 = _t.time()
    try:
        text, used = nlq_llm.chat_any(
            cfg, [{"role": "user", "content": "只回复两个字：正常"}], timeout=20)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}
    return {
        "ok": True,
        "model": used.get("model", ""),
        "base_url": used.get("base_url", ""),
        "seconds": round(_t.time() - t0, 2),
        "reply": (text or "").strip()[:60],
    }


# ---------------------------------------------------------------- AI 生成业务对象


def ai_generate_bo(payload: dict, projlink_header: str | None = None) -> dict:
    """一句话 → 业务对象 + 表单 + 列表（三步流水线）。

    分三步而不是一次问完：每步输出小、成功率高、出错好定位。
    with_form / with_list 可以关掉某一步，只要对象。

    projlink_header：X-Project-Link 请求头（与 /api/parse 同一套）。带了它 = 用户
    在「第 1 步」选了关联业务对象，生成阶段就补「项目选择 / 合同选择」并置顶，
    而不是等出包（AI 这条路不经过 build，等出包就永远等不到，2026-11-21 用户报的
    就是这个）。目标解析/字段复用规则见 _ai_link_target / _attach_ai_link。
    """
    desc = " ".join((payload.get("description") or "").split())
    if not desc:
        raise ValueError("先用一句话描述这个业务对象要记录什么")
    if len(desc) > 800:
        desc = desc[:800]

    cfg = _llm_conf()
    want_form = payload.get("with_form", True)
    want_list = payload.get("with_list", True)
    notices: list[str] = []
    steps: list[dict] = []

    # 目标对象要在生成**之前**解析好：解析不了（用户手输的名称在库里查不到）就直接
    # 报错回 400，不能先花钱调三次模型再报「关联对象无效」。
    link_target, link_decisions = _ai_link_target(projlink_header)

    pool = objects.ensure()

    # 对话克隆（2026-11-26 用户要求）：描述点名了关联目标、且要求「一样/照着做」，
    # 就照上传的对象包快照程序化克隆字段定义，AI 只起新名字——
    # 让 AI 抄几十个字段必丢细节。快照不在（没上传对象包）就照旧从零设计。
    snap = None
    if link_target is not None and _clone_intent(desc, link_target.name):
        from .projlink import load_snapshot as _pl_load
        try:
            snap = _pl_load((link_decisions or {}).get("snapshot"),
                            target=link_target)
        except Exception:
            snap = None
        if not snap or not snap.fields:
            notices.append(
                f"要照「{link_target.name}」克隆，需要先在「关联」处上传它的对象 zip"
                f"（没读到字段快照）；已按你的描述从零设计")

    if snap is not None:
        bo_res = _clone_bo_result(snap, link_target, desc, cfg)
    else:
        bo_res = ai_bo.generate(desc, cfg, known_objects=pool)
    bo = bo_res["bo"]
    notices.extend(bo_res["notices"])
    steps.append({"step": "对象", "model": bo_res["model"], "seconds": bo_res["seconds"]})
    return _finish_bo(bo, notices=notices, steps=steps, want_form=want_form,
                      want_list=want_list, cfg=cfg, link_target=link_target,
                      link_decisions=link_decisions, model=bo_res["model"])


def _finish_bo(bo, *, notices: list[str], steps: list[dict],
               want_form: bool, want_list: bool, cfg: dict,
               link_target, link_decisions, model: str) -> dict:
    """对象定稿之后的部分：撞库校验 → 补链接字段 → 表单 / 列表设计 → 关系识别 → 组装响应。

    /api/ai/bo 与 /api/clone 共用这一份，保证「克隆出来的结果」与「AI 生成的结果」
    结构完全一致——前端只认一套字段，不用按入口分支。
    """
    pool = objects.ensure()

    # 编码撞库硬校验（2026-11-26 用户要求）：连着库时，新对象的 code 不许和
    # 系统已有对象一样——AI 起的名撞了库，导入就会跟线上对象搅在一起。
    # 放在表单/列表设计之前：撞了就报错，不多烧两次模型调用。
    # 存量包修订（/api/patch/*）本来就在改已有对象，不在这条链路上，天然豁免。
    hit = objects.code_taken(bo.code)
    if hit:
        raise ValueError(objects.code_conflict_message(hit))

    # 链接字段插在**表单/列表设计之前**：设计器看得见它，才会把它当成本对象的
    # 一部分（列宽、分组、列表列都跟着它走），否则预览里对象有字段、表单却没有控件。
    link_field = _attach_ai_link(bo, link_target, link_decisions, notices)

    form_payload = None
    if want_form:
        try:
            f_res = ai_form.generate(bo, cfg)
            form_payload = ai_form.to_dict(f_res["form"])
            notices.extend(f_res["notices"])
            steps.append({"step": "表单", "model": f_res["model"], "seconds": f_res["seconds"]})
        except Exception as exc:
            # 表单失败不该让整个生成失败——对象已经生成了，用户还能用
            notices.append(f"表单设计没成功（{str(exc)[:60]}），已用默认布局代替")
            steps.append({"step": "表单", "error": str(exc)[:120]})
        # 控件位置由我们定（置顶 + linkdata），不交给模型
        if _prepend_link_control(form_payload, link_field):
            notices.append(f"表单里已把「{link_field.name}」放到第一行（先选它，再由联动带出其它字段）")

    if want_form:
        notices.append("出包时会自动补一张只读详情表单：新建走新建表单，查看/流程走详情表单")

    list_payload = None
    if want_list:
        try:
            l_res = ai_list.generate(bo, cfg)
            list_payload = ai_list.to_dict(l_res["list"], bo)
            notices.extend(l_res["notices"])
            steps.append({"step": "列表", "model": l_res["model"], "seconds": l_res["seconds"]})
        except Exception as exc:
            notices.append(f"列表配置没成功（{str(exc)[:60]}），已用默认列")
            steps.append({"step": "列表", "error": str(exc)[:120]})

    # 关系识别复用同一套分析：给 AI 生成的对象也出建议，供用户确认。
    from . import relations as _relations
    from .model import Project as _Project
    _rel_proj = _Project()
    _rel_proj.bos.append(bo)
    try:
        relationships = _relations.analyze(_rel_proj)
    except Exception:
        relationships = {"status": "unavailable", "notice": "关系识别失败",
                         "parentOptions": [], "items": []}

    return {
        "ok": True,
        "bo": ai_bo.to_dict(bo),
        "form": form_payload,
        "list": list_payload,
        "notices": notices,
        "steps": steps,
        "types": ai_bo.type_choices(),
        "objects": pool,
        "objectSource": objects.status(),
        "existObject": objects.lookup(bo.name),
        "relationships": relationships,
        # 本次关联的目标对象（没关联时为 None）：前端据此显示「已关联：合同」，
        # 出包/导出时也用它确认两边说的是同一个对象。
        "linkTarget": (link_target.to_dict() if link_target is not None else None),
        "model": model,
        "seconds": round(sum(s.get("seconds", 0) for s in steps), 2),
    }


_CLONE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


def clone_inspect(data: bytes) -> dict:
    """对象 zip → 可选对象清单（弹窗用）。只解析，不调模型、不落库。

    多对象包全列出来：流程包常带一个主对象 + 若干子对象，由用户单选。
    """
    import tempfile

    from .inspect import inspect_path

    if not data:
        raise ValueError("没有收到 zip 文件")
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "clone.zip"
        path.write_bytes(data)
        try:
            proj, notes = inspect_path(path)
        except Exception as exc:      # BadZipFile / 解析异常都归成一句人话
            raise ValueError(f"读不了这份 zip（{type(exc).__name__}）："
                             f"请确认是从「业务对象」导出的同步包") from exc
    if not proj.bos:
        raise ValueError("这份 zip 里没有业务对象（bo/*.xml）；请确认导出的是「业务对象」包")
    items = []
    for bo in proj.bos:
        code = str(bo.code or "").strip()
        if not code:
            continue
        items.append({
            "code": code,
            "name": bo.name or code,
            "tbl": bo.tbl_name or "",
            "fields": len(bo.fields or []),
            "flow": bool(bo.flow_driven),
            "suggestedName": f"{bo.name or code}副本"[:60],
            "suggestedCode": f"{code}_copy"[:40],
        })
    if not items:
        raise ValueError("zip 里的业务对象没有编码，无法克隆")
    return {"ok": True, "objects": items, "notes": [n for n in notes if n][:5]}


def clone_apply(data: bytes, src_code: str, name: str, code: str,
                with_form: bool = True, with_list: bool = True) -> dict:
    """按 zip 里某个对象的字段定义，程序化克隆出一个新对象。

    红线（2026-11-27 设计稿）：字段定义必须来自 zip 解析结果本身，**不让大模型
    转写**——长度、候选值、级联、子表列任何一项靠自然语言描述都会丢。
    AI 只参与「表单/列表怎么排」（与 /api/ai/bo 同一条链）。
    """
    import time as _t

    from . import projlink

    if not data:
        raise ValueError("没有收到 zip 文件")
    src_code = str(src_code or "").strip()
    if not src_code:
        raise ValueError("没有选择要克隆的对象")
    name = " ".join(str(name or "").split())[:60]
    code = str(code or "").strip()
    if not name:
        raise ValueError("新对象名称不能为空")
    if not _CLONE_CODE_RE.match(code):
        raise ValueError("对象编码不合法：小写字母开头，只用小写字母、数字、下划线（2–40 位）")
    # 新编码必须与源对象不同：相同编码出包导入就是「更新原对象」，
    # 等于拿克隆改人家线上数据（没连库时 code_taken 也拦不住这一条）。
    if code == src_code:
        raise ValueError(f"新编码不能和原对象一样（{src_code}）：克隆出来是独立的新对象，"
                         f"编码相同导入就会覆盖原对象")

    t0 = _t.time()
    target = projlink.TargetObject(code=src_code)
    try:
        snap = projlink.snapshot_from_zip(data, "package_upload", target)
    except Exception as exc:
        raise ValueError(f"读不了这份 zip（{type(exc).__name__}）："
                         f"请确认是从「业务对象」导出的同步包") from exc
    if snap.source == "unavailable" or not snap.fields:
        warn = "；".join(snap.warnings) or "没解析到字段"
        raise ValueError(f"没能从 zip 里读到「{src_code}」的字段定义：{warn}")

    spec = {"name": name, "code": code, "fields": _clone_fields_spec(snap)}
    bo, build_notes = ai_bo.build_bo(spec, known_objects=objects.ensure(),
                                     strict_objects=True, prefer_code=True,
                                     trust_codes=True)
    # 字段编码/物理列按原对象原样复制；名称与编码用用户填的那份（build 可能做了清洗）
    bo.name = name
    bo.code = code
    # 原对象是流程对象时，新对象也标成流程：否则那批流程冗余字段没有流程承载
    src_flow = _clone_src_flow(data, src_code)
    if src_flow:
        bo.flow_driven = True

    notices = [f"已照「{snap.project.get('name') or src_code}」克隆出 {len(bo.fields)} 个字段的新对象"
               f"「{bo.name}」（{bo.code}），表单和列表接着自动配"]
    notices.extend(build_notes)
    hint = _clone_missing_values_hint(snap)
    if hint:
        notices.append(hint)
    if src_flow:
        notices.append("原对象是流程对象：流程冗余字段已带上；流程图不克隆，"
                       "需要的话去「新建流程配置」重画")

    steps = [{"step": "对象", "model": "clone", "seconds": round(_t.time() - t0, 2)}]
    out = _finish_bo(bo, notices=notices, steps=steps, want_form=with_form,
                     want_list=with_list, cfg=_llm_conf(), link_target=None,
                     link_decisions={}, model="clone")
    out["bo"]["cloneFrom"] = {"name": snap.project.get("name") or src_code, "code": src_code}
    return out


def _clone_src_flow(data: bytes, src_code: str) -> bool:
    """zip 里这个对象是不是流程对象（只解析，失败按非流程处理）。"""
    import tempfile

    from .inspect import inspect_path

    try:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "clone.zip"
            path.write_bytes(data)
            proj, _notes = inspect_path(path)
        bo = proj.bo(src_code)
        return bool(bo and bo.flow_driven)
    except Exception:
        return False


def ai_workflow(data: bytes, instruction: str, focus: str = "") -> tuple[bytes, str, dict]:
    """一句话生成/修改流程：返回新的 xlsx + 文件名 + 摘要（前端 toast 用）。"""
    from .builder import safe_filename

    body, summary = ai_wf.generate(data, instruction, focus, _llm_conf())
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    names = [w["name"] for w in summary.get("workflows") or []]
    label = names[0] if len(names) == 1 else (f"{len(names)}条流程" if names else "流程")
    return body, f"{safe_filename(label)}-流程草稿-{stamp}.xlsx", summary


def ai_from_sheet(data: bytes, hint: str = "", with_form: bool = True,
                  with_list: bool = True, sheet: str = "",
                  projlink_header: str | None = None) -> dict:
    """非标准 Excel → 标准「对象 + 表单 + 列表」。

    和 ai_generate_bo 的区别只在「需求从哪来」：
    那边是一句话，这边是项目经理自己整理的表格。
    后半段（表单/列表设计、出 Excel）完全复用，保证两条路产物一致。

    projlink_header：X-Project-Link 请求头。与 ai_generate_bo 同口径——两条 AI
    入口都是「生成预览」，用户不会因为这次是上传表格而不是说一句话，就少一个
    「项目选择」字段（少一处就是用户报的那个 bug）。
    """
    if not data:
        raise ValueError("没收到文件")
    cfg = _llm_conf()
    notices: list[str] = []
    steps: list[dict] = []

    # 与 ai_generate_bo 同理：目标先解析，解析不了就别浪费三次模型调用
    link_target, link_decisions = _ai_link_target(projlink_header)

    summary = ai_sheet.summarize(data, only=sheet or None)
    if not summary["sheets"]:
        names = [f"{it['name']}" for it in summary.get("inventory") or []][:6]
        tip = ("（读到了这些表但都为空：" + "、".join(names) + "）") if names else ""
        raise ValueError(
            "这个 Excel 里没读到可用的表单或表格。"
            "支持两种：① 表头 + 数据行；② 表单搜集表（每行「字段名 | 【控件类型】说明」）。"
            + tip)
    steps.append({"step": "读表", "seconds": 0,
                  "note": "、".join(s["name"] for s in summary["sheets"])})

    # 跨 sheet 的疑似重复字段：这次只分析一张，但另外几张里可能已经把
    # 同一个字段叫了别的名字，趁早说，省得建完两个字段再回头合并
    dupes = summary.get("crossDupes") or []
    if dupes:
        top = dupes[:3]
        notices.append(
            "这个文件里多张表的字段很像，可能是同一个字段起了两个名字："
            + "；".join(f"「{d['sheetA']}」{d['a']} ↔ 「{d['sheetB']}」{d['b']}" for d in top)
            + (f"（共 {len(dupes)} 处）" if len(dupes) > 3 else "")
            + "。如果确实是同一个，建议先统一再分析，否则会建成两个字段"
        )

    pool = objects.ensure()
    res = ai_sheet.analyze(summary, cfg, known_objects=pool, hint=hint)
    bo = res["bo"]
    notices.extend(res["notices"])
    steps.append({"step": "映射字段", "model": res["model"], "seconds": res["seconds"]})

    # 与 ai_generate_bo 同一道闸：识别出的对象编码不许撞库（撞了就在这里报错，
    # 不等出包、更不等导入）。存量包修订不走这条链路。
    hit = objects.code_taken(bo.code)
    if hit:
        raise ValueError(objects.code_conflict_message(hit))

    # 链接字段插在表单/列表设计之前（理由见 ai_generate_bo 同名注释）
    link_field = _attach_ai_link(bo, link_target, link_decisions, notices)

    form_payload = None
    if with_form:
        try:
            f_res = ai_form.generate(bo, cfg)
            form_payload = ai_form.to_dict(f_res["form"])
            notices.extend(f_res["notices"])
            steps.append({"step": "表单", "model": f_res["model"], "seconds": f_res["seconds"]})
        except Exception as exc:
            notices.append(f"表单设计没成功（{str(exc)[:60]}），已用默认布局代替")
            steps.append({"step": "表单", "error": str(exc)[:120]})
        if _prepend_link_control(form_payload, link_field):
            notices.append(f"表单里已把「{link_field.name}」放到第一行（先选它，再由联动带出其它字段）")

    list_payload = None
    if with_list:
        try:
            l_res = ai_list.generate(bo, cfg)
            list_payload = ai_list.to_dict(l_res["list"], bo)
            notices.extend(l_res["notices"])
            steps.append({"step": "列表", "model": l_res["model"], "seconds": l_res["seconds"]})
        except Exception as exc:
            notices.append(f"列表设计没成功（{str(exc)[:60]}），已用默认列代替")
            steps.append({"step": "列表", "error": str(exc)[:120]})

    from . import relations as _relations
    from .model import Project as _Project
    _rel_proj = _Project()
    _rel_proj.bos.append(bo)
    try:
        relationships = _relations.analyze(_rel_proj)
    except Exception:
        relationships = {"status": "unavailable", "notice": "关系识别失败",
                         "parentOptions": [], "items": []}

    return {
        "ok": True,
        "bo": ai_bo.to_dict(bo),
        "form": form_payload,
        "list": list_payload,
        "notices": notices,
        "steps": steps,
        "types": ai_bo.type_choices(),
        "objects": objects.ensure(),
        "objectSource": objects.status(),
        "existObject": objects.lookup(bo.name),
        "relationships": relationships,
        # 与 ai_generate_bo 同口径：本次关联的目标对象（没关联 = None）
        "linkTarget": (link_target.to_dict() if link_target is not None else None),
        "model": res["model"],
        "sourceColumns": res.get("sourceColumns") or {},
        "sheets": [s["name"] for s in summary["sheets"]],
        # 一张文件里可能有二三十张表单，让用户能换一张再分析
        "formSheets": summary.get("inventory") or [],
        "sheetCount": summary.get("sheetCount", 1),
        "seconds": round(sum(s.get("seconds", 0) for s in steps), 2),
    }


def _field_options(f: dict) -> dict:
    """把预览/JSON 里散落在顶层的开关收进 options。

    AI 和前端习惯把 isMultiple / isIndex 这种开关放在字段顶层，
    但生成器只认 BoField.options，不收进来这些设置就全丢了。
    options 里已经有的优先（Excel 走的是 options 那条路）。
    """
    opts = dict(f.get("options") or {})
    two = f.get("isMultiple")
    # isMultiple 只对「选项 / 用户 / 部门」有意义，别的类型塞进去是噪声
    if two is not None and (f.get("type") in ("dic", "user", "department")):
        opts["isMultiple"] = bool(two)
    if f.get("isIndex"):
        opts["isIndex"] = True
    return opts


def _source_configs(fields: list[dict]) -> dict[str, dict]:
    """预览里「值来源」面板配的东西：字段编码 -> 配置。

    形状（前端 state 原样回传）：

        {"custom": true, "datasetKey": "", "filterField": "xiangMuLeiXing",
         "filterTarget": "", "resultFields": ["id", "name", "zhuanYe"],
         "linkage": [{"name": "zhuan_ye_", "field": "zhuanYe", "label": "专业"}],
         "filterLogic": "AND",
         "filterRules": [{"field": "classification_id_", "operator": "equal",
                          "source": "dynamic", "fieldSource": "formField",
                          "control": "xiangMuLeiXing"}]}

    没配过的字段不在返回里；下游据此决定「自动推导」还是「按配置出」。

    ``filterRules``（2026-11-20）：面板把「过滤条件」单选下拉换成了规则行列表。
    **只有 payload 里真的带了这个键才走规则路径**（``filterRules: []`` = 用户
    把规则全删了 = 明确不过滤）；老会话只带 ``filterField``，那就仍然走 legacy
    口径，产物逐字不变。前端发 ``fieldSource``，Excel 里叫 ``field_source``，
    两种写法都认。

    ``custom`` 是「用户动过面板没有」的标记（2026-11-20）：
    面板在用户没动过时会把**全部业务字段**显示成勾上（那是后端的默认口径
    「返回全部业务字段 + 主键、不加过滤」），如果把这批默认勾选当成用户配置
    写进 6_数据集，后端就不再加全字段了——用户什么也没改，产物却从 38 列
    变成他屏幕上看着的那几列。所以 ``custom !== true`` 时**忽略 resultFields**，
    只认过滤字段与联动（那两项非动手不会出现）。

    ``custom === true`` 但 ``resultFields`` 是空数组：用户把业务列**全取消**了
    （只剩锁定的主键），或者是别的入口产出了空列表。这时不能写「返回字段为空」
    ——空列表在生成侧等于「没配过」，产物落回默认全字段，于是又出现
    「面板说 1 列、包里 38 列」。按「只返回主键 + 名称」处理（``["id"]``，
    名称由 ``links.LinkTarget.result_list`` 强制补），并让调用方出一条 notice。
    """
    out: dict[str, dict] = {}
    for f in fields or []:
        if not isinstance(f, dict):
            continue
        code = str(f.get("code") or "").strip()
        cfg = f.get("sourceConfig")
        if not code or not isinstance(cfg, dict):
            continue
        custom = cfg.get("custom") is True
        wanted = ([str(x).strip() for x in (cfg.get("resultFields") or []) if str(x).strip()]
                  if custom else [])
        linkage = [it for it in (cfg.get("linkage") or []) if isinstance(it, dict)]
        fallback = False
        if custom and not wanted:
            # 见 docstring：空返回列不能当「没配过」，否则口径倒回全字段
            wanted, linkage, fallback = ["id"], [], True
        # 过滤规则（2026-11-20）：面板把单选下拉换成了规则行列表。
        # **只有 payload 里真的带了 filterRules 才走规则路径**——老会话只带
        # filterField，那就仍然交给 legacy 口径，产物逐字不变（兼容红线）。
        rules_raw = cfg.get("filterRules")
        has_rules = isinstance(rules_raw, list)
        rules: list[FilterRule] = []
        # 半填的规则（面板上加了行、但没选目标列 / 格式不全）必须记数：
        # 直接 continue 会让 filterRules 变成 []，而 [] 的语义是「用户主动删光 =
        # 明确不过滤」，于是「配了三条、一条没生效」在界面上看起来像「没配过滤」，
        # 产物里也就真的没有过滤——静默失效。这里只统计、不阻断，交给调用方出提示。
        dropped = 0
        if has_rules:
            for it in rules_raw:
                if not isinstance(it, dict):
                    dropped += 1
                    continue
                field = str(it.get("field") or "").strip()
                source = str(it.get("source") or "dynamic").strip() or "dynamic"
                control = str(it.get("control") or "").strip()
                # 只选了「本表单哪个字段」的写法则 field 留空，目标列由生成侧按
                # 显示名推导（links.filter_rules_of）。其余三种必须有目标列。
                if not field and not (source == "dynamic" and control):
                    dropped += 1
                    continue
                rules.append(FilterRule(
                    field=field,
                    operator=str(it.get("operator") or "equal").strip() or "equal",
                    source=source,
                    field_source=str(it.get("fieldSource") or it.get("field_source")
                                     or "formField").strip() or "formField",
                    value=it.get("value"),
                    control=control,
                    type=str(it.get("type") or "").strip(),
                    input=str(it.get("input") or "").strip(),
                    label=str(it.get("label") or "").strip(),
                ))
        filter_logic = str(cfg.get("filterLogic") or "AND").strip().upper() or "AND"
        if filter_logic not in ("AND", "OR"):
            filter_logic = "AND"
        if not (wanted or linkage or str(cfg.get("filterField") or "").strip()
                or has_rules):
            continue
        out[code] = {
            "datasetKey": str(cfg.get("datasetKey") or "").strip(),
            "filterField": str(cfg.get("filterField") or "").strip(),
            "filterTarget": str(cfg.get("filterTarget") or "").strip(),
            # None = 没配规则（走 legacy），[] = 明确不过滤，非空 = 按规则
            "filterRules": rules if has_rules else None,
            "filterLogic": filter_logic,
            # 被跳过的条数（0 = 没有半填的规则）。filterRules=[] 本身不算 dropped：
            # 那是用户把规则全删了，语义是「明确不过滤」。
            "droppedRules": dropped,
            "resultFields": wanted,
            "linkage": linkage,
            "resultFallback": fallback,
        }
    return out


def _apply_source_configs(proj, bo, fields: list[dict]) -> list[str]:
    """把值来源配置落成值来源的行（+ 控件联动在表单段里带走）。

    落的是 ``DataSet(ds_type=valueSource)``，写 Excel 时由 template.sheet_data
    按 ds_type 分流到 ``6.1_值来源``（列表数据集 / 筛选器仍在 ``6_数据集``）。

    为什么要在这里建 DataSet、而不是等 blueprint 自动建：自动建只按「同名
    字段」推一套配置，用户在界面上勾的返回列 / 过滤字段 / 联动映射必须显式
    写进 Excel，否则下次打开或换台机器就丢了（预览=产物这条铁律）。

    同一目标对象 + 同一套配置复用一份值来源；配置不同就另起 `<key>_2`——
    两个控件过滤条件不同却共用一份值来源时，导入后必有一个是错的。
    """
    from .modules import DS_VALUE

    cfgs = _source_configs(fields)
    if not cfgs:
        return []
    notices: list[str] = []
    used: dict[str, tuple[str, str]] = {}     # 配置指纹 -> (target, key)
    taken = {d.key for d in proj.datasets}
    by_code = {f.code: f for f in bo.fields}
    for code, cfg in cfgs.items():
        bf = by_code.get(code)
        target = (bf.object_type if bf is not None else "") or ""
        if not target:
            continue
        fp = json.dumps([target, cfg["filterField"], cfg["filterTarget"],
                         cfg["resultFields"],
                         # 规则必须进指纹：同一个目标对象、同一批返回列，但过滤
                         # 规则不同 → 必须另起 <key>_2，否则两个控件共用一份
                         # 值来源，导入后必有一个过滤条件失效。
                         ([r.to_dict() for r in cfg["filterRules"]]
                          if cfg["filterRules"] is not None else None),
                         cfg["filterLogic"]],
                        ensure_ascii=False, sort_keys=True)
        if fp in used:
            continue
        # 面板上传的键名还叫 datasetKey（前端契约，没改），语义是「这份值来源的
        # key」。留空就按统一规则生成 <对象名翻英文>_value（冲突才 _2/_3）——
        # 与 blueprint/reader 同一处真相，别在这里再拼一遍。名字优先、编码兜底：
        # 对象编码本身可能是拼音（caiGou），拿它拼就是 caigou_value。
        target_name = object_name_of(proj, target) or target
        base = cfg["datasetKey"] or value_source_key(target, bo_name=target_name)
        key = base
        n = 2
        while key in taken:
            key = f"{base}_{n}"
            n += 1
        used[fp] = (target, key)
        taken.add(key)
        proj.datasets.append(DataSet(
            key=key,
            name=f"值来源_{bf.name}",
            from_table=target,
            ds_type=DS_VALUE,
            filter_field=cfg["filterField"],
            filter_target=cfg["filterTarget"],
            # None = 老会话（只带 filterField）→ 走 legacy 口径，产物不变；
            # [] = 面板里把规则全删了 → 明确不要任何过滤；非空 = 按规则生成。
            filter_rules=cfg["filterRules"],
            filter_logic=cfg["filterLogic"],
            result_fields=cfg["resultFields"],
        ))
        # 过滤那一格的文案必须跟着**真正会生效的那条路径**走：
        #   有规则     → 「N 条规则（AND 连接）」；此时 filterField 已被忽略，
        #                再写「过滤：自动」就是在骗人（P2-2 的原始症状）
        #   []        → 「不过滤」（用户把规则删光了，= 明确不过滤）
        #   None      → 老口径，才轮到 filterField / 自动
        rules = cfg["filterRules"]
        if rules:
            # 用逗号而不是括号：这行本身已经在一个括号里，嵌套括号读起来别扭
            filt_text = f"{len(rules)} 条规则，{cfg['filterLogic']} 连接"
        elif rules is not None:
            filt_text = "不过滤"
        else:
            filt_text = cfg["filterField"] or "自动"
        notices.append(f"值来源 {key}：按界面配置生成（过滤：{filt_text}，"
                       f"返回 {len(cfg['resultFields']) or 2} 列）")
        if cfg.get("droppedRules"):
            # 半填的规则被跳过时必须说：否则界面上配了三条、包里一条都没有，
            # 而 [] 的语义还会被理解成「用户主动不要过滤」，属于静默失效
            notices.append(f"值来源 {key}：有 {cfg['droppedRules']} 条过滤规则"
                           f"没选目标列（或格式不全），已忽略")
        if not cfg["datasetKey"]:
            # key 是工具生成的：词典缺词退拼音时要说清（用户才知道该去补词）
            notices.extend(value_source_key_notes(key, target_name))
        if cfg.get("resultFallback"):
            # 用户把返回列全取消了：按「只返回主键 + 名称」生成，必须说出来。
            # 不提示的话导入后发现下拉只有「名称」一列，用户会以为配置丢了。
            notices.append(f"值来源 {key}：已按你的选择只返回主键/名称"
                           f"（面板里把返回列都取消了）")
    return notices


def _demote_controls_of(proj, key: str) -> list[str]:
    """把控件从「挂这份值来源」退回对象选择器。"""
    touched: list[str] = []
    for form in proj.forms:
        for ff in form.fields:
            if ff.value_source != key:
                continue
            ff.value_source = ""
            if ff.field_type == "linkdata":
                ff.field_type = "selectObject"
            touched.append(ff.label or ff.name)
    return touched


def _apply_value_sources(proj, items) -> list[str]:
    """把「值来源」页签的清单应用成模型：开 / 关。

    ``items is None``（老会话没打开过页签）→ 一行都不碰，产物与改造前逐字一致。
    带清单时先跑一遍自动生成（与出包同一函数），否则前端看到的 key 在这里不存在。
    """
    notices: list[str] = []
    if items is None:
        return notices
    from . import blueprint as _bp

    _bp.ensure_self_value_sources(proj)
    _bp._ensure_value_sources(proj, announce=False)
    by_key = {d.key: d for d in proj.datasets}
    for item in items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        ds = by_key.get(key)
        if ds is None:
            continue
        want = bool(item.get("enabled", True))
        if want == bool(ds.enabled):
            continue
        if not want and not ds.toggleable():
            notices.append(
                f"值来源 {key}（{ds.origin_label() or '默认'}）是控件取数的基础，"
                f"不能关闭，已忽略")
            continue
        ds.enabled = want
        if want:
            notices.append(f"值来源 {key}：已开启，随包生成")
        else:
            touched = _demote_controls_of(proj, key)
            notices.append(
                f"值来源 {key}：已关闭，不生成选择器 XML"
                + (f"；控件 {'、'.join(touched)} 改用对象选择器" if touched else ""))
    return notices


def ai_value_sources(payload: dict, projlink_header: str | None = None) -> dict:
    """预览「值来源」页签的清单：出包时包里会有哪些值来源、谁在用、能不能关。

    key 必须与出包一致 → 这里跑的是和 build 完全同一套
    ``ensure_self_value_sources`` + ``_ensure_value_sources``，输入也同一份
    payload（对象 + 表单 + 列表），所以同一份配置两次请求拿到的 key 不变。
    """
    probe: dict = {}
    ai_apply_bo(payload, None, [], projlink_header, probe=probe)
    proj = probe.get("proj")
    if proj is None:
        raise ValueError("缺少对象信息")
    from . import blueprint as _bp

    _bp.ensure_self_value_sources(proj)
    _bp._ensure_value_sources(proj, announce=False)

    used_by: dict[str, list[str]] = {}
    for form in proj.forms:
        for ff in form.fields:
            if ff.value_source:
                used_by.setdefault(ff.value_source, []).append(
                    f"{form.name}·{ff.label or ff.name}")

    items = []
    for ds in proj.datasets:
        bo = proj.bo(ds.from_table) or next(
            (b for b in proj.bos if b.tbl_name == ds.from_table), None)
        target = bo.code if bo is not None else ds.from_table
        items.append({
            "key": ds.key,
            "name": ds.name,
            "target": target,
            "targetName": (bo.name if bo is not None else "") or target,
            "origin": ds.guess_origin(),
            "originLabel": ds.origin_label(),
            "enabled": bool(ds.enabled),
            "toggleable": bool(ds.toggleable()),
            "resultAll": bool(ds.result_all),
            "resultFields": list(ds.result_fields or []),
            "filterRules": [r.to_dict() for r in (ds.filter_rules or [])],
            "filterLogic": ds.filter_logic or "AND",
            "filterField": ds.filter_field or "",
            "filterTarget": ds.filter_target or "",
            "filterNone": ds.filter_rules == [],
            "usedBy": used_by.get(ds.key, []),
        })

    return {"ok": True, "valueSources": items}


def ai_apply_bo(payload: dict, relations_header: str | None = None,
                notices: list[str] | None = None,
                projlink_header: str | None = None,
                probe: dict | None = None) -> tuple[bytes, str]:
    """把 AI 生成的「对象 + 表单 + 列表」落成一份 Excel。

    前端会把 /api/ai/bo 的完整结果回传（含 form/list），
    这样用户看到的预览和下载到的 Excel 是同一份东西。
    没带 form/list 时（旧调用）退回默认布局，保证还能用。
    relations_header：用户在确认区做的关系决定，写进这份 Excel 的父子三列。
    projlink_header：X-Project-Link（与 /api/parse 同一套）。这份 Excel 必须自己
    带上「关联项目 = 是」+ 链接字段，原因有两层：
      ① 用户下载后可能**线下改完再拖回来**，那时前端还会带一次头，能补救；
      ② 但用户也可能拿着它去出包/给实施，这份 Excel 就是唯一的底稿——里面若
         没有该字段、「关联项目」写着「否」，builder.ensure_project_links 连门
         都不会进，字段就永久丢了（而且重新上传也补不回来）。
    没带头时兜底：预览回传的字段里带 isLink 的，照样保留并置「关联项目=是」
    （用户勾过、前端漏带头时不能把字段吃掉）。
    notices：出参（可选）。值来源配置的提示（例如「你把返回列全取消了，
    已按只返回主键/名称生成」）需要有人能看见——本函数的返回是一份二进制
    xlsx，HTTP 那条路没有提示通道；调用方传一个 list 进来就能拿到，
    同时这些行也会写进 app 日志，方便实施事后核对。
    """
    data = payload.get("bo") or {}
    if not data.get("code"):
        raise ValueError("缺少对象信息")

    # 导出前的最后一道闸（2026-11-26 用户要求）：生成/微调时没连库拦不住，
    # 预览停留期间库里也可能被人建了同名对象。撞了就拒绝导出，别让一份
    # 必撞库的 Excel 流出去。存量包修订不经过这条链路。
    hit = objects.code_taken(str(data["code"]))
    if hit:
        raise ValueError(objects.code_conflict_message(hit))

    proj = Project()
    demo = demo_project()
    proj.tenant_id = demo.tenant_id
    proj.file_prefix = demo.file_prefix
    proj.module_id = demo.module_id
    proj.type_id = demo.type_id

    # 关联业务对象：目标先解析（解析不了就别生成半份 Excel）
    link_target, link_decisions = _ai_link_target(projlink_header)
    proj.link_target = link_target
    # 这次要不要**按目标**补链接字段（有目标 + 决策没说关）。
    # 只有这种情况才把预览回传的那条「XX选择」从循环里摘出去：
    # 它要按 sn = max+1 的口径重新注入，不能顺着数组顺序占掉 sn=1。
    inject_link = (link_target is not None
                   and _ai_should_link(link_decisions, data["code"]))
    # 预览回传的「XX选择」字段（可能没有）：显示名要保住（用户/AI 改过）
    payload_fields = data.get("fields") or []
    link_payload = next(
        (f for f in payload_fields if f.get("isLink")), None)
    link_payload_code = str((link_payload or {}).get("code") or "")
    link_payload_name = str((link_payload or {}).get("name") or "").strip()

    from .model import table_name_for
    bo = Bo(code=data["code"], name=data.get("name") or data["code"],
            tbl_name=table_name_for(data["code"]), desc=data.get("desc") or "")
    sn = 0
    for i, f in enumerate(payload_fields, start=1):
        # 只有「就是范式那条」（编码 == target.link_code）才摘出去，在循环后按
        # sn = max+1 重注入。**复用来的非范式编码必须留在循环里**：AI 常自己给一个
        # 「所属项目」（suoShuXiangMu），预览侧按口径复用它并打了 isLink；若这里
        # 把它也摘掉，ensure_link_field 在 bo 里就找不到它、只能新建 projectId——
        # 等于悄悄把列名从 suoShuXiangMu 改成 projectId（预览≠产物，线上要 ALTER），
        # 而且 sn 会从 1 漂到 2、列表列与表单控件一起换掉。
        if (inject_link and link_payload_code
                and link_payload_code == link_target.link_code
                and (not f.get("objectType") or f.get("objectType") == link_target.code)
                and str(f.get("code") or "") == link_payload_code):
            continue                 # 范式链接字段统一在循环后按 max+1 注入
        sn += 1
        _groups = dict(f.get("choiceGroups") or {})
        _choices = list(f.get("choices") or [])
        # 分组里的子项必须都在 choices 里，否则 Excel 写出去会缺项
        for _kids in _groups.values():
            for _k in (_kids or []):
                if _k and _k not in _choices:
                    _choices.append(_k)
        bo.fields.append(BoField(
            bo_code=bo.code,
            code=f.get("code") or f"field{i}",
            name=f.get("name") or f.get("code") or f"字段{i}",
            type_new=f.get("type") or "varchar",
            length=f.get("length"),
            object_type=f.get("objectType") or "",
            # 选项值要落到 2_字段 的「选项值」列，否则预览里改的候选值白改
            choices=_choices,
            # 分组必须一起带走：Excel 的「选项值」列按「父：子1、子2」写，
            # 读回来才能还原 choice_groups → dependValues
            choice_groups=_groups,
            # 单选/多选（dic）、重复值校验、索引这些开关都走 options。
            # 前端预览传的是顶层字段 isMultiple/isIndex，这里归到 options 里，
            # 否则 resolved() 拿不到、生成出来的永远是单选。
            options=_field_options(f),
            # 字段级级联（写进 attribute options）。表单控件层没单独填时，
            # 生成阶段会从这里继承，所以界面/AI 只需要说一次。
            cascade_parent=str(f.get("cascadeParent") or "").strip(),
            # 表格字段的子列：AI 给的 columns 原样带进模型，下载到的 Excel
            # 才会在「2.1_表格列」里把列一行行写出来（否则用户只看到一张空子表）
            table_columns=ai_bo._clean_columns(f.get("columns")),
            sn=sn,
        ))
    proj.bos.append(bo)

    # ---- 「XX选择」链接字段：必须是这份 Excel 的一部分，不能等出包时补
    # ① 带了决策头 → 按目标补/复用（与 builder.ensure_project_links 同一份
    #    plan_link_field，已有指向目标的字段就复用，绝不加第二个）；
    # ② 没带头、但预览回传里带 isLink → 把标记与「关联项目=是」保住。
    #    前端漏带头时不至于把用户看到的字段吃掉（少一处会怎样的另一半）。
    if inject_link:
        _field = _attach_ai_link(bo, link_target, link_decisions, notices,
                                name=link_payload_name)
        # 控件也要进这张 Excel 的「4_表单控件」：否则用户在新建表单里选不到目标
        # 对象（字段有、控件没有 = 下拉框根本不存在）
        _prepend_link_control(data.get("form"), _field)
    elif link_payload is not None:
        _restore = bo.field_by_code(link_payload_code)
        if _restore is not None:
            # 没带头（或头里说这个对象不关联）但预览里明明有这个字段：
            # 字段留着、标记打上、「关联项目」写「是」——用户看到过的东西不能凭空消失。
            # is_link 到这里只是**内部标记**（Excel 里不存在这一列），
            # 下次出包时 builder.ensure_project_links 会重新按目标判定并复用同一字段。
            _restore.is_link = True
            bo.project_link = True
            if notices is not None:
                notices.append(
                    f"业务对象 {bo.code}: 已按预览保留链接字段 {_restore.code}"
                    f"（{_restore.name}），1_业务对象 的「关联项目」写「是」")
    # 界面上配的「值来源」先落成数据集：表单段要靠它接 linkdata，
    # Excel 也要靠它把配置写进 6.1_值来源（预览=产物）。
    src_notices = _apply_source_configs(proj, bo, data.get("fields") or [])
    if src_notices:
        if notices is not None:
            notices.extend(src_notices)
        # 提示不能只活在返回值里：这个接口回的是 xlsx，前端没有展示位。
        # 落一行日志，用户反馈「下拉里怎么只有名称」时能对得上。
        try:
            from . import applog
            log = applog.get("pp6kit.ai")
            for line in src_notices:
                log.info("%s", line)
        except Exception:            # noqa: BLE001 —— 日志坏了不许影响出 Excel
            pass
    src_cfgs = _source_configs(data.get("fields") or [])

    # ---- 表单：优先用 AI 设计好的布局
    # 有效字段集合要在表单/列表两段之前算好：
    # 列表那段在 form_data 为空时也要用到它，放在 if 里面会 NameError
    # （旧代码就踩过：AI 没返回表单布局时导出报 cannot access local variable 'valid'）
    valid = {f.code for f in bo.fields}
    CONTAINERS = {"grid", "grid_layout", "tabs", "card", "tablelayout", "label", "attachment"}

    form_data = payload.get("form")
    if form_data and form_data.get("fields"):
        form = Form(
            key=form_data.get("key") or f"create_{bo.code}",
            name=form_data.get("name") or f"新建{bo.name}",
            bo_code=bo.code,
            kind=FORM_CREATE,
            form_type="form",
            layout=int(form_data.get("layout") or 2),
            layout_style=("flat" if form_data.get("layoutStyle") == "flat" else "grouped"),
        )
        # 用户在预览里删过字段时，表单里可能还引用着它——剔除掉，
        # 否则会生成「控件 xxx 在对象字段中不存在」的坏表单
        # 下拉类控件的取值来源以「字段本身」为准：object → 对象编码，dic → 字典 key。
        # 预览往返里前端可能没带 valueSource，漏了它就成空下拉。
        #
        # ⚠ dic 的字典 key 是派生数据（对象编码_字段编码），**必须用当前对象算**，
        # 不能信预览里带过来的 valueSource——用户改过对象编码后，旧 key 全部失效，
        # 校验会报「控件引用的字典在对象里不存在」直接中止出包
        # （2026-11-12 用户截图：innovationPipeline 等四个控件全中招）。
        src_of = {}
        # 关联对象字段：新建表单里必须是 linkdata（详情表单另按对象字段重出）
        object_codes = {f.code for f in bo.fields if f.type_new == "object"}
        for f in bo.fields:
            if f.type_new == "dic":
                # dic 一律用 dict_key()（对象编码_字段编码），**绝不**采信
                # object_type——AI 路径里它可能还是裸字段编码（2026-11-12 实测：
                # 裸编码压过正确 key，整包 dic 控件的取值来源全错）。
                # dict_key 与 _dict_blocks 生成字典 / model.resolved 写 XML 用
                # 同一规则，三处天然一致。
                src_of[f.code] = f.dict_key()
            elif f.type_new == "object":
                src_of[f.code] = f.object_type
        for ff in form_data["fields"]:
            fname = (ff.get("name") or "").strip()
            ftype = ff.get("type") or "text"
            if fname and ftype not in CONTAINERS and fname not in valid:
                continue
            if fname in src_of and ftype in ("select", "dictionary", "linkdata", "selectObject"):
                # dic/object 控件：取值来源永远重算，陈旧值直接丢弃
                vs = src_of[fname]
                # 新建表单里的关联对象一律 linkdata（样本 config-sync-20261119184322）：
                # AI 常给 "select"，那是旧口径——带不了值来源、联动和过滤条件。
                if fname in object_codes:
                    ftype = "linkdata"
            else:
                vs = ff.get("valueSource") or src_of.get(fname) or ""
            form.fields.append(FormField(
                form_key=form.key,
                name=fname,
                label=ff.get("label") or fname,
                field_type=ftype,
                value_source=vs,
                cascade_parent=(ff.get("cascadeParent") or ""),
                placeholder=ff.get("placeholder") or "请输入",
                required=bool(ff.get("required")),
                readonly=bool(ff.get("readonly")),
                full_row=bool(ff.get("fullRow")),
                group=(ff.get("group") or "").strip(),
                # 值来源的联动映射：界面面板上配的「返回列 → 本表单控件」。
                # 没配就留空，生成阶段按物理列名自动对上（见 links._linkage）。
                linkage=((src_cfgs.get(fname) or {}).get("linkage") or []),
            ))
        # 条件隐藏：前端没带就为空，带了就落成规则（引用了不存在的控件直接扔掉）
        names = {ff.name for ff in form.fields}
        # 级联也兼容「整体给一张表」的形状：{"子字段": "父字段"}
        pairs = form_data.get("cascades") or {}
        if isinstance(pairs, dict):
            for child, parent in pairs.items():
                child = (child or "").strip()
                parent = (parent or "").strip()
                if child in names and parent in names:
                    for ff in form.fields:
                        if ff.name == child:
                            ff.cascade_parent = parent
                            break
        for rule in (form_data.get("hideRules") or []):
            if not isinstance(rule, dict):
                continue
            target = (rule.get("target") or "").strip()
            source = (rule.get("source") or "").strip()
            if target not in names or source not in names:
                continue
            op_raw = (rule.get("operator") or "等于").strip()
            operator = OP_BY_LABEL.get(op_raw, op_raw)
            if operator not in HIDE_OPERATORS:
                operator = "equal"
            value = str(rule.get("value") or "").strip()
            if operator not in EMPTY_OPERATORS and not value:
                continue
            form.hide_rules.append(HideRule(
                form_key=form.key, target=target, source=source,
                operator=operator, value=value,
                value_label=str(rule.get("valueLabel") or "").strip(),
            ))
        if form.fields:
            proj.forms.append(form)
        else:
            form_data = None

    if not (form_data and form_data.get("fields")):
        # 没带布局：退回「标准全套」，build/预览时会自动展开成三张表单
        proj.forms.append(Form(
            key=f"create_{bo.code}", name=f"新建{bo.name}", bo_code=bo.code,
            kind=FORM_BUNDLE, form_type="form", layout=2,
        ))
    else:
        # AI 只画「新建」这一张，详情表单得按对象字段补一张：
        #   · 关联对象在详情里必须是**原本字段类型** selectObject（样本
        #     PP6/表单/项目详情表单 1.zip），不是新建页那种带值来源的 linkdata；
        #   · 不补的话，AI 路径出的包没有详情页，列表「点开详情」无处可去。
        from dataclasses import replace as _replace

        from . import blueprint as _bp
        _base = proj.forms[-1] if proj.forms else None
        if _base is not None and _base.kind == FORM_CREATE and \
                not any(f.bo_code == bo.code and f.kind == FORM_DETAIL for f in proj.forms):
            proj.forms.append(_bp._make(
                bo, FORM_DETAIL, _replace(_base, fields=[]), group_src=_base))

    # ---- 列表：优先用 AI 配的列和视图
    list_data = payload.get("list")
    if list_data and list_data.get("columns"):
        cols = [c.get("code") if isinstance(c, dict) else str(c)
                for c in list_data["columns"]]
        # 同理：列 / 排序 / 分组字段被删掉后也要剔除
        cols = [c for c in cols if c and c in valid]
        sort_f = (list_data.get("sort") or "").split(" ")[0]
        if sort_f and sort_f not in valid:
            sort_f = ""
        title_f = list_data.get("titleField") or ""
        if title_f and title_f not in valid:
            title_f = ""
        proj.lists.append(ListDef(
            key=list_data.get("key") or f"{bo.code}_list",
            name=list_data.get("name") or f"{bo.name}列表",
            bo_code=bo.code,
            views=list_data.get("views") or ["grid"],
            columns=cols,
            sort_field=sort_f,
            sort_dir=("asc" if (list_data.get("sort") or "").endswith("asc") else "desc"),
            title_field=title_f,
            board_group=list_data.get("boardGroup") or "",
            gantt_start=((list_data.get("gantt") or {}).get("start") or ""),
            gantt_end=((list_data.get("gantt") or {}).get("end") or ""),
            # 级联详情字段：用户在预览的下拉里选的（前端 state 带回来）
            detail_field=(list_data.get("detailField") or "").strip(),
        ))
    else:
        proj.lists.append(ListDef(
            key=f"{bo.code}_list", name=f"{bo.name}列表", bo_code=bo.code,
            views=["grid"],
            columns=[f.code for f in bo.fields if f.type_new not in ("clob", "attachment")][:6],
        ))

    # 用户在确认区做的关系决定，写进这份 Excel（不覆盖已有显式值之外的东西）
    if relations_header:
        from . import relations as _relations
        decisions = _relations.parse_decisions(relations_header)
        if decisions:
            _relations.apply_confirmations(proj, decisions)

    # 值来源清单（用户关掉的 / 自己加的）：只在这一层做，因为它必须看到**完整**
    # 的 proj（对象 + 表单控件 + 字段级值来源配置）才能算准 key 与引用关系。
    # 名单为空（老会话没打开过「值来源」页签）→ 一行都不碰，产物与改造前一致。
    vs_items = payload.get("valueSources")
    if vs_items is not None:
        vs_notices = _apply_value_sources(proj, vs_items)
        if notices is not None:
            notices.extend(vs_notices)

    if probe is not None:
        # 只给 /api/ai/values 用：把构造好的 proj 交出去，不落 Excel。
        # 返回空 bytes 而不是 None，是为了不破坏现有调用方对 tuple 的解包。
        probe["proj"] = proj
        probe["bo"] = bo
        return b"", ""

    wb = build_workbook(proj)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), f"PP6-{bo.name}.xlsx"


def nlq_test() -> dict:
    cfg = _db_conf()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT VERSION()")
        version = cur.fetchone()[0]
        cur.close()
        pack = nlq_introspect.build_schema(conn, cfg["database"])
        nlq_introspect.annotate_liveness(conn, pack, list(pack.tables))
        nlq_introspect.annotate_columns(conn, pack)
        # 连上之后要告诉用户「这条连接能不能写」——界面上没有别的地方能看到
        readonly = nlq_guard.check_readonly_config(conn)
    finally:
        conn.close()
    key = f"{cfg.get('host')}:{cfg.get('port')}/{cfg.get('database')}"
    _SCHEMA_CACHE[key] = pack

    tables = sorted(pack.tables.values(), key=lambda t: -t.rows)
    return {
        "ok": True,
        "version": version,
        "database": pack.database,
        "tableCount": len(pack.tables),
        "columnCount": sum(len(t.columns) for t in pack.tables.values()),
        "dictCount": len(pack.dictionaries),
        "readonly": readonly,
        "tables": [
            {"name": t.name, "label": t.label, "rows": t.rows, "cols": len(t.columns)}
            for t in tables[:60]
        ],
        "templates": nlq_engine.templates(pack),
        "library": nlq_store.library_stats(),
        "semantic": nlq_semantic.glossary(pack),
    }


def _nlq_ask_impl(payload: dict, on_delta=None, on_event=None) -> dict:
    """engine: hybrid（默认）/ local / llm / auto / agent

    hybrid = 规则先跑，规则没把握才调 AI，AI 答对了顺便沉淀成示例。
    这是默认模式：简单问题秒出结果不花钱，复杂问题交给 AI。

    on_delta 非空时代表「流式调用」：AI 每吐一段就回调，前端能实时看到 SQL
    在长出来。规则命中的分支不会有 delta——那本来就是毫秒级返回。
    on_event 是 agent 模式的工具进度（正在查哪张表），与 delta 分开走。
    """
    question = (payload.get("question") or "").strip()
    if not question:
        raise ValueError("问题不能为空")
    # 第一道闸：问句层。破坏性意图/注入片段在这里就拒，**不产生任何 SQL**，
    # 也不去调模型——省一次调用，更重要的是不让注入有机会改写 WHERE。
    nlq_guard.screen_question(question)
    mode = (payload.get("engine") or "hybrid").lower()
    cfg = _llm_conf()
    has_key = bool(nlq_llm.active_channels(cfg))

    conn = _connect()
    try:
        pack = _schema()
        allowed = set(pack.tables) | nlq_guard.PARTY_TABLES | {"iform_cat_dic", "iform_cat_type"}

        # @ 点名的表：优先于打分器，也保证不被 schema 预算裁掉。
        # 解析完把 question 换成去掉 @xx 的干净版本，后续规则/模型都吃它。
        from .nlq import mentions as nlq_mentions
        pinned, question = nlq_mentions.parse(pack, question)

        # 缺时间范围时先追问一句，避免「新增/趋势」把全量历史算进去
        if not payload.get("clarified"):
            from .nlq import clarify as nlq_clarify
            clar = nlq_clarify.check(question)
            if clar:
                return {"sql": "", "columns": [], "rows": [],
                        "clarify": clar,
                        "source": "需要确认时间范围",
                        "origin": "clarify", "origin_label": "待确认",
                        "question": question}

        if mode == "agent":
            # 查证模式：模型自己 list/describe/sample/试跑，证据够了才落笔。
            # 不降级到规则——用户选了这个模式就是要 AI 查证，悄悄换成规则
            # 会拿到一个「看起来答了但其实没查证」的结果。
            from .nlq import agent as nlq_agent
            return nlq_agent.run(
                conn, pack, question, cfg, allowed,
                examples=nlq_store.pick_examples(
                    question,
                    tables=nlq_engine.select_tables(pack, question, pinned=pinned)),
                pinned=pinned,
                instructions=nlq_store.load_instructions(),
                on_event=on_event)

        if mode == "llm":
            return _ask_ai(conn, pack, question, cfg, learn=True, pinned=pinned,
                           on_delta=on_delta)

        if mode in ("local", "auto") and not has_key:
            return _ask_local(conn, pack, question, allowed, pinned=pinned)

        if mode == "local":
            return _ask_local(conn, pack, question, allowed, pinned=pinned)

        if mode == "auto":       # 旧行为：有 key 就直接用 AI
            try:
                return _ask_ai(conn, pack, question, cfg, learn=True, pinned=pinned,
                           on_delta=on_delta)
            except Exception as exc:
                res = _ask_local(conn, pack, question, allowed, pinned=pinned)
                res["source"] = f"本地规则（大模型不可用：{str(exc)[:60]}）"
                return res

        # ---- hybrid：规则先跑 ----
        try:
            res = _ask_local(conn, pack, question, allowed, pinned=pinned)
        except nlq_local.NotUnderstood as exc:
            # 规则引擎明确说解析不了（问句里没有任何对象名/字段名/句式线索）。
            # 不能硬编一条错 SQL 去查生产库——有模型就交给 AI，没有就如实报错。
            if not has_key:
                raise
            res = {"sql": "", "columns": [], "rows": [],
                   "source": f"规则没看懂这个问题（{exc}），已改由 AI 分析",
                   "origin": "rule", "origin_label": "规则生成",
                   "question": question}
        except nlq_guard.SqlRejected:
            # 安全闸拦下的（写了破坏性语句/越界表）：这不是"规则不够聪明"，
            # 是问题本身不能执行，换个模型也不该答。直接顶回去。
            raise
        except Exception as exc:
            # 规则生成的 SQL 跑不通：典型是 ONLY_FULL_GROUP_BY 下的非聚合列
            # （实测「每个项目阶段的数量」就死在这），或者库结构变了。
            # 把一条执行不了的 SQL 顶给用户是最差的结果——有模型就让它重来。
            if not has_key:
                raise
            res = {"sql": "", "columns": [], "rows": [],
                   "source": f"规则生成的 SQL 跑不通（{str(exc)[:60]}），已改由 AI 分析",
                   "origin": "rule", "origin_label": "规则生成",
                   "question": question}
        if not has_key:
            res["source"] += "（未配置大模型，仅用规则）"
            return res

        verdict = nlq_hybrid.judge(question, res.get("sql", ""),
                                   len(res.get("rows") or []),
                                   _expected_tables(pack, question),
                                   pack=pack)
        if not nlq_hybrid.should_try_ai(verdict, "hybrid"):
            res["verdict"] = "规则答对了"
            return res

        try:
            ai = _ask_ai(conn, pack, question, cfg, learn=True,
                         rule_sql=res.get("sql", ""),
                         rule_rows=len(res.get("rows") or []),
                         pinned=pinned, on_delta=on_delta)
        except Exception as exc:
            res["source"] += f"（AI 也失败：{str(exc)[:50]}）"
            res["ai_error"] = str(exc)[:120]
            res["origin"] = "rule"          # 最终还是规则的结果
            res["origin_label"] = "规则生成"
            return res

        # AI 0 行而规则有行：
        #   - 规则本来就被判「没答到点上」→ 不能把 200 行错结果端给用户，
        #     改用 AI 的 0 行 + 诊断，并说明为什么（2026-11-17「张三」实测）
        #   - 规则没被判错 → 保留规则结果，别把能用的答案换掉
        if not ai.get("rows") and res.get("rows"):
            if not verdict.ok:
                ai["source"] = f"AI 按更严格的条件重查（{verdict.note}），结果 0 行"
                ai["ruleSql"] = res.get("sql")
                ai["ruleRows"] = len(res.get("rows") or [])
                ai["rule_reason"] = verdict.note
                ai["uncertain"] = (
                    f"规则结果没答到点上（{verdict.note}），AI 重查 0 行；"
                    f"请核对筛选条件是否写得太严")
                # 去掉「查一下/帮我」前缀，否则会把「一下张三」当成名字
                q_name = re.sub(
                    r"^(?:请|帮我|麻烦)?\s*(?:查一下|查一查|查询|查|看看|看一下|列出|找出)\s*",
                    "", question)
                m = re.search(r"([\u4e00-\u9fa5]{2,4})\s*(?:负责|经办|提交|创建|处理)的",
                              q_name)
                if m and m.group(1) not in ("项目经理", "项目负责人", "负责人", "责任人"):
                    ai["hint"] = (f"库里可能没有「{m.group(1)}」这个人，"
                                  f"或该人员名下没有对应数据；可先查人员表核对姓名。")
                else:
                    ai.setdefault("hint", nlq_engine.diagnose(conn, pack, ai.get("sql", "")))
                return ai
            res["source"] += "；AI 没答出来，保留规则结果"
            res["ai_error"] = "AI 查询结果为空"
            res["origin"] = "rule"          # 呈现在眼前的是规则结果
            res["origin_label"] = "规则生成"
            res["uncertain"] = f"这条规则本来没把握（{verdict.note}），AI 也没查出来，请人工核对"
            return res

        # AI 接手：带上「为什么换 AI」和「规则原来的答案」，方便人工核对
        ai["source"] = nlq_hybrid.fallback_note(verdict)
        ai["rule_reason"] = verdict.note
        ai["ruleSql"] = res.get("sql")
        ai["ruleRows"] = len(res.get("rows") or [])
        return ai
    finally:
        conn.close()


def _expected_tables(pack, question: str) -> list[str]:
    """问句应该落在哪些表上（给 judge 用，判错表）。"""
    from .nlq import select
    try:
        ch = select.choose(pack, question, nlq_local._label_variants)
    except Exception:
        return []
    names = [ch.table.name]
    for _, n in ch.ranking[1:3]:
        if n in pack.tables:
            names.append(n)
    return names


def _ask_ai(conn, pack, question: str, cfg: dict, learn: bool = True,
            rule_sql: str = "", rule_rows: int = 0,
            pinned: list[str] | None = None,
            on_delta=None) -> dict:
    """走大模型；答对了就沉淀成示例，下次同类问题规则能直接命中。

    rule_sql / rule_rows 是规则引擎当时的结论：如果 AI 和规则**明显冲突**，
    就不沉淀——AI 也会错，错了再沉淀等于把错误固化。

    pinned 是用户 @ 点名的表，透传给选表和 schema 预算（点名表不被截断）。
    """
    # 传入选表结果，让示例挑选能按「表重合」加权（问工时就用工时的示例）
    try:
        want_tables = nlq_engine.select_tables(pack, question, pinned=pinned)
    except Exception:
        want_tables = pinned
    res = nlq_engine.ask(conn, pack, question, cfg,
                         nlq_store.pick_examples(question, tables=want_tables),
                         pinned=pinned,
                         instructions=nlq_store.load_instructions(),
                         on_delta=on_delta)
    res["source"] = "大模型生成" + (f"（{res['channel']}）" if res.get("channel") else "")
    res["origin"] = "ai"                      # 大模型生成
    res["origin_label"] = "AI 生成"
    res["verified"] = False
    res["model"] = res.get("channel") or ""
    res["question"] = question
    if not res.get("rows"):
        res["hint"] = nlq_engine.diagnose(conn, pack, res["sql"])
        return res

    if learn and nlq_hybrid.conflicts(rule_sql, rule_rows, res["sql"]):
        res["conflict"] = "AI 结论和规则不一致，这条没沉淀（请人工核对）"
        return res

    if learn and nlq_hybrid.learn_worthy(question, res["sql"], len(res["rows"])):
        nlq_store.add_example(question, res["sql"])
        res["learned"] = True
    return res


def _ask_local(conn, pack, question: str, allowed: set, allowed_extra: bool = False,
               pinned: list[str] | None = None) -> dict:
    """本地规则引擎：不依赖大模型。

    先看有没有「AI 教过」的相似问题——有就直接用它的 SQL，
    这是沉淀机制真正生效的地方：AI 只答一次，之后都走规则。
    """

    def _touches_pinned(sql: str) -> bool:
        """这条 SQL 有没有碰到用户 @ 点名的表。

        点名了表却拿到一条查别的表的 SQL，比不答更糟——用户会以为
        「AI 听懂了」，把结果当真。所以模板/历史答案只要没沾上点名表，
        就让路给 local.build 重新按点名表构造。
        没点名时恒为 True（不影响原有行为）。
        """
        if not pinned:
            return True
        refs = set(nlq_guard.referenced_tables(sql))
        return any(n in refs for n in pinned)

    # ① 人工修正过这个问句 → 直接用人改的（人比模板权威）
    fixed = nlq_store.find_correction(question)
    if fixed:
        try:
            safe = nlq_guard.validate(fixed["sql"], allowed)
            cols, rows = nlq_engine.run_sql(conn, safe)
            if rows:
                tip = fixed.get("note") or ""
                return {"sql": safe, "columns": cols, "rows": rows,
                        "source": "你之前手工修正过的写法" + (f"（原因：{tip}）" if tip else ""),
                        "note": tip,
                        "origin": "corrected",
                        "origin_label": "人工修正",
                        "verified": True,
                        "question": question}
        except Exception:
            pass          # 改过的 SQL 也失效了（表结构变了），往下走

    # ② 高置信语义模板优先：报表/团队这类是验证过的确定性 SQL，
    # 而历史示例可能是早期答错的那一版（「超支的项目有哪些」就被带偏过）
    try:
        tpl = nlq_local.match_template(pack, question)
    except Exception:
        tpl = None
    if tpl and _touches_pinned(tpl[0]):
        sql, note = tpl
        safe = nlq_guard.validate(sql, allowed)
        cols, rows = nlq_engine.run_sql(conn, safe)
        if rows:
            return {"sql": safe, "columns": cols, "rows": rows,
                    "source": note,
                    "origin": "rule",
                    "origin_label": "报表模板",
                    "question": question}

    similar = nlq_store.find_similar(question)
    if similar and _touches_pinned(similar.get("sql", "")):
        try:
            safe = nlq_guard.validate(similar["sql"], allowed)
            cols, rows = nlq_engine.run_sql(conn, safe)
            if rows:
                # 这条可能是用户手工改过的，标注要如实
                by_human = similar.get("source") == "corrected"
                tip = similar.get("note") or "" if by_human else ""
                return {"sql": safe, "columns": cols, "rows": rows,
                        "source": ("沿用手工修正过的写法" if by_human else "复用已验证问答")
                                  + (f"（原因：{tip}）" if tip else ""),
                        "note": tip,
                        "origin": "corrected" if by_human else "memory",
                        "origin_label": "人工修正" if by_human else "复用的 AI 答案",
                        "verified": bool(similar.get("verified")) or by_human,
                        "source_question": similar.get("question", ""),
                        "question": question}
        except Exception:
            pass          # 历史 SQL 失效（表结构变了），继续走规则

    sql, note = nlq_local.build(pack, question, pinned=pinned)
    safe = nlq_guard.validate(sql, allowed)
    cols, rows = nlq_engine.run_sql(conn, safe)
    out = {"sql": safe, "columns": cols, "rows": rows,
           "source": note,
           "origin": "rule",                 # 本地规则生成
           "origin_label": "规则生成",
           "question": question}
    if not rows:
        out["hint"] = nlq_engine.diagnose(conn, pack, safe)
    return out


def nlq_ask(payload: dict) -> dict:
    """对外入口：在结果上补一句「查询理解」，前端直接展示。"""
    res = _nlq_ask_impl(payload)
    try:
        if res.get("sql"):
            res["summary"] = nlq_engine.explain_sql(res["sql"], _schema())
    except Exception:
        pass
    return explain_result(res)


def nlq_ask_stream(payload: dict):
    """SSE 流式版 /api/nlq/ask：生成过程实时吐字，其余流程与同步版完全一致。

    为什么用线程 + 队列而不是把整条链路改成生成器：
    `_nlq_ask_impl` 里套着规则引擎 → 判定 → AI → 冲突检测 → 沉淀五段逻辑，
    改成 yield 形式等于把它重写一遍，回归风险远大于收益。这里只多开一个
    线程跑原来那套，主线程负责把 delta 转成 SSE 事件——业务逻辑一行没动。

    事件时序：
        {"stage": "start"}                     请求已受理
        {"stage": "delta", "delta": "SEL"}     AI 每吐一段（可能没有，规则命中时直接 done）
        {"stage": "done",  "result": {...}}    完整结果，结构与 /api/nlq/ask 相同
        {"stage": "error", "error": "..."}     失败原因
    """
    import queue as _queue
    import threading

    box: _queue.Queue = _queue.Queue()

    def _worker() -> None:
        try:
            res = _nlq_ask_impl(
                payload,
                on_delta=lambda piece: box.put({"stage": "delta", "delta": piece}),
                on_event=lambda ev: box.put(ev))
            box.put({"stage": "done", "result": explain_result(res)})
        except Exception as exc:      # 闸门拒绝 / 未配 Key / 所有渠道失败
            box.put({"stage": "error", "error": str(exc)})
        finally:
            box.put(None)

    yield {"stage": "start"}
    threading.Thread(target=_worker, daemon=True, name="nlq-ask-stream").start()
    while True:
        ev = box.get()
        if ev is None:
            break
        yield ev


def nlq_run(payload: dict) -> dict:
    sql = (payload.get("sql") or "").strip()
    if not sql:
        raise ValueError("SQL 不能为空")
    conn = _connect()
    try:
        pack = _schema()
        allowed = set(pack.tables) | nlq_guard.PARTY_TABLES | {"iform_cat_dic", "iform_cat_type"}
        safe = nlq_guard.validate(sql, allowed)
        cols, rows = nlq_engine.run_sql(conn, safe)
        out = {"sql": safe, "columns": cols, "rows": rows}
        try:
            out["summary"] = nlq_engine.explain_sql(safe, pack)
        except Exception:
            pass
        return explain_result(out)
    finally:
        conn.close()


def nlq_verify(payload: dict) -> dict:
    """人工确认「这条 SQL 是对的」→ 进已验证库，之后优先复用。"""
    q = (payload.get("question") or "").strip()
    sql = (payload.get("sql") or "").strip()
    out = nlq_store.verify_example(q, sql)
    return {"ok": True, **out}


def nlq_save_example(payload: dict) -> dict:
    q = (payload.get("question") or "").strip()
    sql = (payload.get("sql") or "").strip()
    if q and sql:
        # 示例会被 `_ask_local` 在下次同类问题时**直接取出来执行**。
        # 这里不校验，等于允许把一条 DROP 存进"记忆"里，虽然取出时还会过 validate，
        # 但让危险语句落进示例库本身就是隐患（也更容易被误当成"验证过的写法"）。
        if not nlq_guard.is_read_only(sql):
            raise nlq_guard.SqlRejected(
                "只有只读查询（SELECT）能存为示例，这条 SQL 不是。")
        nlq_store.add_example(q, sql)
    return {"ok": True, "examples": len(nlq_store.all_examples())}


def nlq_save_correction(payload: dict) -> dict:
    """用户手工改对了机器给的 SQL → 记下来，以后同问直接用他改的。

    比「存为示例」更严格：必须能跑通、且真的出数据，避免把改坏的 SQL 学进去。
    """
    q = (payload.get("question") or "").strip()
    sql = (payload.get("sql") or "").strip()
    original = (payload.get("original_sql") or "").strip()
    note = (payload.get("note") or "").strip()
    if not q or not sql:
        raise ValueError("问题和 SQL 都要有")
    if original and sql.strip() == original.strip():
        raise ValueError("SQL 没改动，不需要记录修正")

    conn = _connect()
    try:
        pack = _schema()
        allowed = set(pack.tables) | nlq_guard.PARTY_TABLES | {"iform_cat_dic", "iform_cat_type"}
        safe = nlq_guard.validate(sql, allowed)
        cols, rows = nlq_engine.run_sql(conn, safe)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not rows:
        # 能跑但没数据：可能是"本来就该是 0 行"，也可能是条件写歪了。
        # 这种情况不入库，但要如实告诉用户，别让他以为存上了。
        return {"ok": False, "error": "这条 SQL 能执行但返回 0 行，没有记录。确认要存请先让它能查出数据。",
                "columns": cols, "rows": []}

    res = nlq_store.save_correction(q, safe, original, note)
    return {"ok": True, **res, "columns": cols, "rows": rows,
            "note": "已记下你的改法" + ("和原因" if note else "") + "，下次同样的问题会直接用这条"}


def template_bytes(blank: bool = False) -> bytes:
    """填写模板 xlsx。

    ``blank=True`` 出**空白模板**：14 个页签、表头与说明行齐全，一条数据都不预填，
    给「先下模板手填」的用户（2026-11-18 恢复这个入口，之前随入口改版一起下线过）。
    默认仍是带「采购申请」示例的版本，「载入示例看效果」继续用它。
    两者页签、列顺序、说明行完全一致，只差有没有示例数据。
    """
    wb = build_workbook(Project() if blank else demo_project())
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- 响应头契约（P0-4 / P0-5）
# 两个纯函数：只从 zip 原始字节「读」结构事实，绝不写 zip、绝不参与 blob 生成。
# 供 X-Package-Summary（/api/inspect 200 与 400）与 X-Build-Report（/api/build 200）复用。
# 新增信息只走「zip comment（已有）+ 界面 + 响应头」三个通道，交付包内不新增任何条目。

_ZIP_NOTE_PREFIX = "CONFIG_SYNC_NOTE:"
_ZIP_LEGAL_DIRS = ("bo", "form", "data_template", "dataset")
_ZIP_IMPORT_PATH = "系统管理 › 发布管理 › 导入同步包"
_ZIP_COMMENT_LIMIT = 512          # UTF-8 字符数，不是字节
_ZIP_NAME_LIMIT = 120
_ZIP_ENTRY_LIMIT = 30
_ZIP_HEADER_LIMIT = 8192          # base64 后单头硬上限


def _header_json(obj: dict) -> str:
    """dict -> ASCII 安全的 HTTP 头值（base64 of UTF-8 JSON）。

    HTTP 头只能写 latin-1（见 _send 原注释），中文 / 换行 / 引号直接进头会
    UnicodeEncodeError。编码定死：
        base64(json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
               .encode("utf-8")).decode("ascii")
    返回字符集仅 [A-Za-z0-9+/=]。前端必须用
    atob -> Uint8Array -> TextDecoder("utf-8") -> JSON.parse 解码；
    严禁 JSON.parse(atob(raw))（中文必乱码），且与既有 X-Filename 的
    quote()/decodeURIComponent() 是两套编码，不得混用。
    """
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _zip_report(data: bytes, error: Exception | None = None) -> dict:
    """纯只读：从 zip 字节提取「结构事实」，供两个新增响应头复用。

    绝不写 zip、绝不调用 build_zip()/inspect_zip()、绝不修改传入字节。
    字段契约见「增量架构设计」§4.1 / §4.2 / §4.6：
      ok / problem / message / empty / entries / entryCount / counts /
      comment / commentTruncated / truncated / entriesTotal / summaryOnly / valid
    """
    result: dict = {
        "ok": True, "problem": None, "empty": True,
        "entries": [], "entryCount": 0, "counts": {},
        "comment": "", "commentTruncated": False,
        "truncated": False, "valid": False,
    }
    if error is not None:
        result["ok"] = False
        result["problem"] = "not_zip"
        result["message"] = str(error)
        return result

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            # comment 原文：UTF-8 replace 解码；build_zip() 会把整段生成报告挂进
            # comment，可达数 KB，按字符截断且必须完整保留 CONFIG_SYNC_NOTE: 前缀。
            comment = (zf.comment or b"").decode("utf-8", "replace")
            if len(comment) > _ZIP_COMMENT_LIMIT:
                head = _ZIP_NOTE_PREFIX if comment.startswith(_ZIP_NOTE_PREFIX) else ""
                tail = "…（已截断）"
                keep = max(_ZIP_COMMENT_LIMIT - len(head) - len(tail), 0)
                comment = head + comment[len(head):len(head) + keep] + tail
                result["commentTruncated"] = True
            result["comment"] = comment

            infos = [i for i in zf.infolist() if not i.is_dir()]
            result["entryCount"] = len(infos)
            counts: dict[str, int] = {}
            entries: list[dict] = []
            for info in infos:
                name = info.filename[:_ZIP_NAME_LIMIT]
                kind = name.split("/", 1)[0] if "/" in name else "(root)"
                counts[kind] = counts.get(kind, 0) + 1
                basename = name.rsplit("/", 1)[-1]
                m = re.match(r"^(\d{3})-", basename)
                seq = int(m.group(1)) if m else None
                utf8 = True
                if basename.lower().endswith(".xml"):
                    try:
                        with zf.open(info) as fp:
                            chunk = fp.read(4096)
                        try:
                            chunk.decode("utf-8")
                        except UnicodeDecodeError as ue:
                            # 4096 边界正好切断一个多字节字符时是
                            # "unexpected end of data"，不算真的编码错误
                            utf8 = bool(ue.reason == "unexpected end of data"
                                        and ue.end == len(chunk))
                    except Exception:
                        # 读不出来（加密 / 压缩方式不支持）也标成不可解码，
                        # 不抛异常、不阻断响应头生成
                        utf8 = False
                entries.append({
                    "name": name, "size": int(info.file_size),
                    "kind": kind, "seq": seq, "utf8": utf8,
                })
            result["counts"] = counts

            # D4：empty = 可反解的 bo/form/data_template XML 条数 === 0
            usable = sum(1 for e in entries
                         if e["kind"] in ("bo", "form", "data_template")
                         and e["name"].lower().endswith(".xml"))
            result["empty"] = usable == 0

            # 结构事实：前端 classifyInspect() 仍按 §6.3 优先级自行复判，
            # 这里只把「多余条目 / 套层 / 编码 / 空」如实写进 problem。
            white = set(_ZIP_LEGAL_DIRS)

            def _nested(name: str) -> bool:
                """外层多套了一层目录：首段不是合法目录，但路径里有 /bo/ 之类。"""
                low = name.lower()
                if not low.endswith(".xml"):
                    return False
                if low.split("/", 1)[0] in white:
                    return False
                return any("/%s/" % d in low
                           for d in ("bo", "form", "data_template"))

            # 套层必须优先判：套层包的顶层目录同样不在白名单，先判 extra
            # 会让 nested_dir 永远不可达，并把用户引向「删 manifest/txt」
            # 这种根本解决不了问题的修复动作（线上原话也不同）。
            nested = any(_nested(e["name"]) for e in entries)
            extra = any(e["kind"] not in white and not _nested(e["name"])
                        for e in entries)
            bad_utf8 = any(not e["utf8"] for e in entries)
            if nested:
                result["problem"] = "nested_dir"
            elif extra:
                result["problem"] = "extra_entries"
            elif bad_utf8:
                result["problem"] = "encoding"
            elif result["empty"]:
                result["problem"] = "empty"
            else:
                result["problem"] = None

            legal = re.compile(
                r"^(bo|form|data_template|dataset)/\d{3}-[a-z0-9_]+\.xml$")
            result["valid"] = bool(
                not result["empty"] and result["problem"] is None
                and all(legal.match(e["name"]) for e in entries)
            )

            # §4.6 阶梯截断：30 条 -> 10 条 -> 仅统计，
            # 硬保证 base64 后 <= 8192 字节；截断对前端可见。
            shown = entries[:_ZIP_ENTRY_LIMIT]
            if len(entries) > len(shown):
                result["truncated"] = True
                result["entriesTotal"] = len(entries)
            result["entries"] = shown

            def _size() -> int:
                packed = json.dumps(result, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")
                return len(base64.b64encode(packed))

            if _size() > _ZIP_HEADER_LIMIT:
                result["entries"] = entries[:10]
                result["truncated"] = True
                result["entriesTotal"] = len(entries)
                if _size() > _ZIP_HEADER_LIMIT:
                    result["entries"] = []
                    result["summaryOnly"] = True
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        result["ok"] = False
        result["problem"] = "not_zip"
        result["message"] = str(exc)
    except Exception as exc:                      # 绝不因读结构失败而阻断路由
        result["ok"] = False
        result["problem"] = "other"
        result["message"] = str(exc)
    return result


# ---------------------------------------------------------------- HTTP

# 默认只绑回环，Host 必须是本机地址；--lan 模式下每个服务实例发一次性口令。
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
_LAN_COOKIE = "pp6_lan"


def _host_only(value: str) -> str:
    """Host 头去掉端口并转小写：`[::1]:8770` → `::1`，`192.168.1.5:8770` → `192.168.1.5`。"""
    text = (value or "").strip().lower()
    if text.startswith("["):
        return text[1:text.index("]")] if "]" in text else text[1:]
    return text.split(":", 1)[0]


def _origin_host(origin: str) -> str:
    """Origin 只认 `http(s)://host[:port]`，返回 host[:port]；`null` 与畸形值返回空串。"""
    m = re.match(r"^https?://([^/?#]+)", (origin or "").strip(), re.I)
    return m.group(1).lower() if m else ""


def _cookie_value(header: str, name: str) -> str:
    for part in (header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value.strip()
    return ""


class Handler(BaseHTTPRequestHandler):
    server_version = "pp6kit"
    _pending_cookie = ""   # 本次响应要下发的 Cookie（--lan 模式凭链接里的口令换）

    def log_message(self, fmt, *args):  # 静音访问日志
        pass

    # --- 工具
    def _send(self, status: int, body: bytes, ctype: str, filename: str | None = None,
              extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if filename:
            # HTTP 头只能是 latin-1：中文名按 RFC 5987 编码，同时给个 ASCII 兜底名
            fallback = "pp6-export" + Path(filename).suffix
            self.send_header(
                "Content-Disposition",
                f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}",
            )
            self.send_header("X-Filename", quote(filename))
        # 纯附加：默认 None -> 所有既有调用零变化；extra 值均为 ASCII 安全的 base64
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        if self._pending_cookie:
            self.send_header("Set-Cookie", self._pending_cookie)
        self.end_headers()
        self.wfile.write(body)

    # --- 来源闸
    def _cross_site(self) -> str:
        """请求不是「用户在自己的界面上操作」时返回拒绝原因，否则返回空串。

        挡本机服务的两类越权（2026-11-24）：
        1. 用户浏览器里**别的网页**向本服务发请求。界面从没发过 CORS 头，对方读不到
           响应，但能触发出包、查库、保存配置、甚至触发升级替换程序；
        2. DNS 重绑定：恶意域名解析到 127.0.0.1，响应就变得可读——靠 Host 判定。

        放行规则（与桌面壳 WKWebView、浏览器模式、--headless 自检都兼容）：
        - 带 Origin 的请求，Origin 必须与 Host 一致（界面自身就是这样发的）；
        - `Sec-Fetch-Site: cross-site` 一律拒（现代浏览器都会带，不带也不影响）；
        - 默认回环模式：Host 必须是本机地址；
        - --lan 模式：Host 管不了（本来就是局域网 IP），改为口令 Cookie。
        """
        headers = self.headers
        if (headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
            return "跨站请求已拒绝"
        origin = headers.get("Origin")
        if origin and _origin_host(origin) != (headers.get("Host") or "").strip().lower():
            return "请求来源与本服务不一致"
        token = getattr(self.server, "lan_token", "")
        if not token:
            if _host_only(headers.get("Host")) not in _LOOPBACK_HOSTS:
                return "只接受通过本机地址访问"
            return ""
        supplied = _cookie_value(headers.get("Cookie"), _LAN_COOKIE).encode()
        if hmac.compare_digest(supplied, token.encode()):
            return ""
        # 首次进入：链接里带 ?token= 的首页请求放行，并用 Set-Cookie 记下口令。
        if self.command == "GET" and self.path.split("?")[0] in ("/", "/index.html"):
            given = parse_qs(urlparse(self.path).query).get("token", [""])[0]
            if hmac.compare_digest(given.encode(), token.encode()):
                self._pending_cookie = (
                    f"{_LAN_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax")
                return ""
        return "缺少访问口令：请用启动时打印的带口令链接打开"

    def _deny(self, reason: str) -> None:
        self._send(403, json.dumps({"ok": False, "error": reason},
                                  ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _quit_local(self) -> None:
        """让这个进程退出：新版接管的握手口，也是「退出应用」的入口。

        会让进程直接消失，所以只认两样东西：回环来源 + 启动时写进
        instance.json 的令牌。同网段（--lan）和浏览器里的其它页面都碰不到。
        """
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self._json({"ok": False, "error": "只允许在本机操作"}, 403)
            return
        expected = getattr(self.server, "instance_token", "")
        given = self.headers.get("X-PP6-Token") or ""
        if not expected or not hmac.compare_digest(given.encode(), expected.encode()):
            self._json({"ok": False, "error": "缺少实例令牌"}, 403)
            return

        hook = getattr(self.server, "on_quit", None)
        self._json({"ok": True, "pid": os.getpid()})

        def _bye() -> None:
            try:
                if hook:
                    hook()          # 窗口模式：destroy() 结束事件循环
            except Exception:
                traceback.print_exc()
            time.sleep(0.2)
            try:
                from . import applog, instance
                instance.clear_registry()   # os._exit 不跑 atexit，自己把登记表擦掉
                applog.get("pp6kit.app").info("收到退出请求（来自另一个实例或前端），进程退出")
            except Exception:
                pass
            os._exit(0)             # 浏览器模式没人收尾，直接退

        threading.Timer(0.25, _bye).start()

    def _json(self, payload: dict, status: int = 200,
              extra: dict | None = None) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", extra=extra)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("没有收到文件内容")
        if length > MAX_UPLOAD:
            raise ValueError("文件超过 30MB，请精简后再传")
        return self.rfile.read(length)

    def _sse(self, events) -> None:
        """SSE 流式响应：不设 Content-Length，靠连接关闭界定结尾。

        BaseHTTPRequestHandler 默认 HTTP/1.0，无 Content-Length 时响应体就是
        「写到关闭为止」，浏览器 fetch 的 ReadableStream 会边收边给——这正是
        流式需要的语义，不必自己拼 chunked 帧。

        X-Accel-Buffering: no 是给 Nginx 之类反代用的；本机直连无害。
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for ev in events:
                payload = "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
                self.wfile.write(payload.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # 用户关了页面或点了停止：安静收场，不要往 stderr 喷栈
            return

    # --- 路由
    def do_GET(self) -> None:
        reason = self._cross_site()
        if reason:
            self._deny(reason)
            return
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            # 首页同样不能留缓存：它引用的 JS/CSS 名字是固定的，缓存住首页
            # 就会一直加载旧脚本（改完界面「看不到变化」的另一个来源）。
            self._send(200, (STATIC / "index.html").read_bytes(),
                       "text/html; charset=utf-8",
                       extra={"Cache-Control": "no-cache, must-revalidate"})
        elif path.startswith("/static/"):
            name = path[len("/static/"):]
            target = (STATIC / name).resolve()
            if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
                self._send(404, b"not found", "text/plain")
                return
            ctype = {
                ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".svg": "image/svg+xml",
            }.get(target.suffix, "application/octet-stream")
            # 本地工具没有构建流程，前端改完刷新就该看到——浏览器默认会按
            # 无校验头做启发式缓存，结果改了 CSS/JS 刷新还是旧样子（排查过一次）
            self._send(200, target.read_bytes(), ctype,
                       extra={"Cache-Control": "no-cache, must-revalidate"})
        elif path == "/api/environments":
            self._json({**environments.read(), "status": environments.snapshot()})
        elif path == "/api/version":
            # 关于页与更新检查共用：版本不再写死在 HTML 里
            from . import version
            self._json({"ok": True, **version.build_info()})
        elif path == "/api/update/check":
            # 只查不装：手动点「检查更新」时 force=1 绕过缓存
            from urllib.parse import parse_qs, urlparse
            from . import updater
            qs = parse_qs(urlparse(self.path).query)
            force = (qs.get("force") or [""])[0].lower() in ("1", "true", "yes")
            self._json(updater.check(force=force))
        elif path == "/api/history" or path.startswith("/api/history/"):
            from . import history
            try:
                if path == "/api/history/compare":
                    from urllib.parse import parse_qs, urlparse
                    query = parse_qs(urlparse(self.path).query)
                    self._json(history.compare(query.get("left", [""])[0], query.get("right", [""])[0]))
                elif path == "/api/history":
                    self._json({"items": history.recent()})
                else:
                    _, _, _, key, filename = path.split("/")
                    data = history.read(key, filename)
                    self._send(200, data, "application/octet-stream", filename,
                               extra={"Cache-Control": "no-store"})
            except (ValueError, OSError):
                self._json({"error": "任务记录不存在或无法读取"}, 404)
        elif path == "/api/nlq/config":
            self._json(nlq_config())
        elif path == "/api/nlq/test":
            try:
                self._json({"ok": True, **nlq_test()})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 400)
        elif path == "/api/objects":
            # 业务对象目录：接了库就是目标库里的真实对象，没接就是随包快照
            self._json({"ok": True, **objects.status(), "objects": objects.ensure()})
        elif path == "/api/objects/fields":
            # 值来源面板：目标对象有哪些列能当返回列 / 过滤列
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            self._json(object_field_options((qs.get("code") or [""])[0]))
        elif path == "/api/project-snapshot":
            # 关联项目要用「项目对象字段」：优先库、其次随包、再次用户上传的项目 zip。
            # 前端拿 dbConfigured 判断要不要弹「导入项目包」。
            self._json(project_snapshot_status())
        elif path == "/api/link-target":
            # 关联「其它业务对象」：按用户输入的名称在库里精确核对，
            # 顺带回物理表名与表说明（iform_bo_table.comment_）。
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            self._json({"ok": True, **link_target_status(
                (qs.get("name") or [""])[0],
                (qs.get("code") or [""])[0])})
        elif path == "/api/objects/lookup":            # 用户填了名字 → 查库里有没有：有就返回 id 和已有模板 key
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            name = (qs.get("name") or [""])[0]
            self._json({"ok": True, "query": name, **objects.lookup(name)})
        elif path == "/api/template":
            # ?blank=1 给空白模板（只有表头 + 说明行），默认给带示例的填写模板。
            # 两个入口并存：手填走空白，想先看长什么样走「载入示例看效果」。
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            blank = (qs.get("blank") or [""])[0].lower() in ("1", "true", "yes")
            self._send(200, template_bytes(blank),
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       "PP6-填写模板-空白.xlsx" if blank else "PP6-填写模板.xlsx")
        elif path == "/api/patch/fields-template":
            self._send(200, bo_patch.fields_template_bytes(),
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       "PP6-新增字段表.xlsx")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        reason = self._cross_site()
        if reason:
            self._deny(reason)
            return
        try:
            if self.path == "/api/quit":
                self._quit_local()
                return
            if self.path == "/api/update/stream":
                # 下载/替换要几十秒，用 SSE 把进度推给前端；替换成功时
                # updater 会在响应写完后自行退出进程（脚本等着接手）。
                # 只允许本机触发：--lan 模式下不该让同网段的人替换你的程序。
                if self.client_address[0] not in ("127.0.0.1", "::1"):
                    self._json({"ok": False, "error": "只允许在本机升级"}, 403)
                    return
                from . import updater
                self._sse(updater.install_stream(self.server.server_address[1]))
                return
            if self.path == "/api/parse":
                # 「关联项目」开关走同一个决策头。/校验放在路由层：头非法是**请求**
                # 的问题（与 /api/build 4xx 一个口径），不能被 _safe_parse 的
                # 「坏文件 → 200 + ok:false」兜底吞掉，否则前端只看到一句话。
                pl_header = _projlink_header(self.headers)
                if pl_header:
                    from .projlink import parse_decisions as _pl_decisions
                    _pl_decisions(pl_header)
                self._json(self._safe_parse(pl_header))
            elif self.path.startswith("/api/environments/"):
                self._json(environments.update(self.path.rsplit("/", 1)[-1], json.loads(self._body())))
            elif self.path == "/api/build":
                environment = environments.snapshot(self.headers.get("X-Environment-Id"))
                source = self._body()
                body, name = build_zip(source, _codes(self.headers),
                                       _relations_header(self.headers),
                                       _object_scope(self.headers),
                                       _projlink_header(self.headers),
                                       _workflow_all(self.headers))
                # X-Build-Report 的数据从「返回的 zip 字节」实测读取，不是推测；
                # build_zip() 本身一行未动，blob / X-Filename / Content-* 全不变。
                extra = None
                try:
                    report = _zip_report(body)
                    # Q1 口径未确认：4 个数量列固定 bo/form/data_template/dataset，
                    # 缺的补 0，禁止臆造线上列名。
                    report["counts"] = {
                        k: int(report["counts"].get(k, 0))
                        for k in ("bo", "form", "data_template", "dataset")
                    }
                    report.update({
                        "ok": True,
                        "packageName": name,
                        "status": "待导入",
                        "time": datetime.now().isoformat(timespec="seconds"),
                        "importPath": _ZIP_IMPORT_PATH,
                    })
                    report["valid"] = bool(
                        report["valid"] and report["comment"].startswith(_ZIP_NOTE_PREFIX))
                    # 附加字段可能把头撑过 8KB：逐级降 entries -> 仅统计
                    if len(_header_json(report)) > _ZIP_HEADER_LIMIT and report.get("entries"):
                        report["entries"] = report["entries"][:10]
                        report["truncated"] = True
                        report["entriesTotal"] = report["entryCount"]
                    if len(_header_json(report)) > _ZIP_HEADER_LIMIT:
                        report["entries"] = []
                        report["summaryOnly"] = True
                    extra = {"X-Build-Report": _header_json(report)}
                except Exception:
                    extra = None                  # 头是纯附加，任何异常都不阻断下载
                from . import history
                extra = extra or {}
                try:
                    params = {k: self.headers.get(k) for k in (
                        "X-Modules", "X-Relations", "X-Object-Scope",
                        "X-Project-Link", "X-Workflow-All") if self.headers.get(k) is not None}
                    extra["X-History-Id"] = history.save(source, body, name, params, _zip_report(body), environment)
                except Exception as exc:
                    diagnostics.failure("history-save", exc)
                    extra["X-History-Status"] = "failed"
                self._send(200, body, "application/zip", name, extra=extra)
            elif self.path.split("?")[0] == "/api/confirmed-excel":
                # 必须**先剥掉 query 再比**：前端带的是 `?name=xxx.xlsx`，
                # 直接 `self.path == "/api/confirmed-excel"` 永远不成立 →
                # 按钮点了必 404（2026-11-14 加「关联项目」开关时发现的老 bug，
                # 从初始提交起就在；不修的话确认版 Excel 这条路根本走不到，
                # 页面上的开关决策也就写不回 Excel）
                from urllib.parse import parse_qs, urlparse
                qs = parse_qs(urlparse(self.path).query)
                body, name = confirmed_excel(
                    self._body(), _relations_header(self.headers),
                    (qs.get("name") or [""])[0],
                    _projlink_header(self.headers))
                self._send(200, body,
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           name)
            elif self.path == "/api/nlq/save-config":
                self._json(nlq_save_config(json.loads(self._body())))
            elif self.path == "/api/nlq/test-llm":
                self._json(nlq_test_llm(json.loads(self._body())))
            elif self.path == "/api/nlq/ask":
                self._json({"ok": True, **nlq_ask(json.loads(self._body()))})
            elif self.path == "/api/nlq/ask-stream":
                # 流式：先解析完 body 再进 SSE，避免 _sse 已经开始写响应后
                # 才发现 JSON 有问题（那时改不了状态码了）
                self._sse(nlq_ask_stream(json.loads(self._body())))
            elif self.path == "/api/nlq/run":
                self._json({"ok": True, **nlq_run(json.loads(self._body()))})
            elif self.path == "/api/nlq/verify":
                self._json(nlq_verify(json.loads(self._body())))
            elif self.path == "/api/nlq/example":
                self._json(nlq_save_example(json.loads(self._body())))
            elif self.path == "/api/nlq/correct":
                self._json(nlq_save_correction(json.loads(self._body())))
            elif self.path == "/api/inspect":
                # 局部 try/except：成功 200 + 原 blob + 原 X-Filename + X-Package-Summary；
                # 失败 400 + 原 JSON body 逐字不变 + X-Package-Summary(ok:false)，
                # 后 return，避免落到全局 except 丢头。inspect_zip() 一行未动。
                raw = b""
                try:
                    raw = self._body()
                    summary = _zip_report(raw)
                    body, name = inspect_zip(raw)
                except Exception as exc:
                    traceback.print_exc()
                    self._json(
                        {"ok": False, "error": str(exc)}, 400,
                        extra={"X-Package-Summary": _header_json(_zip_report(raw, exc))})
                    return
                self._send(200, body,
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           name,
                           extra={"X-Package-Summary": _header_json(summary)})
            elif self.path == "/api/patch/inspect":
                self._json(patch_inspect(self._body()))
            elif self.path == "/api/patch/fields":
                self._json(patch_fields(self._body()))
            elif self.path.split("?")[0] == "/api/patch/fields/free":
                # 任意 Excel → 候选字段（不落库）。模板路径 /api/patch/fields 一行不动。
                from urllib.parse import parse_qs, unquote, urlparse
                from . import ai_patch
                qs = parse_qs(urlparse(self.path).query)
                self._json(ai_patch.free_fields(
                    self._body(), _llm_conf(),
                    filename=unquote(self.headers.get("X-Filename") or ""),
                    sheet=(qs.get("sheet") or [""])[0]))
            elif self.path == "/api/patch/dry-run":
                self._json(patch_dry_run(json.loads(self._body())))
            elif self.path == "/api/patch/build":
                body, name = patch_build(json.loads(self._body()))
                self._send(200, body, "application/zip", name)
            elif self.path == "/api/patch/workbook":
                body, name = patch_workbook(json.loads(self._body()))
                self._send(200, body,
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           name)
            elif self.path == "/api/project-snapshot":
                # 用户上传「项目对象包」→ 存成第③档快照，返回 token。
                # 之后 parse/build 的 X-Project-Link 里带 snapshot=token 就会用它。
                try:
                    self._json(project_snapshot_upload(self._body()))
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
            elif self.path.split("?")[0] == "/api/link-target":
                # 没连库 / 库里查不到时，用户上传目标业务对象的 zip。
                from urllib.parse import parse_qs, urlparse
                qs = parse_qs(urlparse(self.path).query)
                try:
                    self._json(link_target_upload(
                        self._body(),
                        (qs.get("name") or [""])[0],
                        (qs.get("code") or [""])[0]))
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
            elif self.path == "/api/objects/refresh":
                # 手工刷新：连目标库重新拉一遍对象清单
                try:
                    res = objects.refresh()
                    self._json({"ok": True, **res, **objects.status(),
                                "templateSummary": {
                                    "forms": len(objects.extra()["forms"]),
                                    "lists": len(objects.extra()["lists"]),
                                    "modules": len(objects.extra()["modules"])}})
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
            elif self.path == "/api/extract":
                # PDF / Word 只抽文字，不直接生成对象；用户确认/修改后再走 /api/ai/bo
                from urllib.parse import unquote
                from . import doc_text
                name = unquote(self.headers.get("X-Filename") or "")
                try:
                    self._json({"ok": True, **doc_text.extract(name, self._body())})
                except ValueError as exc:
                    self._json({"ok": False, "error": str(exc)})
                except Exception as exc:
                    self._json({"ok": False, "error": f"文档解析失败：{exc}"})
            elif self.path.split("?")[0] == "/api/clone/inspect":
                # 克隆预检：zip → 对象清单（前端单选）。不调模型，纯本地解析。
                self._json(clone_inspect(self._body()))
            elif self.path.split("?")[0] == "/api/clone":
                # 显式克隆：zip 走二进制 body，命名与选项放 query（与 /api/ai/sheet 同口径）
                from urllib.parse import parse_qs, unquote, urlparse
                qs = parse_qs(urlparse(self.path).query)
                self._json(clone_apply(
                    self._body(),
                    (qs.get("src") or [""])[0],
                    unquote((qs.get("name") or [""])[0]),
                    (qs.get("code") or [""])[0],
                    with_form=(qs.get("form") or ["1"])[0] != "0",
                    with_list=(qs.get("list") or ["1"])[0] != "0"))
            elif self.path == "/api/ai/bo":
                # 与 /api/parse、/api/build 同一个决策头：AI 生成也要能「关联项目」。
                # 头非法 → parse_decisions 抛 ValueError → 统一 400（不静默当成没勾）。
                self._json(ai_generate_bo(json.loads(self._body()),
                                          _projlink_header(self.headers)))
            elif self.path == "/api/ai/refine":
                # 预览后不满意 → 在现有结果上按新指令改，不用从头重来。
                # 微调也必须带同一个头：不然「改一句话」之后链接字段就掉档成普通
                # object 字段（不置顶、值来源不是全字段），用户以为我们偷偷改了。
                from . import ai_refine
                try:
                    link_target, link_decisions = _ai_link_target(
                        _projlink_header(self.headers))
                    self._json(ai_refine.refine(json.loads(self._body()), _llm_conf(),
                                                link_target=link_target,
                                                link_decisions=link_decisions))
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
            elif self.path.startswith("/api/ai/sheet"):
                # 文件走二进制 body，补充说明放 query（文件名可能是中文，不用它）
                from urllib.parse import parse_qs, urlparse
                qs = parse_qs(urlparse(self.path).query)
                hint = (qs.get("hint") or [""])[0]
                sheet = (qs.get("sheet") or [""])[0]
                with_form = (qs.get("form") or ["1"])[0] != "0"
                with_list = (qs.get("list") or ["1"])[0] != "0"
                self._json(ai_from_sheet(self._body(), hint=hint, sheet=sheet,
                                         with_form=with_form, with_list=with_list,
                                         projlink_header=_projlink_header(self.headers)))
            elif self.path == "/api/ai/patch-fields":
                # 存量包修订的对话：只动「本次草稿」，包里已有字段由服务端强制拦住
                from . import ai_patch
                self._json(ai_patch.adjust(json.loads(self._body()), _llm_conf()))
            elif self.path == "/api/ai/values":
                # 预览「值来源」页签的清单（出包时包里会有哪些、谁在用、能不能关）
                self._json(ai_value_sources(json.loads(self._body()),
                                            _projlink_header(self.headers)))
            elif self.path == "/api/ai/bo/apply":
                # 这条以前只收 X-Relations：导出的 Excel 因此永远写着「关联项目=否」，
                # 用户拿它回头出包时 ensure_project_links 不补字段（2026-11-21 报的坑）。
                body, name = ai_apply_bo(json.loads(self._body()),
                                         _relations_header(self.headers),
                                         projlink_header=_projlink_header(self.headers))
                self._send(200, body,
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", name)
            elif self.path.startswith("/api/import/") and self.command == "POST":
                code = self.path[len("/api/import/"):].split("?")[0]
                # 与 build / confirmed-excel 同一个决策头：模块行上的「导入」
                # 也得按页面上的「关联项目」开关出包
                body, name = import_module(self._body(), code,
                                           _projlink_header(self.headers),
                                           _workflow_all(self.headers))
                self._send(200, body, "application/zip", name)
            elif self.path == "/api/workflow":
                body, name, ctype = workflow_files(self._body(), _workflow_all(self.headers))
                self._send(200, body, ctype, name)
            else:
                self._send(404, b"not found", "text/plain")
        except nlq_guard.SqlRejected as exc:
            # 安全闸拒绝是**预期结果**，不是程序出错：不打 traceback，
            # 统一回 400 + blocked 标记，前端按普通错误展示原因即可。
            self._json({"ok": False, "error": str(exc), "blocked": True}, 400)
        except Exception as exc:  # 统一错误出口，避免前端拿到空响应
            traceback.print_exc()
            self._json({"ok": False, "error": str(exc)}, 400)

    def _safe_parse(self, projlink_header: str | None = None) -> dict:
        """解析接口的兜底：坏文件一律 200 + {ok:false}（前端按 ok 判断）。

        决策头本身非法（不是 JSON / version≠1）在路由层就已经抛出来了，
        不会走到这里——那是**请求**的问题，与 /api/build 的 WEB25 用例同口径回 400。
        """
        try:
            data = self._body()
            return parse_excel(data, projlink_header)
        except Exception as exc:
            traceback.print_exc()
            return {"ok": False, "error": str(exc)}


def lan_addresses() -> list[str]:
    """本机局域网 IPv4，供手机/同事在同一 WiFi 下访问。

    先按主机名解析（能拿到多网卡地址），解析不到再用 UDP connect 探出口网卡——
    后者不发包，只是让内核选出默认路由对应的地址。
    """
    found: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in found:
                found.append(ip)
    except OSError:
        pass
    if not found:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("8.8.8.8", 80))
                found.append(probe.getsockname()[0])
        except OSError:
            pass
    return found


def make_server(port: int = 8770, host: str = "127.0.0.1") -> tuple[ThreadingHTTPServer, int]:
    """在 port..port+19 里找可用端口，返回 (server, 实际端口)。

    host 默认只绑回环：桌面壳与本地浏览器够用，也不给同网段留入口。
    要让手机访问须显式传 0.0.0.0（见 `pp6 ui --lan`）：该模式下 Host 管不了，
    服务实例会带一个一次性口令（`server.lan_token`），须用带口令的链接进入。
    """
    lan_token = "" if host in _LOOPBACK_HOSTS else secrets.token_urlsafe(16)
    for candidate in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer((host, candidate), Handler)
        except OSError:
            continue
        httpd.lan_token = lan_token
        # 退出接口的令牌：只有读到 ~/.pp6kit/instance.json 的本机进程拿得到。
        # 每次启动都换，旧进程的令牌对新进程无效。
        httpd.instance_token = secrets.token_urlsafe(16)
        httpd.on_quit = None
        return httpd, candidate
    raise RuntimeError(f"端口 {port}-{port + 19} 都被占用，请用 --port 指定其它端口")


def start_background(port: int = 8770, host: str = "127.0.0.1") -> tuple[ThreadingHTTPServer, str]:
    """桌面壳用：后台线程起服务，返回 (server, url)。"""
    httpd, used = make_server(port, host)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{used}/"


def serve(port: int = 8770, open_browser: bool = True, host: str = "127.0.0.1") -> None:
    httpd, used = make_server(port, host)
    url = f"http://127.0.0.1:{used}/"
    print(f"  pp6kit 已启动 -> {url}")
    if host not in ("127.0.0.1", "localhost"):
        print("  手机/同事访问（须同一 WiFi），必须用带口令的链接，转发链接等于转发权限：")
        for ip in lan_addresses():
            print(f"    http://{ip}:{used}/?token={httpd.lan_token}")
        print("  ⚠ 能打开这个链接的人都能查库（只读），用完按 Ctrl+C 关掉")
    print("  按 Ctrl+C 停止")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止")
    finally:
        httpd.server_close()
