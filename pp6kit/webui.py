"""本地 Web 界面：拖入 Excel -> 预览结构 -> 生成 zip。

只用标准库，不引入 Web 框架；文件以原始字节 POST 上来（不走 multipart），
服务端保持无状态：解析和出包各自独立，前端拿着同一个 File 对象再发一次即可。
"""
from __future__ import annotations

import base64
import copy
import io
import json
import re
import shutil
import threading
import traceback
import webbrowser
import zipfile
from datetime import datetime
from urllib.parse import quote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import ai_bo, ai_form, ai_list, ai_sheet, ai_wf, objects
from . import patch as bo_patch
from .builder import build, ensure_default_workflow, ensure_project_links, package_name
from .model import (
    FORM_BUNDLE,
    FORM_CREATE,
    EMPTY_OPERATORS,
    HIDE_OPERATORS,
    OP_BY_LABEL,
    WF_KIND_BY_LABEL,
    WF_ONGOING,
    WF_START,
    WF_STATE_BY_KIND,
    Bo,
    BoField,
    Form,
    FormField,
    HideRule,
    ListDef,
    Project,
)
from .gen_form import Snippets, _plan_groups, _plan_rows, _span_of
from .inspect import inspect_path
from . import modules as mod_def
from .reader import read_workbook
from .template import build_workbook, demo_project
from .nlq import engine as nlq_engine
from .nlq import local as nlq_local
from .nlq import guard as nlq_guard
from .nlq import hybrid as nlq_hybrid
from .nlq import introspect as nlq_introspect
from .nlq import llm as nlq_llm
from .nlq import providers as nlq_providers
from .nlq import store as nlq_store
from .resources import snippets_path, static_dir, work_dir
from .validate import validate

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
    """
    raw = (headers.get("X-Project-Link") or "").strip()
    return raw or None


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
    out = []
    for f in sorted(bo.fields, key=lambda x: x.sn):
        p = f.resolved()
        out.append({
            "code": f.code,
            "name": f.name,
            "type": f.type_new,
            "fieldName": p["fieldName"],
            "length": p["length"],
            "precision": p["precision"],
            "index": p["isindex"] == "Y",
            "objectType": f.object_type,
        })
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
        "dataset": sum(1 for d in proj.datasets if d.ds_type != mod_def.DS_FILTER),
        "filter": sum(1 for d in proj.datasets if d.ds_type == mod_def.DS_FILTER),
        "workflow": len(proj.workflows),
    }
    detail: dict[str, list[str]] = {
        "bo": [f"{b.name}（{b.code}）" for b in proj.bos],
        "form": [f"{f.name}" for f in proj.forms],
        "list": [f"{x.name}" for x in proj.lists],
        "dataset": [f"{d.name}" for d in proj.datasets if d.ds_type != mod_def.DS_FILTER],
        "filter": [f"{d.name}" for d in proj.datasets if d.ds_type == mod_def.DS_FILTER],
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
            # 表单/列表在没有业务对象时导不进去；数据集与筛选器只按表名取数，不强制
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
    if projlink_header:
        decisions = _projlink.parse_decisions(projlink_header)
        notices.extend(_projlink.apply_linked(proj, decisions))

    try:
        project_align = _projlink.analyze(proj, decisions=decisions)
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
        _projlink.apply(proj, decisions)
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
    stats = {
        "业务对象": len(proj.bos),
        "字段": sum(len(b.fields) for b in proj.bos),
        "表单": len(proj.forms),
        "表单控件": sum(len(f.fields) for f in proj.forms),
        "列表": len(proj.lists),
        "数据集": sum(1 for d in proj.datasets if d.ds_type != mod_def.DS_FILTER),
        "筛选器": sum(1 for d in proj.datasets if d.ds_type == mod_def.DS_FILTER),
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
            {"key": ds.key, "name": ds.name, "table": ds.from_table, "type": ds.ds_type}
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
              projlink_header: str | None = None) -> tuple[bytes, str]:
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
        if projlink_header is not None or any(
                getattr(b, "project_link", False) for b in proj.bos):
            align_decisions = _projlink.parse_decisions(projlink_header, proj)
            align_report = _projlink.apply(proj, align_decisions)

        issues = [Issue("error", "读取", e) for e in read_errors] + validate(proj)
        errors = [i for i in issues if i.level == "error"]
        if errors:
            raise ValueError("存在校验错误，已中止：\n" + "\n".join(str(e) for e in errors[:20]))

        # 校验按**整份 Excel** 走（用户看到的问题要是完整的），切分只影响出包。
        scope = (object_scope or "all").strip().lower()
        if scope in ("plain", "workflow"):
            from .builder import slice_by_mode
            proj = slice_by_mode(proj, scope == "workflow")
            if not proj.bos:
                if scope == "workflow":
                    raise ValueError("这份 Excel 里没有流程驱动对象：到「1_业务对象」把"
                                     "「流程驱动」改成「是」，流程对象才会被列出来")
                raise ValueError("这份 Excel 里的业务对象全是流程模式，同步包里没有可出的"
                                 "内容：流程对象请到左侧「流程模式」工作区导出")

        res = build(proj, _snippets(), work, codes=codes)
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
            raise ValueError("流程是裸 XML，不走同步包：请点「下载流程 XML」，"
                             "或到 管理 › 流程设计 › 导入 单独上传")

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
            lines.insert(4, f"项目对齐: 新增 {_s.get('linkFields', 0)} / "
                            f"对齐 {_s.get('aligned', 0)} / 跳过 {_s.get('skipped', 0)}")
        if scope == "workflow":
            # 这一包是从「流程模式」工作区导出的：只有流程模式的对象，
            # 普通对象在同步包里。两边导入入口不同，写清楚免得上错菜单。
            lines += [
                "【这是流程对象包】",
                "  只含流程模式的对象（mode=workflow）及其表单 / 列表 / 数据集。",
                "  导入：管理 › 业务对象 › 导入（和普通对象同一个入口，但建议先导普通包，",
                "        流程对象的父子关系要能找到已存在的父对象）。",
                "  流程定义是裸 XML，另点「下载流程 XML」，到 管理 › 流程设计 › 导入。",
                "",
                "  ⚠ 本包**不含**普通业务对象——那部分走左侧「普通模式」出包。",
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
            # 流程的 XML 确实**不在**这个 zip 里（_pack_all 会跳过），
            # 不写清楚用户会以为包里漏了
            lines.append("")
            lines.append("  ⚠ 流程是裸 XML，需在 管理 › 流程设计 › 导入 单独上传（本包不含流程）；"
                         "页面底部「下载流程 XML」可以直接拿到。")
        if res.notices:
            lines += ["", "提示:"] + [f"  - {n}" for n in dict.fromkeys(res.notices)]
        if align_report and not align_report.get("noop", True):
            # 独立小节：用户要能从报告里逐条看到「哪个字段对齐成了什么、哪些跳过了」
            lines += ["", "项目对齐:"] + [f"  {x}"
                                         for x in _projlink.report_lines(align_report)[1:]]
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
        if projlink_header is not None or any(
                getattr(b, "project_link", False) for b in proj.bos):
            align_decisions = _projlink.parse_decisions(projlink_header, proj)
            _projlink.apply(proj, align_decisions)
        for note in ensure_project_links(proj):
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
                  projlink_header: str | None = None) -> tuple[bytes, str]:
    """只生成某一个模块的独立 zip（页面上的「导入」按钮）。

    与 build_zip 的区别：不打包汇总，直接给系统能吃的那个单包。
    projlink_header：X-Project-Link（关联项目 / 对齐决策）。这条路径在界面上比
    「下载导入包」更显眼（每个模块行上都有一个「导入」），漏了它就是
    「勾了『关联项目』→ 点导入 → 包里还是没有 projectId」。
    """
    if code not in mod_def.BY_CODE:
        raise ValueError(f"未知模块 {code}")
    if not mod_def.of(code).zipped:
        # 流程是裸 XML。这个接口只回 zip：不拦住就会把 .xml 当 application/zip
        # 发出去（前端那个按钮虽然被禁用，接口是公开的，curl 一下就能踩到）
        raise ValueError(f"「{mod_def.label(code)}」是裸 XML，不走模块包："
                         f"请用「下载流程 XML」按钮（/api/workflow）")
    # **必须深拷贝**：_cache_proj() 是跨请求共享的同一个 Project，apply 会改字段
    # 定义、改名、重写引用——直接改缓存的话，下一次「没带头」的请求也会带着
    # projectId（用户会看到开关关不掉，且顺序不同结果不同，极难排查）。
    # 副本上对齐是安全的：_cache_proj 已经 blueprint.expand 过，build() 内部再
    # expand 一次是幂等的，ensure_project_links 也只在副本上加字段。
    proj = copy.deepcopy(_cache_proj(data))
    if projlink_header:
        from . import projlink as _projlink

        decisions = _projlink.parse_decisions(projlink_header)
        _projlink.apply(proj, decisions)
    work = _tmp_dir("import-build")
    try:
        res = build(proj, _snippets(), work, codes=[code])
        if not res.deliverables:
            raise ValueError(f"「{mod_def.label(code)}」没有可导入的内容")
        d = res.deliverables[0]
        return d.path.read_bytes(), d.filename
    finally:
        shutil.rmtree(work, ignore_errors=True)


def workflow_files(data: bytes) -> tuple[bytes, str, str]:
    """流程定义是裸 XML：一条流程回一个 .xml，多条回一个 zip（内含 {流程名}.xml）。

    返回 (body, filename, content_type)。

    为什么不复用 ``import_module``：那个接口的语义是「单模块单 zip」，
    而 9_流程 是多行表，多流程时它只会静默给出第一条（见架构文档 D3 方案 B 的否决理由）。
    """
    proj = _cache_proj(data)
    # 与 build/parse 同一套兜底：流程驱动对象即使不填 9_流程 也有默认三步，
    # 否则这里会先拦住，用户点了「下载流程 XML」却被告知「9_流程 是空的」。
    # _cache_proj 有缓存，ensure_default_workflow 幂等（按 bo_code 判重），不会越调越多。
    ensure_default_workflow(proj)
    if not proj.workflows:
        raise ValueError(
            "这份 Excel 里没有流程驱动对象：到「1_业务对象」把「流程驱动」改成「是」，"
            "就会自动生成 开始 → 部门审批 → 通过")

    # 只把**流程层面**的错误当阻断条件：用户可能只想先拿流程 XML，
    # 表单/列表那半边的问题不该拦住这个下载入口。
    errors = [i for i in validate(proj)
              if i.level == "error" and str(i.where).startswith("流程")]
    if errors:
        raise ValueError("流程定义有错误，已中止：\n"
                         + "\n".join(str(e) for e in errors[:20]))

    out = _tmp_dir("workflow-out")
    try:
        res = build(proj, _snippets(), out, codes=["workflow"])
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
        # 内置免费平台预设：前端拿它渲染下拉框，用户选完自动填地址和模型
        "providers": nlq_providers.as_list(),
        "examples": len(nlq_store.all_examples()),
    }


def nlq_save_config(payload: dict) -> dict:
    db = payload.get("db") or {}
    if db:
        current = _db_conf()
        if not db.get("password"):
            db["password"] = current.get("password", "")
        nlq_store.save_db(db)
    llm = payload.get("llm") or {}
    warnings = []
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


def ai_generate_bo(payload: dict) -> dict:
    """一句话 → 业务对象 + 表单 + 列表（三步流水线）。

    分三步而不是一次问完：每步输出小、成功率高、出错好定位。
    with_form / with_list 可以关掉某一步，只要对象。
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

    pool = objects.ensure()
    bo_res = ai_bo.generate(desc, cfg, known_objects=pool)
    bo = bo_res["bo"]
    notices.extend(bo_res["notices"])
    steps.append({"step": "对象", "model": bo_res["model"], "seconds": bo_res["seconds"]})

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
        "model": bo_res["model"],
        "seconds": round(sum(s.get("seconds", 0) for s in steps), 2),
    }


def ai_workflow(data: bytes, instruction: str, focus: str = "") -> tuple[bytes, str, dict]:
    """一句话生成/修改流程：返回新的 xlsx + 文件名 + 摘要（前端 toast 用）。"""
    from .builder import safe_filename

    body, summary = ai_wf.generate(data, instruction, focus, _llm_conf())
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    names = [w["name"] for w in summary.get("workflows") or []]
    label = names[0] if len(names) == 1 else (f"{len(names)}条流程" if names else "流程")
    return body, f"{safe_filename(label)}-流程草稿-{stamp}.xlsx", summary


def ai_from_sheet(data: bytes, hint: str = "", with_form: bool = True,
                  with_list: bool = True, sheet: str = "") -> dict:
    """非标准 Excel → 标准「对象 + 表单 + 列表」。

    和 ai_generate_bo 的区别只在「需求从哪来」：
    那边是一句话，这边是项目经理自己整理的表格。
    后半段（表单/列表设计、出 Excel）完全复用，保证两条路产物一致。
    """
    if not data:
        raise ValueError("没收到文件")
    cfg = _llm_conf()
    notices: list[str] = []
    steps: list[dict] = []

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


def ai_apply_bo(payload: dict, relations_header: str | None = None) -> tuple[bytes, str]:
    """把 AI 生成的「对象 + 表单 + 列表」落成一份 Excel。

    前端会把 /api/ai/bo 的完整结果回传（含 form/list），
    这样用户看到的预览和下载到的 Excel 是同一份东西。
    没带 form/list 时（旧调用）退回默认布局，保证还能用。
    relations_header：用户在确认区做的关系决定，写进这份 Excel 的父子三列。
    """
    data = payload.get("bo") or {}
    if not data.get("code"):
        raise ValueError("缺少对象信息")

    proj = Project()
    demo = demo_project()
    proj.tenant_id = demo.tenant_id
    proj.file_prefix = demo.file_prefix
    proj.module_id = demo.module_id
    proj.type_id = demo.type_id

    from .model import table_name_for
    bo = Bo(code=data["code"], name=data.get("name") or data["code"],
            tbl_name=table_name_for(data["code"]), desc=data.get("desc") or "")
    for i, f in enumerate(data.get("fields") or [], start=1):
        bo.fields.append(BoField(
            bo_code=bo.code,
            code=f.get("code") or f"field{i}",
            name=f.get("name") or f.get("code") or f"字段{i}",
            type_new=f.get("type") or "varchar",
            length=f.get("length"),
            object_type=f.get("objectType") or "",
            # 选项值要落到 2_字段 的「选项值」列，否则预览里改的候选值白改
            choices=list(f.get("choices") or []),
            # 单选/多选（dic）、重复值校验、索引这些开关都走 options。
            # 前端预览传的是顶层字段 isMultiple/isIndex，这里归到 options 里，
            # 否则 resolved() 拿不到、生成出来的永远是单选。
            options=_field_options(f),
            # 字段级级联（写进 attribute options）。表单控件层没单独填时，
            # 生成阶段会从这里继承，所以界面/AI 只需要说一次。
            cascade_parent=str(f.get("cascadeParent") or "").strip(),
            sn=i,
        ))
    proj.bos.append(bo)

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
            if fname in src_of and ftype in ("select", "dictionary"):
                # dic/object 控件：取值来源永远重算，陈旧值直接丢弃
                vs = src_of[fname]
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
    }


def nlq_ask(payload: dict) -> dict:
    """engine: hybrid（默认）/ local / llm / auto

    hybrid = 规则先跑，规则没把握才调 AI，AI 答对了顺便沉淀成示例。
    这是默认模式：简单问题秒出结果不花钱，复杂问题交给 AI。
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

        if mode == "llm":
            return _ask_ai(conn, pack, question, cfg, learn=True)

        if mode in ("local", "auto") and not has_key:
            return _ask_local(conn, pack, question, allowed)

        if mode == "local":
            return _ask_local(conn, pack, question, allowed)

        if mode == "auto":       # 旧行为：有 key 就直接用 AI
            try:
                return _ask_ai(conn, pack, question, cfg, learn=True)
            except Exception as exc:
                res = _ask_local(conn, pack, question, allowed)
                res["source"] = f"本地规则（大模型不可用：{str(exc)[:60]}）"
                return res

        # ---- hybrid：规则先跑 ----
        try:
            res = _ask_local(conn, pack, question, allowed)
        except nlq_local.NotUnderstood as exc:
            # 规则引擎明确说解析不了（问句里没有任何对象名/字段名/句式线索）。
            # 不能硬编一条错 SQL 去查生产库——有模型就交给 AI，没有就如实报错。
            if not has_key:
                raise
            res = {"sql": "", "columns": [], "rows": [],
                   "source": f"规则没看懂这个问题（{exc}），已改由 AI 分析",
                   "origin": "rule", "origin_label": "规则生成",
                   "question": question}
        if not has_key:
            res["source"] += "（未配置大模型，仅用规则）"
            return res

        verdict = nlq_hybrid.judge(question, res.get("sql", ""),
                                   len(res.get("rows") or []),
                                   _expected_tables(pack, question))
        if not nlq_hybrid.should_try_ai(verdict, "hybrid"):
            res["verdict"] = "规则答对了"
            return res

        try:
            ai = _ask_ai(conn, pack, question, cfg, learn=True,
                         rule_sql=res.get("sql", ""),
                         rule_rows=len(res.get("rows") or []))
        except Exception as exc:
            res["source"] += f"（AI 也失败：{str(exc)[:50]}）"
            res["ai_error"] = str(exc)[:120]
            res["origin"] = "rule"          # 最终还是规则的结果
            res["origin_label"] = "规则生成"
            return res

        # AI 0 行而规则有行 → 保留规则结果，别把能用的答案换掉
        if not ai.get("rows") and res.get("rows"):
            res["source"] += "；AI 没答出来，保留规则结果"
            res["ai_error"] = "AI 查询结果为空"
            res["origin"] = "rule"          # 呈现在眼前的是规则结果
            res["origin_label"] = "规则生成"
            # 规则本来就判不过，AI 又没答出来 → 结果可能不对，必须提示
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
            rule_sql: str = "", rule_rows: int = 0) -> dict:
    """走大模型；答对了就沉淀成示例，下次同类问题规则能直接命中。

    rule_sql / rule_rows 是规则引擎当时的结论：如果 AI 和规则**明显冲突**，
    就不沉淀——AI 也会错，错了再沉淀等于把错误固化。
    """
    # 传入选表结果，让示例挑选能按「表重合」加权（问工时就用工时的示例）
    try:
        want_tables = nlq_engine.select_tables(pack, question)
    except Exception:
        want_tables = None
    res = nlq_engine.ask(conn, pack, question, cfg,
                         nlq_store.pick_examples(question, tables=want_tables))
    res["source"] = "大模型生成" + (f"（{res['channel']}）" if res.get("channel") else "")
    res["origin"] = "ai"                      # 大模型生成
    res["origin_label"] = "AI 生成"
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


def _ask_local(conn, pack, question: str, allowed: set, allowed_extra: bool = False) -> dict:
    """本地规则引擎：不依赖大模型。

    先看有没有「AI 教过」的相似问题——有就直接用它的 SQL，
    这是沉淀机制真正生效的地方：AI 只答一次，之后都走规则。
    """
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
                        "question": question}
        except Exception:
            pass          # 改过的 SQL 也失效了（表结构变了），往下走

    # ② 高置信语义模板优先：报表/团队这类是验证过的确定性 SQL，
    # 而历史示例可能是早期答错的那一版（「超支的项目有哪些」就被带偏过）
    try:
        tpl = nlq_local.match_template(pack, question)
    except Exception:
        tpl = None
    if tpl:
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
    if similar:
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
                        "source_question": similar.get("question", ""),
                        "question": question}
        except Exception:
            pass          # 历史 SQL 失效（表结构变了），继续走规则

    sql, note = nlq_local.build(pack, question)
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
        return {"sql": safe, "columns": cols, "rows": rows}
    finally:
        conn.close()


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


def template_bytes() -> bytes:
    wb = build_workbook(demo_project())
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


class Handler(BaseHTTPRequestHandler):
    server_version = "pp6kit"

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
        self.end_headers()
        self.wfile.write(body)

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

    # --- 路由
    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
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
        elif path == "/api/objects/lookup":
            # 用户填了名字 → 查库里有没有：有就返回 id 和已有模板 key
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            name = (qs.get("name") or [""])[0]
            self._json({"ok": True, "query": name, **objects.lookup(name)})
        elif path == "/api/template":
            self._send(200, template_bytes(),
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       "PP6-填写模板.xlsx")
        elif path == "/api/patch/fields-template":
            self._send(200, bo_patch.fields_template_bytes(),
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       "PP6-新增字段表.xlsx")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        try:
            if self.path == "/api/parse":
                # 「关联项目」开关走同一个决策头。/校验放在路由层：头非法是**请求**
                # 的问题（与 /api/build 4xx 一个口径），不能被 _safe_parse 的
                # 「坏文件 → 200 + ok:false」兜底吞掉，否则前端只看到一句话。
                pl_header = _projlink_header(self.headers)
                if pl_header:
                    from .projlink import parse_decisions as _pl_decisions
                    _pl_decisions(pl_header)
                self._json(self._safe_parse(pl_header))
            elif self.path == "/api/build":
                body, name = build_zip(self._body(), _codes(self.headers),
                                       _relations_header(self.headers),
                                       _object_scope(self.headers),
                                       _projlink_header(self.headers))
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
            elif self.path == "/api/nlq/run":
                self._json({"ok": True, **nlq_run(json.loads(self._body()))})
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
            elif self.path == "/api/ai/bo":
                self._json(ai_generate_bo(json.loads(self._body())))
            elif self.path == "/api/ai/refine":
                # 预览后不满意 → 在现有结果上按新指令改，不用从头重来
                from . import ai_refine
                try:
                    self._json(ai_refine.refine(json.loads(self._body()), _llm_conf()))
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
                                         with_form=with_form, with_list=with_list))
            elif self.path.startswith("/api/ai/workflow"):
                # 和 /api/ai/sheet 同一套：xlsx 走二进制 body，说明和当前流程名放 query
                from urllib.parse import parse_qs, urlparse
                qs = parse_qs(urlparse(self.path).query)
                instruction = (qs.get("instruction") or [""])[0]
                focus = (qs.get("focus") or [""])[0]
                body, name, summary = ai_workflow(self._body(), instruction, focus)
                # HTTP 头只能放 latin-1：摘要先转 JSON 再 URL 编码，前端 decodeURIComponent
                self._send(
                    200, body,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    name,
                    extra={"X-AI-Summary": quote(json.dumps(summary, ensure_ascii=False))},
                )
            elif self.path == "/api/ai/bo/apply":
                body, name = ai_apply_bo(json.loads(self._body()),
                                         _relations_header(self.headers))
                self._send(200, body,
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", name)
            elif self.path.startswith("/api/import/") and self.command == "POST":
                code = self.path[len("/api/import/"):].split("?")[0]
                # 与 build / confirmed-excel 同一个决策头：模块行上的「导入」
                # 也得按页面上的「关联项目」开关出包
                body, name = import_module(self._body(), code,
                                           _projlink_header(self.headers))
                self._send(200, body, "application/zip", name)
            elif self.path == "/api/workflow":
                # 流程是裸 XML：单条回 application/xml，多条回 zip。
                # ctype 由 workflow_files 决定（它比路由更清楚这次是哪种）
                body, name, ctype = workflow_files(self._body())
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


def make_server(port: int = 8770) -> tuple[ThreadingHTTPServer, int]:
    """在 port..port+19 里找可用端口，返回 (server, 实际端口)。"""
    for candidate in range(port, port + 20):
        try:
            return ThreadingHTTPServer(("127.0.0.1", candidate), Handler), candidate
        except OSError:
            continue
    raise RuntimeError(f"端口 {port}-{port + 19} 都被占用，请用 --port 指定其它端口")


def start_background(port: int = 8770) -> tuple[ThreadingHTTPServer, str]:
    """桌面壳用：后台线程起服务，返回 (server, url)。"""
    httpd, used = make_server(port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{used}/"


def serve(port: int = 8770, open_browser: bool = True) -> None:
    httpd, used = make_server(port)
    url = f"http://127.0.0.1:{used}/"
    print(f"  pp6kit 已启动 -> {url}")
    print("  按 Ctrl+C 停止")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止")
    finally:
        httpd.server_close()
