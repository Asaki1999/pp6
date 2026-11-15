"""业务对象「关联项目」：链接字段、字段对齐、改名与引用重写、存量包对齐。

这组用例守的是四件事（每条都能让线上出问题）：
  1. **未勾选零变化**：同一份 Excel 改动前后两次出包，bo/form/data_template 逐字节相同；
  2. **零静默改写**：相似字段一条都不自动对齐，勾了才动；
  3. **引用完整性**：对齐改名后 workbook 里搜旧编码 = 0 命中（表单脚本除外，只提示）；
  4. **存量包铁律**：已有字段的 code / fieldName / sn 一个都不许变。

不连库、不读网：快照用内存里手写的最小 Snapshot（`fake_snapshot()`），
必要时临时替换 `projlink.load_snapshot`。涉及真实 `assets/project_fields.json` 的
用例在文件缺失时 `skip()`。
"""
from __future__ import annotations

import contextlib
import io
import re
import zipfile

from pp6kit import patch, projlink
from pp6kit import spec as S
from pp6kit import webui
from pp6kit.model import Bo, BoField
from pp6kit.reader import read_workbook
from tests._harness import (all_tags, attrs, case, field, pick, read_xlsx_rows,
                            skip, texts, unzip, xlsx)

# ---------------------------------------------------------------- 固定样本


def _pf(code: str, name: str, type_new: str = "varchar", length=None,
        object_type: str = "", is_index: bool = False, sn: int = 1,
        options: dict | None = None, meta: projlink.DictMeta | None = None,
        precision=None) -> projlink.ProjectField:
    return projlink.ProjectField(
        code=code, name=name, type_new=type_new, datatype="",
        field_name=projlink.camel_to_snake(code) + "_", object_type=object_type,
        length=length, precision=precision, is_null=True, is_index=is_index,
        sn=sn, options=options or {}, dict=meta,
    )


def _report_type() -> projlink.DictMeta:
    items = tuple(
        projlink.DictItem(key=k, name=n, sn=s)
        for k, n, s in (
            ("MonthlyReportModules", "月报", 146),
            ("QuarterlyReportModules", "季报", 178),
            ("PhaseReportModules", "项目阶段报告", 198),
            ("WeeklyReportModules", "周报", 239),
            ("HalfMonthlyReportModules", "半月报", 321),
            ("BiweeklyReportModules", "双周报", 399),
        )
    )
    return projlink.DictMeta(ref="report_type", name="项目汇报类型", stru_type="1",
                             scope="GLOBAL", type_sn=1, items=items)


def fake_snapshot() -> projlink.Snapshot:
    """最小项目字段快照（字段与线上 project 对象同名同族）。"""
    fields = [
        _pf("name", "项目名称", "varchar", 200, sn=1),
        _pf("shortName", "项目简称", "varchar", 255, sn=2),
        _pf("classificationId", "项目类型", "object", 64, "classification", sn=3),
        _pf("stateId", "项目状态", "object", 64, "project_state", is_index=True, sn=6),
        _pf("gradeId", "项目级别", "object", 64, "project_grade", sn=7),
        _pf("code", "项目编号", "varchar", 64, is_index=True, sn=10),
        _pf("phase", "项目阶段名称", "varchar", 1000, sn=11),
        _pf("phaseIds", "项目阶段", "object", 1000, "project_phase", sn=12),
        _pf("start", "计划开始时间", "datetime", None, sn=15),
        _pf("managerIds", "项目经理", "user", 1000, sn=29),
        _pf("notes", "备注", "clob", None, sn=30),
        _pf("totalBudget", "总预算", "number", 22, sn=31, precision=4),
        _pf("huiBaoLeiXing", "汇报类型", "dic", 32, "report_type", sn=37,
            options={"isMultiple": False, "isDefault": False}, meta=_report_type()),
    ]
    return projlink.Snapshot(
        source="target_db", generated_at="2026-11-14T09:12:00+08:00",
        project={"code": "project", "name": "项目", "tbl": "pp_project",
                 "attributeCount": len(fields), "businessFieldCount": len(fields)},
        fields=fields, system_codes=set(projlink.SYSTEM_CODES),
        warnings=[], declared_total=len(fields),
    )


@contextlib.contextmanager
def using(snap: projlink.Snapshot | None = None):
    """临时把 projlink.load_snapshot 换成手写快照（builder 也会走这条）。"""
    snap = snap if snap is not None else fake_snapshot()
    old = projlink.load_snapshot

    def _fake(*_a, **_k):
        return snap

    projlink.load_snapshot = _fake
    try:
        yield snap
    finally:
        projlink.load_snapshot = old


def bo_row(name: str = "合同", code: str = "contract", **kw) -> dict:
    row = {"对象名称": name, "对象编码": code, "物理表名": f"pp_{code}",
           "对象类型": "object", "记录变更日志": "是", "流程驱动": "否"}
    row.update(kw)
    return row


def sheets(fields: list[dict], bos: list[dict] | None = None, **extra) -> dict:
    data = {S.S_BO: bos or [bo_row()], S.S_FIELD: fields}
    data.update(extra)
    return data


def build(data: dict) -> bytes:
    return webui.build_zip(xlsx(data))[0]


def project_of(data: dict):
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as fh:
        fh.write(xlsx(data))
        path = Path(fh.name)
    try:
        proj, errors, _stats = read_workbook(path)
    finally:
        path.unlink(missing_ok=True)
    return proj, errors


def comment_of(body: bytes) -> str:
    return zipfile.ZipFile(io.BytesIO(body)).comment.decode("utf-8", "replace")


def attr_row(text: str, code: str) -> dict:
    for frag in all_tags(text, "attribute"):
        got = attrs(frag)
        if got.get("code") == code:
            return got
    raise AssertionError(f"没有 code={code} 的 <attribute>（实际："
                         f"{[attrs(f).get('code') for f in all_tags(text, 'attribute')]}）")


def attr_line(text: str, code: str) -> str:
    for frag in all_tags(text, "attribute"):
        if attrs(frag).get("code") == code:
            return frag
    raise AssertionError(f"没有 code={code} 的 <attribute>")


# ---------------------------------------------------------------- PL01–PL05


@case("PL01", "projlink", "项目编号精确同名 → 自动对齐成 code（编码/列名/长度/索引同步）",
      "P0", ["E0 自动对齐", "物理列 code_", "isindex Y"])
def _():
    data = sheets([field("contract", "项目编号", "字符串")],
                  bos=[bo_row(关联项目="是")])
    with using():
        body = build(data)
    row = attr_row(pick(body, "bo/"), "code")
    assert row["fieldName"] == "code_", row
    assert row["length"] == "64", row
    assert row["isindex"] == "Y", row
    assert row["name"] == "项目编号", row
    assert "✓ 合同 项目编号 → code" in comment_of(body)
    # 链接字段同时被补出来（勾了关联项目就该有「所属项目」）
    link = attr_row(pick(body, "bo/"), "projectId")
    assert link["objectType"] == "project", link


@case("PL02", "projlink", "相近字段默认不覆盖：弹窗里勾了「按项目覆盖」（align=true）才照项目定义改",
      "P0", ["S1 默认 pending", "align:true 才覆盖", "没勾零变化"])
def _():
    # 名称只写「状态」：和项目侧「项目状态」是 S1（包含关系），**不是**精确同名。
    # 2026-11-15 用户口径：只有「直接对应」（E0 同族唯一）自动覆盖；S1-S4 一律
    # 进 pending，弹窗逐条问，默认保留我方。所以这里守三件事：
    #   ① 没勾「关联项目」→ 一个字节都不改；
    #   ② 勾了但没确认这条相近字段 → 我方定义原样保留；
    #   ③ 弹窗里勾了「按项目覆盖」（align=true）→ 真的覆盖。
    base = field("contract", "状态", "选项", **{"字段编码": "projectState",
                                               "选项值": "进行中,已结束"})
    plain = build(sheets([dict(base)], bos=[bo_row()]))
    with using():
        kept = build(sheets([dict(base)], bos=[bo_row(关联项目="是")]))
        confirmed = webui.build_zip(
            xlsx(sheets([dict(base)], bos=[bo_row(关联项目="是")])),
            projlink_header=pl_header(
                {"contract": {"align": {"projectState": True}}}))[0]
    # ① 没勾「关联项目」：老行为一个字节都不改（这个不能退化）
    assert 'code="projectState"' in pick(plain, "bo/")
    assert 'code="stateId"' not in pick(plain, "bo/")
    # ② 勾了「关联项目」但相近字段没确认 → 保留我方（编码/类型/候选值都不动）
    mine = attr_row(pick(kept, "bo/"), "projectState")
    assert mine["dataTypeNew"] == "dic", mine
    assert "stateId" not in {attrs(a).get("code")
                              for a in all_tags(pick(kept, "bo/"), "attribute")}
    # 链接字段照补（说明差异只来自字段对齐，不是「什么都没做」）
    assert 'code="projectId"' in pick(kept, "bo/")
    # ③ 勾了「按项目覆盖」→ 编码/物理列/类型/关联对象全部照项目侧
    row = attr_row(pick(confirmed, "bo/"), "stateId")
    assert row["fieldName"] == "state_id_", row
    assert row["dataTypeNew"] == "object" and row["objectType"] == "project_state", row
    assert row["length"] == "64", row


@case("PL03", "projlink", "勾选后按决策头对齐：编码变 stateId，旧编码全文 0 命中",
      "P0", ["X-Project-Link", "引用重写"])
def _():
    data = sheets([field("contract", "项目状态", "选项",
                         **{"字段编码": "projectState", "选项值": "进行中,已结束"})],
                  bos=[bo_row(关联项目="是")])
    proj, errors = project_of(data)
    assert not errors, errors
    decisions = {"version": 1, "objects": {"contract": {"align": {"projectState": True}}}}
    report = projlink.apply(proj, decisions, snap=fake_snapshot())
    bo = proj.bo("contract")
    f = bo.field_by_code("stateId")
    assert f is not None, [x.code for x in bo.fields]
    assert f.resolved_field_name() == "state_id_"
    assert f.type_new == "object" and f.object_type == "project_state"
    assert report["aligned"], report
    assert not any(x.code == "projectState" for x in bo.fields)


@case("PL04", "projlink", "相似判定：同义词归一到 S4，包含/编辑距离各归其位",
      "P0", ["S1/S2/S3/S4", "纯函数"])
def _():
    sim = projlink.similar
    # 四组同义词（类别→类型 / 等级→级别 / 编码→编号 / 状况→状态）全部 S4
    assert sim("项目类别", "项目类型") == "S4"
    assert sim("项目等级", "项目级别") == "S4"
    assert sim("项目编码", "项目编号") == "S4"
    assert sim("项目状况", "项目状态") == "S4"
    assert sim("项目状态", "状态") == "S1"
    # S2 只认编辑距离 ≤1（放宽度到 ≤2 会把「合同名称/项目名称」这类全判成相似，
    # 几乎每个对象都会弹窗）。差 2 次编辑的组合交给 S1 兜：仍是相似、仍不自动对齐
    assert sim("汇报类型", "项目汇报类型") in ("S1", "S2")
    assert sim("项目阶段名称", "项目阶段") in ("S1", "S2")
    # 反向保护：换一个词头的「XX名称/XX编号」不是相似字段，不该弹窗
    for a, b in (("合同名称", "项目名称"), ("合同编号", "项目编号"),
                 ("合同类型", "项目类型"), ("合同日期", "项目日期")):
        assert sim(a, b) is None, f"{a}/{b} 不该判成相似"
    assert sim("合同金额", "项目金额") is None
    assert sim("备注", "项目备注说明") is None
    # 「只差一个限定字」不做包含匹配（多半是两个不同字段）
    assert sim("金额", "总金额") is None
    # 结构同源：编码或物理列相同，但名称不同 → S3
    own = BoField(bo_code="contract", code="stateId", name="项目当前状态")
    assert sim(own, _pf("stateId", "项目状态", "object", 64, "project_state")) == "S3"
    # 归一：空白/括号内容/尾部「字段」都不算差异
    assert projlink.normalize("项目 状态") == projlink.normalize("项目状态")
    assert projlink.normalize("项目状态（旧）") == "项目状态"
    assert projlink.normalize("项目状态字段") == "项目状态"
    # 同族判定：varchar 族的成员互换不 ALTER
    assert projlink.family("dic") == projlink.family("object") == "varchar"
    assert projlink.family("number") == "number"


@case("PL05", "projlink", "可疑名单只留 3 项：项目名称进「需确认」，其余同名维度字段照样自动覆盖",
      "P0", ["B10 名单", "可疑 → 需确认", "suspect 只作提示"])
def _():
    data = sheets([
        field("contract", "项目名称", "字符串", **{"字段编码": "projectName"}),
        field("contract", "项目类型", "对象", **{"关联对象/字典": "project_type"}),
        field("contract", "项目级别", "对象", **{"关联对象/字典": "project_grade"}),
        field("contract", "项目状态", "对象", **{"关联对象/字典": "project_state"}),
        field("contract", "项目阶段", "对象", **{"关联对象/字典": "project_phase"}),
        field("contract", "项目编号", "字符串", **{"字段编码": "projectCode"}),
    ], bos=[bo_row(关联项目="是")])
    proj, errors = project_of(data)
    assert not errors, errors
    vm = projlink.analyze(proj, fake_snapshot())
    item = vm["items"][0]
    aligned = {a["fieldName"]: a for a in item["exact"] + item["similar"]}
    # 五个同名维度字段是「直接对应」（E0 同族唯一）→ 自动覆盖
    for name in ("项目类型", "项目级别", "项目状态", "项目阶段", "项目编号"):
        assert name in aligned, f"{name} 没有对齐：{list(aligned)}"
        assert aligned[name]["auto"] is True, aligned[name]
    # 「项目名称」在可疑名单：**不自动覆盖**（2026-11-15 口径），进 pending 等确认
    assert "项目名称" not in aligned, list(aligned)
    pending = {a["fieldName"]: a for a in item["pending"]}
    assert pending["项目名称"]["suspect"] is True, pending["项目名称"]
    assert pending["项目名称"]["auto"] is False, pending["项目名称"]
    assert "可疑" in pending["项目名称"]["note"], pending["项目名称"]["note"]
    # 关系表里两者的状态也要能区分
    rel = {r["fieldName"]: r for r in item["relations"]}
    assert rel["项目名称"]["status"] == "pending", rel["项目名称"]
    assert rel["项目类型"]["status"] == "auto", rel["项目类型"]
    assert set(projlink.SEMANTIC_SUSPECT) == {"项目名称", "项目简称", "项目经理"}


# ---------------------------------------------------------------- PL06–PL11


@case("PL06", "projlink", "编码被别的字段占着 → 跳过并报告，出包不产生重复编码",
      "P0", ["code_taken", "不中止出包"])
def _():
    data = sheets([
        field("contract", "项目当前状态", "字符串", **{"字段编码": "stateId"}),
        field("contract", "项目状态", "对象", **{"关联对象/字典": "project_state",
                                                 "字段编码": "projectState"}),
    ], bos=[bo_row(关联项目="是")])
    proj, errors = project_of(data)
    assert not errors, errors
    report = projlink.apply(proj, None, snap=fake_snapshot())
    kinds = [c["kind"] for c in report["conflicts"]]
    assert "code_taken" in kinds, report["conflicts"]
    bo = proj.bo("contract")
    codes = [f.code for f in bo.fields]
    assert len(codes) == len(set(codes)), codes
    assert bo.field_by_code("projectState").resolved_field_name() != "state_id_"


@case("PL07", "projlink", "两个字段命中同一项目字段 → 只对齐一个，另一个报 target_taken",
      "P0", ["同一目标只认一个"])
def _():
    data = sheets([
        field("contract", "项目状态", "对象", **{"字段编码": "projectStateA",
                                                 "关联对象/字典": "project_state"}),
        field("contract", "项目 状态", "对象", **{"字段编码": "projectStateB",
                                                  "关联对象/字典": "project_state"}),
    ], bos=[bo_row(关联项目="是")])
    proj, errors = project_of(data)
    assert not errors, errors
    report = projlink.apply(proj, None, snap=fake_snapshot())
    assert [c["kind"] for c in report["conflicts"]] == ["target_taken"], report["conflicts"]
    bo = proj.bo("contract")
    assert bo.field_by_code("stateId") is not None
    assert bo.field_by_code("projectStateB") is not None      # 第二个保持原样


@case("PL08", "projlink", "已有对象ID的对象：只覆盖定义，编码与物理列名不变",
      "P0", ["update_mode", "存量对象不改名"])
def _():
    data = sheets([field("contract", "项目编号", "字符串", **{"字段编码": "code"})],
                  bos=[bo_row(关联项目="是", 已有对象ID="abc123")])
    proj, errors = project_of(data)
    assert not errors, errors
    proj.bo("contract").exist_id = "abc123"
    report = projlink.apply(proj, None, snap=fake_snapshot())
    bo = proj.bo("contract")
    f = bo.field_by_code("code")
    assert f is not None and f.resolved_field_name() == "code_", f
    assert f.length == 64 and f.is_index is True
    assert any("保留编码与物理列" in (x.get("note") or "") for x in report["aligned"])
    # D1 修好后的文案：列名必须是**物理列**（code_），不能是显示名「项目编号」
    lines = "\n".join(projlink.report_lines(report))
    assert "保留物理列 code_（项目侧 code_" in lines, lines
    assert "保留物理列 项目编号" not in lines, lines


@case("PL09", "projlink", "链接字段与系统范式逐键一致：6 键 options、无 format、sn 最大",
      "P0", ["PRD A1/A2", "project_id_ varchar 64"])
def _():
    data = sheets([field("contract", "合同编号", "字符串")],
                  bos=[bo_row(关联项目="是")],
                  **{S.S_FORM: [{"表单key": "create_contract", "表单名称": "新建合同",
                                 "对象编码": "contract", "表单类型": "新建表单"}],
                     S.S_FORMFIELD: [{"表单key": "create_contract", "字段编码": "heTongBianHao",
                                      "显示标签": "合同编号", "控件类型": "text"}]})
    with using():
        body = build(data)
    bo_text = pick(body, "bo/")
    row = attr_row(bo_text, "projectId")
    assert row["fieldName"] == "project_id_", row
    assert row["name"] == "所属项目", row
    assert row["dataTypeNew"] == "object" and row["objectType"] == "project", row
    assert row["datatype"] == "varchar" and row["length"] == "64", row
    assert row["isnull"] == "Y" and row["isindex"] == "N", row
    assert "format" not in row, row
    import json

    opts = json.loads(row["options"].replace("&quot;", '"'))
    assert set(opts) == {"isMultiple", "isDefault", "uniqueCheck",
                         "optionSource", "columns", "identities"}, opts
    assert opts["optionSource"] == "BO_ATTR_DIC_TYPE"
    # sn 必须大于所有系统字段
    sns = {attrs(f).get("code"): int(attrs(f)["sn"]) for f in all_tags(bo_text, "attribute")
           if attrs(f).get("sn", "").isdigit()}
    assert sns["projectId"] == max(sns.values()), sns
    # 表单里也补了「所属项目」下拉
    form = pick(body, "form/")
    assert "projectId" in form


@case("PL10", "projlink", "已有指向项目的字段 → 复用不新增",
      "P0", ["PRD A7", "重复链接字段"])
def _():
    data = sheets([field("contract", "关联项目", "对象",
                         **{"字段编码": "projId", "关联对象/字典": "project"})],
                  bos=[bo_row(关联项目="是")])
    with using():
        body = build(data)
    bo_text = pick(body, "bo/")
    assert 'code="projectId"' not in bo_text
    assert 'code="projId"' in bo_text
    assert "复用已有链接字段" in comment_of(body)


@case("PL11", "projlink", "汇报类型对齐到全局字典：GLOBAL + 固定候选码，不造新 key",
      "P0", ["PRD A4/A12", "候选值丢失提示"])
def _():
    data = sheets([field("contract", "汇报类型", "选项",
                         **{"字段编码": "huiBaoLeiXing", "选项值": "月报,季报,自定义项"})],
                  bos=[bo_row(关联项目="是")])
    with using():
        body = build(data)
    bo_text = pick(body, "bo/")
    row = attr_row(bo_text, "huiBaoLeiXing")
    assert row["dataTypeNew"] == "dic" and row["objectType"] == "report_type", row
    assert row["length"] == "32", row
    assert "optionSource" not in row.get("options", ""), row
    assert 'typeScope="GLOBAL"' in bo_text
    assert 'typeKey="report_type"' in bo_text
    for key in ("MonthlyReportModules", "QuarterlyReportModules", "PhaseReportModules",
                "WeeklyReportModules", "HalfMonthlyReportModules", "BiweeklyReportModules"):
        assert f'key="{key}"' in bo_text, key
    # 绝不能出现「对象_字段」形态的自定义字典 key
    assert "contract_huiBaoLeiXing" not in bo_text
    # O2：对齐后编码与列名都没变的字段，确认版 Excel 的「物理列名」也要有值
    with using():
        xls, _xname = webui.confirmed_excel(xlsx(data), None, "x.xlsx")
    rows = read_xlsx_rows(xls, S.S_FIELD)
    hit = next(r for r in rows if r.get("字段编码") == "huiBaoLeiXing")
    assert hit["物理列名"] == "hui_bao_lei_xing_", hit
    assert "自定义候选值将丢失（1 项" in comment_of(body)


# ---------------------------------------------------------------- PL12–PL14


@case("PL12", "projlink", "未勾「关联项目」：两次出包的 XML 逐字节相同",
      "P0", ["特性零副作用", "PRD A15"])
def _():
    data = sheets([field("contract", "项目编号", "字符串"),
                   field("contract", "项目状态", "选项", **{"选项值": "进行中,已结束"})])
    first = build(data)
    second = build(data)
    # 对象/表单的 id 是每次生成的随机 UUID（导入端按 id 判断新增还是更新），
    # 比对前统一抹掉，剩下的字节必须完全一样
    id_pat = re.compile(r'\sid="[^"]*"')

    def norm(text: str) -> str:
        return id_pat.sub(' id=""', text)

    a, b = texts(first), texts(second)
    assert a and sorted(a) == sorted(b)
    for name in a:
        assert norm(a[name]) == norm(b[name]), f"{name} 变了"


@case("PL13", "projlink", "快照三档全无：解析/出包照常，只多一条提示，仍补链接字段",
      "P1", ["E1 降级", "不阻断出包"])
def _():
    empty = projlink.Snapshot(source="unavailable", fields=[],
                              warnings=["项目字段快照不可用"])
    data = sheets([field("contract", "项目编号", "字符串")], bos=[bo_row(关联项目="是")])
    with using(empty):
        body = build(data)
        bo_text = pick(body, "bo/")
        assert 'code="projectId"' in bo_text
        assert "项目字段快照不可用" in comment_of(body)
    proj, errors = project_of(data)
    assert not errors, errors
    vm = projlink.analyze(proj, empty)
    assert vm["status"] == "unavailable"
    assert vm["notice"] and "只补" in vm["notice"]
    assert not vm["items"][0]["exact"]


@case("PL14", "projlink", "对齐改名后：表单/列表/隐藏规则/流程条件/级联/父子字段全部改掉",
      "P0", ["PRD A6/S4", "scriptFormKeys"])
def _():
    data = {
        S.S_BO: [bo_row(关联项目="是", 父对象编码="project", 关联字段="project_state_")],
        S.S_FIELD: [
            field("contract", "项目状态", "选项", **{"字段编码": "projectState",
                                                     "选项值": "进行中,已结束"}),
            field("contract", "合同编号", "字符串", **{"字段编码": "contractNo",
                                                       "级联父字段": "projectState"}),
        ],
        S.S_FORM: [{"表单key": "create_contract", "表单名称": "新建合同",
                    "对象编码": "contract", "表单类型": "新建表单"}],
        S.S_FORMFIELD: [
            {"表单key": "create_contract", "字段编码": "projectState",
             "显示标签": "项目状态", "控件类型": "dictionary",
             "取值来源": "contract_projectState"},
            {"表单key": "create_contract", "字段编码": "contractNo",
             "显示标签": "合同编号", "控件类型": "text", "级联父字段": "projectState"},
        ],
        S.S_LIST: [{"列表key": "contract_list", "列表名称": "合同列表",
                    "对象编码": "contract", "视图类型": "grid",
                    "显示字段": "projectState,contractNo", "排序字段": "projectState",
                    "标题字段": "contractNo"}],
        S.S_HIDE: [{"表单key": "create_contract", "隐藏控件": "contractNo",
                    "依赖控件": "projectState", "判断条件": "等于", "条件值": "进行中"}],
        S.S_SCRIPT: [{"表单key": "create_contract", "脚本说明": "内联脚本",
                      "JForm 脚本": "Object.assign(JForm, { onLoad: function(form){"
                                    " form.setData('projectState', 1); } });"}],
        S.S_WF: [{"流程名称": "合同审批", "对象编码": "contract"}],
        S.S_WF_NODE: [
            {"流程名称": "合同审批", "节点序号": 1, "节点名称": "开始", "节点类型": "开始"},
            {"流程名称": "合同审批", "节点序号": 2, "节点名称": "判断_状态", "节点类型": "判断"},
            {"流程名称": "合同审批", "节点序号": 3, "节点名称": "通过", "节点类型": "通过"},
            {"流程名称": "合同审批", "节点序号": 4, "节点名称": "驳回", "节点类型": "驳回"},
        ],
        S.S_WF_EDGE: [
            {"流程名称": "合同审批", "起点节点": "开始", "终点节点": "判断_状态",
             "流转条件": "无条件"},
            {"流程名称": "合同审批", "起点节点": "判断_状态", "终点节点": "通过",
             "流转条件": "字段条件", "条件字段": "projectState",
             "条件运算": "等于", "条件值": "进行中", "顺序": 1},
            # 判断节点必须 ≥2 条出边，否则 validate 直接拦住（和本用例无关）
            {"流程名称": "合同审批", "起点节点": "判断_状态", "终点节点": "驳回",
             "流转条件": "无条件", "顺序": 2},
        ],
    }
    decisions = {"version": 1, "objects": {"contract": {"align": {"projectState": True}}}}
    body, _name = webui.build_zip(xlsx(data), None, None, "all")
    assert body    # 不带决策头时相似项不动，先确认能出包
    with using():
        body, _name = webui.build_zip(
            xlsx(data), None, None, "all",
            projlink_header=__import__("json").dumps(decisions))
    contents = {n: t for n, t in texts(body).items()
                if n.startswith(("bo/", "form/", "data_template/"))}
    # 表单脚本（extendAttr.script）改不了——JS 里的字段编码无法静态解析，
    # 产品口径是「只检测、列清单给人工核对」，所以比对前把它摘掉
    script_pat = re.compile(r"&quot;script&quot;:&quot;.*?&quot;", re.S)
    joined = script_pat.sub("", "\n".join(contents.values()))
    assert "projectState" not in joined, "旧编码仍有残留"
    assert "stateId" in joined
    import json as _json

    def unescape(text: str) -> str:
        return text.replace("&quot;", '"')

    bo_text = pick(body, "bo/")
    # 父子关系的 relField 用物理列名 → 跟着改名
    assert 'relField="state_id_"' in bo_text, re.findall(r'relField="[^"]*"', bo_text)
    # 字段级联落在 attribute 的 options JSON 里（属性值整体被转义过）
    cascade = _json.loads(unescape(attr_row(bo_text, "contractNo")["options"]))
    assert cascade["cascadeParentCode"] == "stateId", cascade
    form = pick(body, "form/")
    assert '"cascadeParentCode":"stateId"' in unescape(form), "表单控件级联父字段没改"
    assert "需人工核对" in comment_of(body)
    assert "create_contract" in comment_of(body)


# ---------------------------------------------------------------- PL15–PL18


def excel_package() -> bytes:
    """造一份「存量包」：合同对象已有 项目编号（物理列 contract_no_）。"""
    data = sheets([
        field("contract", "项目编号", "字符串",
              **{"字段编码": "code", "物理列名": "contract_no_", "长度": 50, "建索引": "否"}),
        field("contract", "合同名称", "字符串", **{"字段编码": "title"}),
    ], **{S.S_FORM: [{"表单key": "create_contract", "表单名称": "新建合同",
                      "对象编码": "contract", "表单类型": "新建表单"}],
          S.S_FORMFIELD: [{"表单key": "create_contract", "字段编码": "code",
                           "显示标签": "项目编号", "控件类型": "text"}]})
    return build(data)


def patch_spec(body: bytes, **kw) -> dict:
    obj = patch.inspect_package(body)["objects"][0]
    spec = {"targets": [obj["file"]], "scope": "full", "mode": "replace",
            "linkProject": [obj["file"]], "fields": []}
    spec.update(kw)
    return spec


@case("PL15", "projlink", "存量包对齐：只覆盖类型/长度/索引，编码与物理列名 0 变化",
      "P0", ["PRD A10", "check_patch 61 条不破"])
def _():
    raw = excel_package()
    obj = patch.inspect_package(raw)["objects"][0]
    before = attr_row(pick(raw, "bo/"), "code")
    with using():
        new_body, _name, payload = patch.build_patch(raw, patch_spec(raw))
    after = attr_row(pick(new_body, "bo/"), "code")
    assert after["fieldName"] == "contract_no_", after
    assert after["code"] == "code"
    assert after["sn"] == before["sn"]
    assert after["length"] == "64" and after["isindex"] == "Y", after
    assert "项目对齐" in payload["report"]
    assert "存量包不改列名" in payload["report"]
    # 其余条目逐字节相同
    old_entries, new_entries = unzip(raw), unzip(new_body)
    for name, data in old_entries.items():
        if name == obj["file"]:
            continue
        assert new_entries[name] == data, f"{name} 被动了"


@case("PL16", "projlink", "存量包新增的链接字段与 Excel 路径产物完全一致",
      "P0", ["PRD A1 逐键", "options 6 键、无 format"])
def _():
    raw = excel_package()
    with using():
        patched, _name, _payload = patch.build_patch(raw, patch_spec(raw))
        excel = build(sheets([field("contract", "项目编号", "字符串",
                                    **{"字段编码": "code"})],
                             bos=[bo_row("合同", "contract", 关联项目="是")]))
    from_excel = attr_row(pick(excel, "bo/"), "projectId")
    from_patch = attr_row(pick(patched, "bo/"), "projectId")
    for key in ("code", "fieldName", "name", "dataTypeNew", "datatype", "objectType",
                "length", "isnull", "isindex"):
        assert from_patch.get(key) == from_excel.get(key), (key, from_patch, from_excel)
    assert "format" not in from_patch
    assert from_patch["options"] == from_excel["options"]
    import json
    opts = json.loads(from_patch["options"].replace("&quot;", '"'))
    assert len(opts) == 6, opts


@case("PL17", "projlink", "X-Project-Link 非法 JSON / version≠1 → 400，文案可读",
      "P1", ["不信任前端", "错误可读"])
def _():
    from tests.test_webui import package, req

    data = package()
    for bad, hint in ((b'{"version":1,"objects":', "不是合法 JSON"),
                      (b'{"version":2,"objects":{}}', "version")):
        status, _headers, body = req("/api/build", data,
                                     {"X-Project-Link": bad.decode("utf-8")})
        assert status == 400, status
        text = body.decode("utf-8", "replace")
        assert hint in text, text
        assert "X-Project-Link" in text, text


@case("PL18", "projlink", "存量包 mode=skip 且存在对齐行 → 出包与预览都报错",
      "P1", ["对齐行需要覆盖模式", "预览与产物一致"])
def _():
    raw = excel_package()
    spec = patch_spec(raw, mode="skip")
    with using():
        for call in (lambda: patch.build_patch(raw, spec),
                     lambda: patch.dry_run(raw, spec)):
            try:
                call()
            except ValueError as exc:
                assert "对齐已有字段需要覆盖模式" in str(exc), str(exc)
            else:
                raise AssertionError("mode=skip + 对齐行应当报错")


@case("PL19", "projlink", "存量包新增的全局字典字段写成 GLOBAL，不造 对象_字段 假字典",
      "P0", ["typeScope=GLOBAL", "固定候选码", "10 键范式仍成立"])
def _():
    raw = excel_package()          # 原包里没有「汇报类型」字段
    obj = patch.inspect_package(raw)["objects"][0]
    spec = {
        "targets": [obj["file"]], "scope": "full", "mode": "replace",
        "linkProject": [obj["file"]],
        "align": {obj["file"]: {"reportType": True}},
        "fields": [{"code": "reportType", "name": "汇报类型", "type": "dic",
                    "choices": "月报,季报"}],
    }
    with using():
        body, _name, payload = patch.build_patch(raw, spec)
    bo_text = pick(body, "bo/")
    row = attr_row(bo_text, "reportType")
    assert row["dataTypeNew"] == "dic", row
    assert row["objectType"] == "report_type", row
    assert row["length"] == "32", row
    assert row["fieldName"] == "report_type_", row      # 新字段的列随自己的编码走
    assert 'typeScope="GLOBAL"' in bo_text
    for key in ("MonthlyReportModules", "QuarterlyReportModules", "PhaseReportModules",
                "WeeklyReportModules", "HalfMonthlyReportModules", "BiweeklyReportModules"):
        assert f'key="{key}"' in bo_text, key
    # 绝不能按自定义字典造 key（导入端会报「引用的字典不存在」）
    assert "contract_reportType" not in bo_text
    assert 'typeScope="BO_ATTR"' not in bo_text
    assert "项目对齐" in payload["report"]


@case("PL20", "projlink", "本对象的「名称/标题」字段不参与对齐（走真实 Excel 路径，编码留空）",
      "P0", ["own_name", "mingCheng 也要拦住", "项目名称进需确认"])
def _():
    # 真实路径：编码留空，naming 会把「名称」翻成 mingCheng（不是 name），
    # 只按 code == "name" 判会漏——这里守的就是那条最常见的路径
    data = sheets([
        field("contract", "名称", "字符串"),            # → mingCheng
        field("contract", "标题", "字符串"),            # → biaoTi
        field("contract", "项目名称", "字符串"),        # B10 可疑名单 → 需确认
    ], bos=[bo_row(关联项目="是")])
    with using():
        res = webui.parse_excel(xlsx(data))
    codes = [f["code"] for f in res["objects"][0]["fields"]]
    assert "mingCheng" in codes, codes
    item = res["projectAlign"]["items"][0]
    aligned = {a["fieldCode"]: a for a in item["exact"] + item["similar"]}
    skipped = {s["fieldCode"]: s["kind"] for s in item["skipped"]}
    assert "mingCheng" not in aligned, list(aligned)
    assert "biaoTi" not in aligned, list(aligned)
    assert skipped.get("mingCheng") == "own_name", item["skipped"]
    assert skipped.get("biaoTi") == "own_name", item["skipped"]
    # 「项目名称」不能被这条守卫吞掉：它要进「需确认」并挂可疑提示
    pending = {a["fieldCode"]: a for a in item["pending"]}
    assert "xiangMuMingCheng" in pending, list(pending)
    assert pending["xiangMuMingCheng"]["suspect"] is True, pending["xiangMuMingCheng"]
    # 精确同名的项目字段也不会被改：名称字段名仍是「名称」
    names = {f["code"]: f["name"] for f in res["objects"][0]["fields"]}
    assert names.get("mingCheng") == "名称", names
    assert names.get("biaoTi") == "标题", names


@case("PL21", "projlink", "对齐带来的 stateId/projectId 不再报「建议去掉 Id 后缀」，自建 contractId 照报",
      "P1", ["命名白名单", "报告不再自相矛盾"])
def _():
    data = sheets([field("contract", "项目状态", "对象",
                         **{"关联对象/字典": "project_state"}),
                   field("contract", "合同ID", "字符串", **{"字段编码": "contractId"})],
                  bos=[bo_row(关联项目="是")])
    with using():
        result = webui.parse_excel(xlsx(data))
    msgs = [i["message"] for i in result["issues"]]
    assert not any("stateId 建议去掉 Id 后缀" in m for m in msgs), msgs
    assert not any("projectId 建议去掉" in m for m in msgs), msgs
    # 用户自建的 contractId 仍然该提醒（规则本身没被废掉）
    assert any("contractId 建议去掉 Id 后缀" in m for m in msgs), msgs


@case("PL22", "projlink", "复用已有链接字段时也要补表单控件，且重复出包不重复 append",
      "P0", ["reuse 也要有下拉", "幂等", "取值来源非空"])
def _():
    from pp6kit import blueprint, builder

    data = sheets([
        field("contract", "关联项目", "对象",
              **{"字段编码": "proj", "关联对象/字典": "project"}),
        field("contract", "合同编号", "字符串", **{"字段编码": "contractNo"}),
    ], bos=[bo_row(关联项目="是")],
        **{S.S_FORM: [{"表单key": "create_contract", "表单名称": "新建合同",
                       "对象编码": "contract", "表单类型": "新建表单"}],
           S.S_FORMFIELD: [{"表单key": "create_contract", "字段编码": "contractNo",
                            "显示标签": "合同编号", "控件类型": "text"}]})

    # ① 出包：表单里必须是 proj 的下拉，且不能再冒出 projectId
    with using():
        body = build(data)
        res = webui.parse_excel(xlsx(data))
    form = pick(body, "form/")
    assert 'name="proj"' in form, form
    assert 'name="projectId"' not in form
    item = res["projectAlign"]["items"][0]
    assert item["linkField"]["action"] == "reuse", item["linkField"]
    assert "projectId" not in {f["code"] for f in res["objects"][0]["fields"]}

    # ② 幂等：同一份 Project 连续调两次，控件不会重复 append
    proj, errors = project_of(data)
    assert not errors, errors
    blueprint.expand(proj)
    with using():
        builder.ensure_project_links(proj)
        builder.ensure_project_links(proj)
    form_obj = next(f for f in proj.forms if f.bo_code == "contract")
    hits = [x for x in form_obj.fields if x.name == "proj"]
    assert len(hits) == 1, [x.name for x in form_obj.fields]
    ctrl = hits[0]
    assert ctrl.field_type == "select", ctrl
    # 取值来源必须接上数据集（系统已有 project / 没有则新建 project_value），不能是空
    assert ctrl.value_source in ("project", "project_value"), ctrl.value_source


@case("PL23", "projlink", "解析页看得见「对齐会丢什么」，且自关联只报一条错误",
      "P0", ["候选值丢失提示在 parse 阶段就有", "前端 pl-warn", "同一问题不重复报"])
def _():
    # ① 损失提示必须在**解析阶段**就出现在 projectAlign 里：
    #    warning 原本只在 apply 里追加，而 /api/parse 不跑 apply → 界面上看不到，
    #    用户只会以为对齐是无损的（zip 注释没人读）
    data = sheets([field("contract", "汇报类型", "选项",
                         **{"字段编码": "huiBaoLeiXing", "选项值": "甲,乙"})],
                  bos=[bo_row(关联项目="是")])
    with using():
        res = webui.parse_excel(xlsx(data))
    exact = res["projectAlign"]["items"][0]["exact"]
    got = next(a["warnings"] for a in exact if a["fieldCode"] == "huiBaoLeiXing")
    assert got == ["自定义候选值将丢失（2 项：甲、乙）"], got

    # ② 前端组件里要有渲染它的位置（否则数据到了界面也不显示）
    from pathlib import Path

    js = (Path(webui.STATIC) / "projlink.js").read_text(encoding="utf-8")
    assert "pl-warn" in js, "projlink.js 没有渲染 warnings 的位置"

    # ③ 自关联：apply 与 validate 都会发现，但用户只需要看到一条
    bad = sheets([field("project", "项目编号", "字符串")],
                 bos=[bo_row("项目", "project", 关联项目="是")])
    with using():
        errs = [i for i in webui.parse_excel(xlsx(bad))["issues"] if i["level"] == "error"]
    assert len(errs) == 1, [e["message"] for e in errs]
    assert "不能勾「关联项目」" in errs[0]["message"], errs[0]
    assert "不能关联自己" not in errs[0]["message"], errs[0]


# ---------------------------------------------------------------- PL24–PL28（QA 独立验收补充）


# 出包产物里每次都会变的东西（都是**既有**行为，与本特性无关）：雪花 id（属性值、
# 以及塞在 attrs JSON 里的一串）、生成时间、容器控件的随机后缀（fieldset_xxx /
# card_xxx …）、隐藏规则与数据来源的随机短 id。比对「零变化」前统一抹掉。
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T[\d:.+-]+")
_SNOW = re.compile(r"\b\d{9,}\b")
_BOX = re.compile(r"(?:fieldset|gridLayout|card|tab|group|collapse|columnLayout)_[a-z0-9]{6,9}")
_JSON_ID = re.compile(
    r'(&quot;|")([A-Za-z_]*[Ii]d)(&quot;|"):(&quot;|")[A-Za-z0-9]{5,9}(&quot;|")')


def volatile_norm(text: str) -> str:
    """抹掉每次出包都会变的 id/时间/随机后缀，剩下的字节用于「零副作用」比对。"""
    text = _ISO.sub("T", text)
    text = _SNOW.sub("N", text)
    text = _BOX.sub("BOX_X", text)
    return _JSON_ID.sub(r"\1\2\3:\4R\5", text)


def _rich_sheets(bo: dict) -> dict:
    """一份带表单（含隐藏规则）/ 列表 / 脚本 / 流程的「全量」样本。"""
    return {
        S.S_BO: [bo],
        S.S_FIELD: [
            field("contract", "项目状态", "选项",
                  **{"字段编码": "projectState", "选项值": "进行中,已结束"}),
            field("contract", "合同编号", "字符串", **{"字段编码": "contractNo"}),
        ],
        S.S_FORM: [{"表单key": "create_contract", "表单名称": "新建合同",
                    "对象编码": "contract", "表单类型": "新建表单"}],
        S.S_FORMFIELD: [
            {"表单key": "create_contract", "字段编码": "projectState",
             "显示标签": "项目状态", "控件类型": "dictionary",
             "取值来源": "contract_projectState"},
            {"表单key": "create_contract", "字段编码": "contractNo",
             "显示标签": "合同编号", "控件类型": "text"},
        ],
        S.S_LIST: [{"列表key": "contract_list", "列表名称": "合同列表",
                    "对象编码": "contract", "视图类型": "grid",
                    "显示字段": "projectState,contractNo", "排序字段": "projectState",
                    "标题字段": "contractNo"}],
        S.S_HIDE: [{"表单key": "create_contract", "隐藏控件": "contractNo",
                    "依赖控件": "projectState", "判断条件": "等于",
                    "条件值": "进行中"}],
        S.S_SCRIPT: [{"表单key": "create_contract", "脚本说明": "内联",
                      "JForm 脚本": "Object.assign(JForm, { onLoad: function(form){"
                                    " form.setData('projectState', 1); } });"}],
    }


@case("PL24", "projlink", "未勾「关联项目」：把特性运行时关掉再出一次包，归一化后逐条完全一致",
      "P0", ["零副作用", "P0 代码路径惰性", "老模板缺列也一致"])
def _():
    from pp6kit import builder

    data = _rich_sheets(bo_row())
    with using():
        first = build(data)

        # 把特性的两个写入口（apply / ensure_project_links）临时置空再出一次包。
        # 两次产物必须一致 —— 不一致就说明「没勾也叫特性改了什么」。
        old_apply = projlink.apply
        old_web = webui.ensure_project_links
        old_build = builder.ensure_project_links
        projlink.apply = lambda *_a, **_k: {
            "noop": True, "notices": [], "aligned": [], "linkFields": [],
            "skipped": [], "conflicts": [], "rewrites": {}, "scriptFormKeys": [],
            "objects": {}, "applied": 0, "summary": {}}
        webui.ensure_project_links = lambda *_a, **_k: []
        builder.ensure_project_links = lambda *_a, **_k: []
        try:
            second = build(data)
        finally:
            projlink.apply = old_apply
            webui.ensure_project_links = old_web
            builder.ensure_project_links = old_build

    a, b = texts(first), texts(second)
    assert sorted(a) == sorted(b), (sorted(a), sorted(b))
    for name in a:
        assert volatile_norm(a[name]) == volatile_norm(b[name]), f"{name} 被特性动过"

    joined = "\n".join(a.values())
    assert "projectId" not in joined, "没勾「关联项目」却补了链接字段"
    assert "stateId" not in joined, "没勾「关联项目」却把字段对齐成了 stateId"

    # 老模板（连「关联项目」这一列都没有）也必须出同一份包：reader 要容忍缺列
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(xlsx(data)))
    ws = wb[S.S_BO]
    header = [c.value for c in ws[1]]
    ws.delete_cols(header.index("关联项目") + 1, 1)
    buf = io.BytesIO()
    wb.save(buf)
    with using():
        legacy = build_bytes_wrapper(buf.getvalue())
    la = texts(legacy)
    assert sorted(la) == sorted(a), (sorted(la), sorted(a))
    for name in a:
        assert volatile_norm(la[name]) == volatile_norm(a[name]), f"{name}: 老模板产物不同"


def build_bytes_wrapper(data: bytes) -> bytes:
    """build() 只接受 sheets dict；这里直接给 xlsx 字节（老模板用例要用）。"""
    return webui.build_zip(data)[0]


@case("PL25", "projlink", "存量包对齐强制覆盖 isnull（否→是）与长度，编码/物理列/sn 仍不变",
      "P0", ["align=True 强制覆盖", "存量包铁律"])
def _():
    from pp6kit import patch as P

    data = sheets([field("contract", "项目编号", "字符串",
                         **{"字段编码": "code", "物理列名": "contract_no_",
                            "允许空": "否", "建索引": "是", "长度": 300})])
    raw = build(data)
    info = P.inspect_package(raw)["objects"][0]
    before = attr_row(pick(raw, "bo/"), "code")
    assert before["isnull"] == "N" and before["length"] == "300", before
    spec = {"targets": [info["file"]], "scope": "full", "mode": "replace",
            "linkProject": [info["file"]], "fields": []}
    with using():
        new_body, _name, _payload = P.build_patch(raw, spec)
    after = attr_row(pick(new_body, "bo/"), "code")
    # 项目侧 isnull=Y / length=64：都必须被覆盖（对齐不是「只在空的时候填」）
    assert after["isnull"] == "Y", after
    assert after["length"] == "64", after
    assert after["isindex"] == "Y", after
    # 铁律：这三项一个都不许变
    assert after["code"] == before["code"]
    assert after["fieldName"] == before["fieldName"]
    assert after["sn"] == before["sn"]


@case("PL26", "projlink", "存量包 mode=skip：纯新增对齐行允许，命中已有字段（含已确认的相近字段）才报错",
      "P0", ["mode 守卫边界", "新增 vs 覆盖", "已确认的相近字段也算覆盖"])
def _():
    from pp6kit import patch as P

    # ① 包内的字段都跟项目对象对不上（合同名称/签署地点），只有用户手填的新字段
    #    「汇报类型」会对齐——它的 code 不在包里，属于纯新增行，skip 模式允许
    plain = build(sheets([field("contract", "合同名称", "字符串", **{"字段编码": "title"}),
                          field("contract", "签署地点", "字符串",
                                **{"字段编码": "signPlace"})]))
    info = P.inspect_package(plain)["objects"][0]
    spec = {"targets": [info["file"]], "scope": "full", "mode": "skip",
            "linkProject": [info["file"]],
            "fields": [{"code": "reportType", "name": "汇报类型", "type": "选项"}]}
    with using():
        body, _name, _payload = P.build_patch(plain, spec)
    bo_text = pick(body, "bo/")
    row = attr_row(bo_text, "reportType")
    assert row["objectType"] == "report_type", row
    assert 'typeScope="GLOBAL"' in bo_text

    # ② 一旦对齐命中包里**已有**字段，skip 模式必须报错（预览与产物同一口径）。
    #    「项目编号」是直接对应（自动），「备注说明」是 S1 相近（用户勾了才覆盖）——
    #    后者必须带上 align=true，否则它压根不进对齐行，这条守卫就测不到了
    for name, code in (("项目编号", "code"), ("备注说明", "remark")):
        hit = build(sheets([field("contract", name, "字符串", **{"字段编码": code})]))
        info2 = P.inspect_package(hit)["objects"][0]
        bad = {"targets": [info2["file"]], "scope": "full", "mode": "skip",
               "linkProject": [info2["file"]], "fields": [],
               "align": {info2["file"]: {code: True}}}
        with using():
            for call in (lambda: P.build_patch(hit, bad), lambda: P.dry_run(hit, bad)):
                try:
                    call()
                except ValueError as exc:
                    assert "对齐已有字段需要覆盖模式" in str(exc), str(exc)
                else:
                    raise AssertionError(f"{name}：命中已有字段的对齐行在 skip 模式下应当报错")


@case("PL27", "projlink", "存量包没勾「关联项目」：报告不出现「项目对齐」、产物不加 projectId",
      "P1", ["P0-5 零副作用", "报告不变"])
def _():
    from pp6kit import patch as P

    data = sheets([field("contract", "项目编号", "字符串", **{"字段编码": "code"})])
    raw = build(data)
    info = P.inspect_package(raw)["objects"][0]
    spec = {"targets": [info["file"]], "scope": "full", "mode": "replace",
            "fields": [{"code": "newField", "name": "新字段", "type": "字符串"}]}
    new_body, _name, payload = P.build_patch(raw, spec)
    assert "项目对齐" not in payload["report"], payload["report"][:400]
    assert "projectId" not in pick(new_body, "bo/"), "没勾也补了链接字段"
    assert "新字段" in payload["report"]


@case("PL28", "projlink", "更新模式的报告写「保留物理列 我方列（项目侧 项目列）」，不拿显示名当列名",
      "P1", ["报告文案", "D1", "PRD A10"])
def _():
    data = sheets([field("contract", "项目编号", "字符串",
                         **{"字段编码": "code", "物理列名": "contract_no_"})],
                  bos=[bo_row(关联项目="是", 已有对象ID="abc123")])
    proj, errors = project_of(data)
    assert not errors, errors
    proj.bo("contract").exist_id = "abc123"
    report = projlink.apply(proj, None, snap=fake_snapshot())
    lines = "\n".join(projlink.report_lines(report))
    # 我方列名（contract_no_）在左、项目侧列名（code_）在右
    assert "保留物理列 contract_no_（项目侧 code_" in lines, lines
    # 显示名「项目编号」绝不能出现在列名的位置（QA D1 的原缺陷）
    assert "保留物理列 项目编号" not in lines, lines
    # 更新模式不该暗示发生了改名：新编码仍是原编码
    row = next(x for x in report["aligned"] if x["fieldCode"] == "code")
    assert row["newCode"] == "code" and row["column"] == "contract_no_", row
    assert row["projectField"] == "code_", row




@case("PL29", "projlink", "字段对齐成关联对象后，原文本框控件强制改成下拉并接上取值来源",
      "P0", ["D2", "控件类型跟随类型", "notice 可见"])
def _():
    # 「项目级别」精确同名 + 同族（字符串→object 都是 varchar 族）→ 自动对齐，
    # 不需要决策头。用户原来把控件做成了文本框——对齐后那个文本框就是死控件。
    data = sheets([field("contract", "项目级别", "字符串",
                         **{"字段编码": "xiangMuJiBie"})],
                  bos=[bo_row(关联项目="是")],
                  **{S.S_FORM: [{"表单key": "create_contract", "表单名称": "新建合同",
                                 "对象编码": "contract", "表单类型": "新建表单"}],
                     S.S_FORMFIELD: [{"表单key": "create_contract",
                                      "字段编码": "xiangMuJiBie",
                                      "显示标签": "项目级别", "控件类型": "text"}]})
    with using():
        body = build(data)
    bo_text = pick(body, "bo/")
    row = attr_row(bo_text, "gradeId")
    assert row["dataTypeNew"] == "object" and row["objectType"] == "project_grade", row
    assert row["fieldName"] == "grade_id_" and row["length"] == "64", row

    import json as _json

    form = pick(body, "form/")
    frag = [t for t in all_tags(form, "formField") if attrs(t).get("name") == "gradeId"]
    assert frag, [attrs(t).get("name") for t in all_tags(form, "formField")]
    got = attrs(frag[0])
    assert got["fieldType"] == "select", got
    opts = _json.loads(got["fieldOptions"].replace("&quot;", '"'))
    assert opts.get("value_source"), opts
    # notice 要让用户知道界面上发生了什么（不是静默改控件）
    assert "控件「项目级别」text → select" in comment_of(body), comment_of(body)


@case("PL30", "projlink", "树形关联对象（options.tree）对齐后控件用 linkdata，取值来源与树形配置都在",
      "P1", ["D2 的另一半", "control_for(tree)", "linkdata + link_config.structure"])
def _():
    # 项目侧「项目阶段」是树形关联对象（线上 options 带 tree=true）。对齐后
    # 控件必须是 linkdata（树选择），不能用普通下拉 select —— 否则只能选到一个
    # 拍平的列表，树形层级全丢。
    tree = projlink.ProjectField(
        code="phaseIds", name="项目阶段", type_new="object", datatype="varchar",
        field_name="phase_ids_", object_type="project_phase", length=1000,
        is_null=True, is_index=False, sn=12,
        options={"tree": True, "isMultiple": True})
    snap = projlink.Snapshot(
        source="target_db", generated_at="2026-11-14T09:12:00+08:00",
        project={"code": "project", "name": "项目", "tbl": "pp_project"},
        fields=[tree], system_codes=set(projlink.SYSTEM_CODES),
        warnings=[], declared_total=1)
    data = sheets([field("contract", "项目阶段", "字符串", **{"字段编码": "jieDuan"})],
                  bos=[bo_row(关联项目="是")],
                  **{S.S_FORM: [{"表单key": "create_contract", "表单名称": "新建合同",
                                 "对象编码": "contract", "表单类型": "新建表单"}],
                     S.S_FORMFIELD: [{"表单key": "create_contract",
                                      "字段编码": "jieDuan",
                                      "显示标签": "项目阶段", "控件类型": "text"}]})
    with using(snap):
        body = build(data)
    # 字段本身对齐成项目侧定义（含 tree 选项）
    row = attr_row(pick(body, "bo/"), "phaseIds")
    assert row["dataTypeNew"] == "object" and row["objectType"] == "project_phase", row
    assert "tree" in row.get("options", ""), row

    import json as _json

    form = pick(body, "form/")
    frag = [t for t in all_tags(form, "formField") if attrs(t).get("name") == "phaseIds"]
    assert frag, [attrs(t).get("name") for t in all_tags(form, "formField")]
    got = attrs(frag[0])
    assert got["fieldType"] == "linkdata", got
    opts = _json.loads(got["fieldOptions"].replace("&quot;", '"'))
    # linkdata 的取值来源写在 linkdata 键上（gen_form 契约），不是 value_source
    assert opts.get("linkdata") == "project_phase", opts
    assert (opts.get("link_config") or {}).get("structure") == "tree", opts.get("link_config")
    assert "控件「项目阶段」text → linkdata" in comment_of(body), comment_of(body)


# ---------------------------------------------------------------- PL31–PL37
# 解析页「关联项目」开关（2026-11-14 用户反馈「你怎么区分这个对象是否关联项目呢
# 咋没有选项」）：维护者只会在 Excel 里填，页面上只看到一个 chip，等于没有入口。
# 这 7 条守的是：开关能落模型、能撤销 Excel 的「是」、三条出包路径都带上它。


def pl_header(objects: dict) -> str:
    """造一份 X-Project-Link 决策头（前端 projLinkHeader() 的等价物）。"""
    import json as _json

    return _json.dumps({"version": 1, "objects": objects}, ensure_ascii=False)


def _linked_data(关联项目: str = "") -> dict:
    """一个普通对象 + 「项目编号」（精确同名，会自动对齐成 code）。"""
    row = bo_row()
    if 关联项目:
        row["关联项目"] = 关联项目
    return sheets([field("contract", "项目编号", "字符串"),
                   field("contract", "项目状态", "对象",
                         **{"关联对象/字典": "project_state"})],
                  bos=[row])


@case("PL31", "projlink", "解析页勾「关联项目」（Excel 没这一列）→ 预览里就有 projectId，且字段已对齐",
      "P0", ["data-pl-link", "X-Project-Link linked", "预览=产物"])
def _():
    # Excel 完全不写「关联项目」列：全靠请求头里的 linked=true
    data = _linked_data()
    with using():
        res = webui.parse_excel(xlsx(data), pl_header({"contract": {"linked": True}}))
    bo = res["objects"][0]
    assert bo["projectLink"] is True, bo
    codes = {f["code"] for f in bo["fields"]}
    # 链接字段与对齐后的编码都必须已经出现在预览里（否则「预览≠产物」）
    assert "projectId" in codes, codes
    assert "code" in codes, codes
    item = res["projectAlign"]["items"][0]
    assert item["objectCode"] == "contract", item
    assert item["linked"] is True, item
    # 界面得有一句「这里勾就行」，否则用户还是不知道入口在哪
    notes = " ".join(res["notes"])
    assert "来自界面开关" in notes, res["notes"]


@case("PL32", "projlink", "页面开关关掉能撤销 Excel 里的「是」：无 projectId，且页签不再算这个对象",
      "P0", ["linked=false 优先", "用户明确关就得关"])
def _():
    data = _linked_data(关联项目="是")
    with using():
        res = webui.parse_excel(xlsx(data), pl_header({"contract": {"linked": False}}))
    bo = res["objects"][0]
    assert bo["projectLink"] is False, bo
    codes = {f["code"] for f in bo["fields"]}
    assert "projectId" not in codes, codes
    item = res["projectAlign"]["items"][0]
    assert item["linked"] is False, item


@case("PL33", "projlink", "决策头里的对象已不在 Excel 里 → 解析照常成功，只给一条忽略提示",
      "P1", ["不 400", "旧 code 不该毁掉整次解析"])
def _():
    data = _linked_data()
    with using():
        res = webui.parse_excel(xlsx(data), pl_header({"nope": {"linked": True}}))
    assert res["ok"] is True, res
    hits = [n for n in res["notes"] if "忽略" in n]
    assert hits, res["notes"]
    assert "nope" in hits[0], hits
    # 真正的对象没被动过：没勾就是没勾
    assert res["objects"][0]["projectLink"] is False, res["objects"][0]


@case("PL34", "projlink", "同步包带开关决策：bo XML 里有 projectId，同名字段按项目口径对齐",
      "P0", ["X-Project-Link 出包生效", "改的是真产物不是中间变量"])
def _():
    data = _linked_data()
    with using():
        body = webui.build_zip(xlsx(data), projlink_header=pl_header(
            {"contract": {"linked": True}}))[0]
    bo = pick(body, "bo/")
    link = attr_row(bo, "projectId")
    assert link["objectType"] == "project", link
    # 「项目编号」精确同名同族 → code / code_ / 64 / 索引 Y
    row = attr_row(bo, "code")
    assert row["fieldName"] == "code_" and row["length"] == "64", row
    assert row["isindex"] == "Y", row
    # 「项目状态」是对象字段，精确同名 → stateId + 物理列 state_id_
    state = attr_row(bo, "stateId")
    assert state["fieldName"] == "state_id_", state
    assert state["objectType"] == "project_state", state


@case("PL35", "projlink", "确认版 Excel 带开关决策：1_业务对象 的「关联项目」= 是（下次打开不用再勾）",
      "P0", ["决策写回 Excel", "下次解析继承"])
def _():
    data = _linked_data()
    with using():
        xls, _name = webui.confirmed_excel(
            xlsx(data), name="合同.xlsx",
            projlink_header=pl_header({"contract": {"linked": True}}))
    rows = [r for r in read_xlsx_rows(xls, S.S_BO)
            if str(r.get("对象编码") or "").strip() == "contract"]
    assert rows, read_xlsx_rows(xls, S.S_BO)
    assert rows[0]["关联项目"] == "是", rows[0]
    # 「所属项目」的字段行也要在，否则再上传会被当成「没关联项目」跑一遍
    codes = [str(r.get("字段编码") or "") for r in read_xlsx_rows(xls, S.S_FIELD)]
    assert "projectId" in codes, codes


@case("PL36", "projlink", "流程对象包（X-Object-Scope=workflow）同样带上 projectId",
      "P0", ["流程对象也要能关联项目", "三条出包路径都带头"])
def _():
    # 流程对象单独出一包，最容易漏决策头：漏了就是「页面有、包里没有」
    data = sheets([field("contract", "项目编号", "字符串")],
                  bos=[bo_row(流程驱动="是")])
    with using():
        body = webui.build_zip(xlsx(data), object_scope="workflow",
                               projlink_header=pl_header(
                                   {"contract": {"linked": True}}))[0]
    bo = pick(body, "bo/")
    assert 'mode="workflow"' in bo, bo[:200]
    link = attr_row(bo, "projectId")
    assert link["objectType"] == "project", link


@case("PL37", "projlink", "前端：对象卡片有可勾的开关，四条出包路径共用同一份决策头",
      "P0", ["data-pl-link", "projLinkHeader", "不允许各拼一份", "头里有 linked + align"])
def _():
    from pathlib import Path

    static = Path(webui.STATIC)
    js = (static / "app.js").read_text(encoding="utf-8")
    css = (static / "style.css").read_text(encoding="utf-8")
    # ① 卡片上真有这个开关（用户能看到、能点）
    assert "data-pl-link" in js, "app.js 没有「关联项目」开关"
    assert "function projLinkHeader" in js, "app.js 没有统一决策头 projLinkHeader"
    # ② 决策头只允许有**一个**来源：app.js 的 projLinkHeader()（state.plLinked
    #    + state.plAlign）。绝不再向 ProjLink 要整份决策——那会变成前后端两套口径。
    assert js.count("ProjLink.decisionsHeader()") == 0, \
        "决策头又开始问 ProjLink 要整份决策了（第二套口径）"
    assert "state.plAlign" in js, "决策头没有合并「相近字段确认」里勾的字段（align）"
    calls = js.count("projLinkHeader()")
    assert calls >= 4, f"projLinkHeader() 调用点只有 {calls} 处（同步包/流程对象包/确认版/重新解析）"
    assert js.count('headers["X-Project-Link"] = plHeader') >= 4, \
        f"漏了某条出包路径：{js.count('headers[\"X-Project-Link\"] = plHeader')} 处"
    # ③ 开关有样式（不然是裸 checkbox 挤在标题里）
    assert ".pl-switch" in css, "style.css 没有 .pl-switch"
    # ④ 确认弹窗的样式在（分组 / chip / 勾选 / 候选下拉）
    for cls in (".pl-rel-row", ".pl-rel-group", ".pl-rel-chip", ".pl-rel-pick",
                ".pl-rel-cand", ".pl-rel-table", ".pl-modal-wide"):
        assert cls in css, f"style.css 没有 {cls}"
    # ④ 帮助文本也说了「不用改 Excel」，用户不必先读到这条反馈才知道
    from pp6kit import spec as _S

    help_text = "".join(t for t, _k in _S.HELP_TEXT)
    assert "不用改 Excel" in help_text, "帮助文本没提页面上的开关"


@case("PL38", "projlink", "真 HTTP 往返：解析/同步包/流程对象包/确认版 Excel 四条请求都认同一个决策头",
      "P0", ["端到端", "确认版 Excel 路由", "不做「函数能跑」的自欺断言"])
def _():
    # 上面几条都是**函数级**断言。真实翻车点在接线上：路由漏了头、或者
    # 路径带 query 没剥（/api/confirmed-excel?name=… 曾必 404），函数再好也没用。
    import http.client
    import json as _json
    import threading
    import urllib.parse

    httpd, port = webui.make_server(18973)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def post(path: str, body: bytes, headers: dict) -> tuple[int, dict, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        try:
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()

    hdr = pl_header({"contract": {"linked": True}})
    base = {"Content-Type": "application/octet-stream"}
    try:
        with using():
            raw = xlsx(_linked_data())
            # ① 解析页：不带开关 → 没有 projectId；带开关 → 有
            _, _, plain = post("/api/parse", raw, dict(base))
            _, _, linked = post("/api/parse", raw, {**base, "X-Project-Link": hdr})
            assert _json.loads(plain)["objects"][0]["projectLink"] is False
            got = _json.loads(linked)
            assert got["objects"][0]["projectLink"] is True, got["objects"][0]
            assert "projectId" in {f["code"] for f in got["objects"][0]["fields"]}

            # ② 同步包
            status, headers, body = post("/api/build", raw, {
                **base, "X-Modules": _json.dumps(["bo"]), "X-Project-Link": hdr})
            assert status == 200, body[:200]
            assert 'code="projectId"' in pick(body, "bo/"), "同步包没带 projectId"

            # ③ 流程对象包（独立入口，最容易漏头）
            wf = xlsx(sheets([field("contract", "项目编号", "字符串")],
                             bos=[bo_row(流程驱动="是")]))
            status, headers, body = post("/api/build", wf, {
                **base, "X-Object-Scope": "workflow",
                "X-Modules": _json.dumps(["bo", "form"]), "X-Project-Link": hdr})
            assert status == 200, body[:200]
            assert 'code="projectId"' in pick(body, "bo/"), "流程对象包没带 projectId"

            # ④ 确认版 Excel：前端带 ?name=，路由必须剥 query 后才比（否则 404）
            status, headers, body = post(
                "/api/confirmed-excel?name=" + urllib.parse.quote("合同.xlsx"), raw,
                {**base, "X-Project-Link": hdr})
            assert status == 200, body[:200]
            rows = [r for r in read_xlsx_rows(body, S.S_BO)
                    if str(r.get("对象编码") or "").strip() == "contract"]
            assert rows and rows[0]["关联项目"] == "是", rows
    finally:
        httpd.shutdown()


@case("PL39", "projlink", "模块行的「导入」按钮（/api/import/<code>）也带关联项目决策，且不污染解析缓存",
      "P0", ["单模块包带 projectId", "_cache_proj 深拷贝", "开关关得掉"])
def _():
    # 这条路径在界面上比「下载导入包」更显眼（每个模块行一个「导入」），
    # 漏了就是「勾了关联项目 → 点导入 → 包里还是没有 projectId」。
    # 第三段专门守深拷贝：_cache_proj 跨请求共享，apply 直接改缓存的话，
    # 下一次**没带头**的请求也会冒出 projectId（用户会以为开关关不掉）。
    import http.client
    import threading

    httpd, port = webui.make_server(18974)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def post(path: str, body: bytes, headers: dict) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        try:
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    # Excel 里**没有**「关联项目」列：全靠决策头
    with using():
        raw = xlsx(_linked_data())
        base = {"Content-Type": "application/octet-stream"}
        try:
            # ① 不带头：单模块包里没有 projectId
            status, body = post("/api/import/bo", raw, dict(base))
            assert status == 200, body[:200]
            assert 'code="projectId"' not in pick(body, "bo/"), \
                "没勾关联项目，单模块包里却有 projectId"

            # ② 带头 linked=true：有 projectId，且同名字段已按项目口径对齐
            status, body = post("/api/import/bo", raw,
                                {**base, "X-Project-Link": pl_header(
                                    {"contract": {"linked": True}})})
            assert status == 200, body[:200]
            bo = pick(body, "bo/")
            link = attr_row(bo, "projectId")
            assert link["objectType"] == "project", link
            row = attr_row(bo, "code")          # 项目编号 → code / code_ / 64
            assert row["fieldName"] == "code_" and row["length"] == "64", row

            # ③ 紧接着再发一次**不带**头的：仍然没有 projectId
            #    （apply 若改了 _cache_proj，这里就会漏出来）
            status, body = post("/api/import/bo", raw, dict(base))
            assert status == 200, body[:200]
            assert 'code="projectId"' not in pick(body, "bo/"), \
                "深拷贝没做：上一次的决策污染了 _PROJ_CACHE"
        finally:
            httpd.shutdown()


# ---------------------------------------------------------------- PL40–PL47
# QA 独立验收（2026-11-14 第二轮）：解析页「关联项目」开关。
# PL31–PL39 是工程师自测，覆盖了「勾了就有、关掉就撤、四条路径都带头」；
# 这 8 条补的是**自测容易漏的那半边**：坏请求怎么回、不带头的零变化、
# 前端真的按状态渲染/回滚（而不是「源码里有这个字符串」）、以及四条出包路径
# 产出的 projectId 定义是否逐键一致。


def _probe_frontend() -> dict:
    """真跑 tests/fe_projlink_probe.js（node + 最小 DOM stub），返回结果 dict。

    只 grep 源码证明不了「开关能压住 ProjLink 的 linked」「失败会回滚」这类行为，
    所以这里把 app.js 放进 vm 跑一遍。没装 node 就 skip（不装依赖、不联网）。
    """
    import json as _json
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        skip("没装 node：前端行为探针跳过（PL37 的函数级断言仍然生效）")
    app = Path(webui.STATIC) / "app.js"
    probe = Path(__file__).resolve().parent / "fe_projlink_probe.js"
    assert probe.is_file(), f"缺少前端探针 {probe}"
    proc = subprocess.run([node, str(probe), str(app)],
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, f"node 探针失败：\n{proc.stderr[-1200:]}"
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    assert lines, "node 探针没有输出"
    return _json.loads(lines[-1])


def _post(port: int, path: str, body: bytes, headers: dict) -> tuple[int, dict, bytes]:
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


@case("PL40", "projlink", "坏决策头：/api/parse 与 /api/build 都回 400，且说的话能看懂",
      "P0", ["坏请求不能 500", "两条路由同一口径", "文案可读"])
def _():
    # 头是前端拼的，坏的概率低但一旦坏了：不能 200 静默出包（用户会拿到没关联项目的包），
    # 也不能 500（前端只看到「解析失败」五个字，不知道去改哪里）。
    import json as _json
    import threading

    httpd, port = webui.make_server(18981)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = {"Content-Type": "application/octet-stream"}
    cases = [
        ("{bad", "X-Project-Link"),        # 根本不是 JSON
        (_json.dumps({"version": 2, "objects": {}}), "version"),   # 版本不认识
        (_json.dumps({"version": 1, "objects": {"contract": {"linked": "yes"}}}),
         "true/false"),                    # 值不是布尔
    ]
    try:
        with using():
            raw = xlsx(_linked_data())
        for bad, expect in cases:
            status, _h, body = _post(port, "/api/parse", raw, {**base, "X-Project-Link": bad})
            assert status == 400, (bad, status, body[:200])
            msg = _json.loads(body)["error"]
            assert expect in msg, (bad, msg)
            assert "X-Project-Link" in msg or "决策" in msg, (bad, msg)
            assert "Traceback" not in msg, msg

            status, _h, body = _post(port, "/api/build", raw,
                                     {**base, "X-Modules": _json.dumps(["bo"]),
                                      "X-Project-Link": bad})
            assert status == 400, (bad, status, body[:200])
            msg = _json.loads(body)["error"]
            assert expect in msg, (bad, msg)
    finally:
        httpd.shutdown()


@case("PL41", "projlink", "不带头零变化：解析结果与改开关之前逐键相同，包里没有 projectId/stateId",
      "P0", ["零变化", "开关不能偷偷改掉老行为"])
def _():
    import json as _json

    with using():
        raw = xlsx(_linked_data())
        a = webui.parse_excel(raw)
        b = webui.parse_excel(raw, None)          # 显式传 None = 老调用方式
        assert _json.dumps(a, sort_keys=True, ensure_ascii=False) == \
            _json.dumps(b, sort_keys=True, ensure_ascii=False), \
            "传 None 与不传的结果不一致：老客户端会看到不同的预览"
        bo = a["objects"][0]
        assert bo["projectLink"] is False, bo
        # 没勾 → 字段仍是用户/自动生成的旧编码，没有被对齐
        assert {f["code"] for f in bo["fields"]} == {"xiangMuBianHao", "xiangMuZhuangTai"}, \
            [f["code"] for f in bo["fields"]]
        assert a["projectAlign"]["items"][0]["linked"] is False
        assert a["projectAlign"]["summary"]["linkFields"] == 0

        body = webui.build_zip(raw, codes=["bo"], object_scope="plain")[0]
    xml = pick(body, "bo/")
    assert 'code="projectId"' not in xml, "没勾关联项目，包里却有 projectId"
    assert 'code="stateId"' not in xml, "没勾关联项目，字段却被对齐成 stateId"
    assert 'objectType="project"' not in xml, "没勾关联项目，却出现了指向 project 的字段"
    assert 'code="xiangMuBianHao"' in xml, "字段编码被改动了"


@case("PL42", "projlink", "前端决策头：界面开关（linked）+ 勾选的相近字段（align）合并成一份",
      "P0", ["一个头只有一个来源", "开关优先", "不发空头", "align 只发 true"])
def _():
    # 2026-11-15 起头里有两个键：linked（对象卡片开关）+ align（「相近字段确认」
    # 弹窗里勾上的字段）。偏一份就会「预览改了、包里没改」——用户导入后才发现。
    # 探针同时记账 ProjLink.decisionsHeader/collect/restore 的调用次数（必须全 0）：
    # 一旦有人把它们接回来，就有第二套口径了。
    res = _probe_frontend()
    import json as _json

    got = _json.loads(res["G_switchWins"])
    assert got["version"] == 1, got
    assert got["objects"]["contract"]["linked"] is False, got
    # 勾上的字段进 align；没勾的（false）不发——服务端不需要「确认不对齐」
    assert got["objects"]["contract"]["align"] == {"projectState": True}, got
    assert res["G_plCalls"] == {"decisionsHeader": 0, "collect": 0, "restore": 0}, res["G_plCalls"]
    # 什么都没拨 → 不带空头（空头会让 /api/build 拿到一个 {} 决策）
    assert res["G_emptyIsNull"] is None, res["G_emptyIsNull"]
    # 只拨了开关（没有勾选字段）：头里只有这一个对象，且没有 align 键
    both = _json.loads(res["G_plusUntouched"])
    assert both["objects"]["apply"]["linked"] is True, both
    assert "align" not in both["objects"]["apply"], both
    # 只勾了字段（没拨开关）：align 单独也要能出去，否则勾选只在预览里生效
    assert both["objects"]["contract"]["align"] == {"projectState": True}, both
    # 多候选：align 的值可以是项目字段编码，必须原样发（压成 true 就变成最匹配了）
    cand = _json.loads(res["G_candPick"])
    assert cand["objects"]["contract"] == {
        "linked": False, "align": {"projectState": "gradeId"}}, cand


@case("PL43", "projlink", "对象卡片：普通对象与流程对象都有开关，勾选态来自模型，无文件时禁用并说明原因",
      "P0", ["流程对象也要能勾", "勾选态=bo.projectLink", "载入示例禁用"])
def _():
    res = _probe_frontend()
    # ① 一个对象一张卡片一个开关（普通 + 流程对象都要有：流程对象是单独出包的，
    #    恰恰是最容易漏的那一类）
    assert res["H_rows"] == 2, f"开关数量不对：{res['H_rows']}"
    assert res["H_normalHasSwitch"] and res["H_flowHasSwitch"], res
    # ② 勾选态 = 后端给的 bo.projectLink（不是前端自己猜）
    assert res["H_checkedFromModel"], res
    assert res["H_uncheckedWhenFalse"], "projectLink=false 的对象被渲染成勾上的"
    # ③ 有文件时可拨：不能残留 disabled
    assert res["H_enabled"], "有 Excel 时开关却是禁用的"
    # ④ 处理器真挂上了（挂不上就是「点了没反应」）
    assert res["H_handlerOk"], "data-pl-link 元素没有绑定 onchange"
    # ⑤ 预览上要有「关联项目」chip，用户才知道这个对象已经勾上了
    assert res["H_chip"], res
    # ⑥ 顶部一句「这里勾选即可，不用改 Excel」；载入示例（无文件）时禁用并说清原因
    assert res["H_tip"], "面板顶部没有「不用改 Excel」的提示"
    assert res["H_noFileDisabled"], "载入示例时开关仍可点（点了只会报错）"
    assert res["H_noFileTipInTitle"], "禁用状态下没告诉用户为什么不能点"


@case("PL44", "projlink", "拨开关：成功后清干净上一份包结构；失败回滚到拨之前那一档",
      "P0", ["预览≠产物 的防呆", "失败不能假装成功", "没有文件就别发请求", "不再回填勾选"])
def _():
    res = _probe_frontend()
    ok = res["I_ok"]
    # ① 请求真的带上了决策头，打的是 /api/parse（重新分析而不是本地改状态）
    assert ok["url"] == "/api/parse", ok["url"]
    import json as _json

    assert _json.loads(ok["header"])["objects"]["contract"]["linked"] is True, ok["header"]
    assert ok["plLinked"] == {"contract": True}, ok["plLinked"]
    # ② 与 handleFile 同一套重置：新关联的对象要能再弹差异对照，
    #    第 7 tab 不能残留上一份包的结构/实测清单
    assert ok["plModalShown"] is False, "plModalShown 没重置，新关联的差异不会再弹"
    assert ok["buildReport"] is None, "buildReport 没清，第 7 tab 还是上一份包的实测"
    assert ok["inspectSummary"] is None, "inspectSummary 没清"
    assert ok["zipPhase"] == {"build": "idle", "inspect": "idle"}, ok["zipPhase"]
    assert ok["renderAll"] == 1, f"renderAll 调用 {ok['renderAll']} 次"
    # ③ 不再有「回填相似字段勾选」这件事：覆盖在解析时就完成了，差异由后端重算。
    #    一旦有人把 collect/restore 接回来，说明老语义（逐条勾选）复活了。
    assert ok["collectCalls"] == 0 and ok["restoreCalls"] == 0, \
        f"还在回填勾选：collect={ok['collectCalls']} restore={ok['restoreCalls']}"
    assert ok["restored"] == [], ok["restored"]
    assert "关联项目=是" in ok["toast"][0], ok["toast"]
    # ④ 失败：state 与 checkbox 都回到拨之前（之前没决策 → 连键都不该留）
    fail = res["I_fail"]
    assert fail["hasKey"] is False, f"失败后残留了决策：{fail}"
    assert fail["inputChecked"] is False, "失败后 checkbox 还是勾着的（假成功）"
    assert fail["inputDisabled"] is False, "失败后 checkbox 还是禁用的，用户没法重试"
    assert "失败" in fail["toast"][0], fail["toast"]
    # ⑤ 之前明确是「否」→ 回滚成 false，不能退化成「没决策」（那会退回 Excel 的「是」）
    assert res["I_failPrev"] == {"plLinked": {"contract": False}, "inputChecked": False}, \
        res["I_failPrev"]
    # ⑥ 载入示例（没有文件）：不发请求、不改状态，只提示先上传
    nf = res["I_noFile"]
    assert nf["fetchCalls"] == 0, "没有 Excel 也发了 /api/parse"
    assert nf["plLinked"] == {}, nf
    assert nf["inputChecked"] is False, nf
    assert "上传 Excel" in nf["toast"][0], nf["toast"]
    # ⑦ 勾上后再取消 → 头里必须是 linked:false（删键会退回 Excel 的「是」）
    un = res["I_uncheck"]
    assert _json.loads(un["header"])["objects"]["contract"]["linked"] is False, un["header"]
    assert un["plLinked"] == {"contract": False}, un


@case("PL45", "projlink", "四条出包路径的 projectId 定义逐键一致（只 sn 随对象字段数变）",
      "P1", ["同一份模型", "不允许某条路径用别的口径"])
def _():
    import urllib.parse

    hdr = pl_header({"contract": {"linked": True}})
    data = sheets([field("contract", "合同编号", "字符串", **{"字段编码": "contractNo"})],
                  bos=[bo_row()],
                  **{S.S_FORM: [{"表单key": "create_contract", "表单名称": "新建合同",
                                 "对象编码": "contract", "表单类型": "新建表单"}],
                     S.S_FORMFIELD: [{"表单key": "create_contract", "字段编码": "contractNo",
                                      "显示标签": "合同编号", "控件类型": "text"}]})
    wf_data = sheets([field("contract", "合同编号", "字符串", **{"字段编码": "contractNo"})],
                     bos=[bo_row(流程驱动="是")])
    with using():
        raw = xlsx(data)
        main = attr_row(pick(webui.build_zip(raw, codes=["bo"], object_scope="plain",
                                              projlink_header=hdr)[0], "bo/"), "projectId")
        wf = attr_row(pick(webui.build_zip(xlsx(wf_data), codes=["bo"],
                                           object_scope="workflow",
                                           projlink_header=hdr)[0], "bo/"), "projectId")
        single = attr_row(pick(webui.import_module(raw, "bo", hdr)[0], "bo/"), "projectId")
        xls, _name = webui.confirmed_excel(raw, name="合同.xlsx", projlink_header=hdr)

    # sn 是「对象内字段序号」，流程对象多 4 个字段 → sn 必然不同，这是对的行为
    def strip_sn(row: dict) -> dict:
        return {k: v for k, v in row.items() if k != "sn"}

    base = strip_sn(main)
    for label, row in (("流程对象包", wf), ("单模块包", single)):
        assert strip_sn(row) == base, f"{label} 的 projectId 定义与同步包不一致：\n{row}\n{base}"
    assert main["objectType"] == "project" and main["length"] == "64", main
    assert main["isnull"] == "Y" and main["isindex"] == "N", main
    # 确认版 Excel 写回 2_字段 的那一行也得是同一份定义
    row = next(r for r in read_xlsx_rows(xls, S.S_FIELD)
               if str(r.get("字段编码") or "").strip() == "projectId")
    assert str(row.get("物理列名") or "").strip() == "project_id_", row
    assert str(row.get("长度") or "").strip() in ("64", "64.0"), row


@case("PL46", "projlink", "确认版 Excel 路由剥 query 的回归：带 ?name= / 不带 query / 中文名都不 404",
      "P0", ["本次顺手修的 404", "别修一处漏一处"])
def _():
    import threading
    import urllib.parse

    httpd, port = webui.make_server(18982)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = {"Content-Type": "application/octet-stream"}
    try:
        with using():
            raw = xlsx(_linked_data())
        for path in ("/api/confirmed-excel?name=" + urllib.parse.quote("合同.xlsx"),
                     "/api/confirmed-excel",
                     "/api/confirmed-excel?name="):
            status, headers, body = _post(port, path, raw, dict(base))
            assert status == 200, f"{path} -> {status} {body[:150]}"
            assert body[:2] == b"PK", f"{path} 回的不是 xlsx"
            assert headers.get("x-filename"), f"{path} 没给文件名"
        # 带中文名时文件名不能变成一坨乱码/百分号（前端会 decodeURIComponent）
        status, headers, _body = _post(
            port, "/api/confirmed-excel?name=" + urllib.parse.quote("合同.xlsx"), raw, dict(base))
        assert urllib.parse.unquote(headers["x-filename"]).startswith("合同"), headers["x-filename"]
    finally:
        httpd.shutdown()


@case("PL47", "projlink", "开关新补的「所属项目」在表单里是下拉、取值来源接 project（add 路径）",
      "P0", ["导入后表单里选得到项目", "PL22 只覆盖 reuse"])
def _():
    # add 路径与 reuse 是两段代码。只覆盖 reuse 的话，勾了开关却发现表单里没有
    # 「所属项目」控件（或是个裸文本框），只有导入后才会发现。
    data = sheets([field("contract", "合同编号", "字符串", **{"字段编码": "contractNo"})],
                  bos=[bo_row()],
                  **{S.S_FORM: [{"表单key": "create_contract", "表单名称": "新建合同",
                                 "对象编码": "contract", "表单类型": "新建表单"}],
                     S.S_FORMFIELD: [{"表单key": "create_contract", "字段编码": "contractNo",
                                      "显示标签": "合同编号", "控件类型": "text"}]})
    with using():
        body = webui.build_zip(xlsx(data), codes=["bo", "form"],
                               projlink_header=pl_header(
                                   {"contract": {"linked": True}}))[0]
    form = pick(body, "form/")
    assert "projectId" in form, "表单里没有「所属项目」控件"
    assert '"field_type":"select"' in form.replace("&quot;", '"'), "所属项目不是下拉"
    assert '"value_source":"project"' in form.replace("&quot;", '"'), \
        "所属项目的取值来源没接上 project（导入后选不到项目）"
    assert "所属项目" in form, "控件标签不是「所属项目」"


# ---------------------------------------------------------------- PL48–PL58
# 2026-11-15 用户口径（改死了，替代 11-14 的「相似也自动覆盖」）：
#   · **只有「直接对应」自动覆盖**（E0 精确同名 + 同族 + 不可疑 + 候选唯一）；
#   · 语义相近（S3/S4）、可能相关（S2/S1）**不自动覆盖**，弹窗逐条列出，
#     默认保留我方，勾了「按项目覆盖」才覆盖；
#   · 同名但类型跨族 / 可疑快照 / 多候选 / 冲突 → 也进弹窗，归「需确认」；
#   · 未匹配字段保留、按原规则生成。
# 这 11 条守的就是这条口径：谁自动（PL48）、谁要确认（PL48/PL53）、
# 可疑与跨族（PL54）、无关（PL50/PL55）、多候选（PL56）、冲突（PL57）、
# 关系表本身（PL58），以及弹窗画得出来（PL52）。


def _dic_snapshot() -> projlink.Snapshot:
    """项目侧的「项目类型」是**全局字典选项**（候选值 key 是业务码 1/2/3）。

    为什么单独造一份：默认的 fake_snapshot 里「项目类型」是**关联对象**
    （classificationId），没有候选值，测不了「选项值差异」这件事。
    """
    items = tuple(
        projlink.DictItem(key=k, name=n, sn=i)
        for i, (k, n) in enumerate((("1", "基建"), ("2", "技改"), ("3", "检修")), start=1)
    )
    meta = projlink.DictMeta(ref="project_type", name="项目类型", stru_type="1",
                             scope="GLOBAL", type_sn=1, items=items)
    snap = fake_snapshot()
    snap.fields = [f for f in snap.fields if f.code != "classificationId"]
    snap.fields.append(_pf("projectType", "项目类型", "dic", 64, "project_type", sn=3,
                           options={"isMultiple": False}, meta=meta))
    return snap


@case("PL48", "projlink", "S4 相近字段不再自动覆盖：默认保留我方，勾了「按项目覆盖」才照项目定义改",
      "P0", ["直接对应才自动", "S4 进 pending", "align=true 才覆盖"])
def _():
    # 「项目类别」与项目侧「项目类型」是 S4（同义词归一）。2026-11-14 那版口径是
    # 「相似也自动覆盖」，11-15 改回「必须逐条确认」——这条用例是那次翻转的守门人。
    data = sheets([field("contract", "项目类别", "选项",
                         **{"字段编码": "projCategory", "选项值": "基建,技改"})],
                  bos=[bo_row(关联项目="是")])
    with using():
        off = webui.parse_excel(xlsx(data))
        res = webui.parse_excel(xlsx(data), pl_header(
            {"contract": {"align": {"projCategory": True}}}))
        body = build(data)
        yes = webui.build_zip(xlsx(data), projlink_header=pl_header(
            {"contract": {"align": {"projCategory": True}}}))[0]
    # ① 没勾：不进 aligned，进 pending；预览里编码/类型/候选值都是我方的
    item = off["projectAlign"]["items"][0]
    got = {a["fieldCode"]: a for a in item["exact"] + item["similar"]}
    assert "projCategory" not in got, list(got)
    pend = {a["fieldCode"]: a for a in item["pending"]}
    assert "projCategory" in pend, item["pending"]
    assert pend["projCategory"]["rule"] == "S4", pend["projCategory"]
    assert pend["projCategory"]["auto"] is False, pend["projCategory"]
    assert pend["projCategory"]["basis"].startswith("同义词"), pend["projCategory"]["basis"]
    codes = {f["code"] for f in off["objects"][0]["fields"]}
    assert "projCategory" in codes and "classificationId" not in codes, codes
    # 导出包同样保留我方定义
    assert "classificationId" not in {attrs(a).get("code")
                                       for a in all_tags(pick(body, "bo/"), "attribute")}
    # ② 勾了：真的覆盖（编码/物理列/类型/关联对象/长度全部照项目侧）
    item2 = res["projectAlign"]["items"][0]
    applied = {a["fieldCode"]: a for a in item2["exact"] + item2["similar"]}
    assert applied["projCategory"]["status"] == "applied", applied["projCategory"]
    assert applied["projCategory"]["confirmed"] is True, applied["projCategory"]
    codes2 = {f["code"] for f in res["objects"][0]["fields"]}
    assert "classificationId" in codes2, codes2
    row = attr_row(pick(yes, "bo/"), "classificationId")
    assert row["name"] == "项目类型", row                 # 显示名跟着项目
    assert row["fieldName"] == "classification_id_", row    # 物理列跟着项目
    assert row["dataTypeNew"] == "object", row              # 类型跟着项目
    assert row["objectType"] == "classification", row
    assert row["length"] == "64", row                       # 长度跟着项目（我方是没填）
    # 差异明细能对上前后的值（弹窗要逐项列出来）
    diff = next(d for d in res["projectAlign"]["diffs"] if d["fieldCode"] == "projCategory")
    changes = {c["key"]: c for c in diff["changes"]}
    assert changes["name"]["project"] == "项目类型" and changes["name"]["mine"] == "项目类别"
    assert changes["code"]["project"] == "classificationId"
    assert changes["fieldName"]["project"] == "classification_id_"
    assert changes["type"]["mine"] == "选项" and changes["type"]["project"] == "对象"


@case("PL49", "projlink", "选项值差异逐条列出：key 不同 = changed，单侧的 = 项目新增 / 将被替换",
      "P0", ["每个选项值一行", "changed/only_project/only_mine", "changes 含 type"])
def _():
    # 项目侧：基建 1 / 技改 2 / 检修 3；我方：基建 01 / 技改 02 / 大修 04
    # → 基建、技改「码不同」，检修「项目新增」，大修「将被替换」。
    # 我方是「字符串 + 手填候选值」：用户常见填法，也是差异表里最典型的
    # 「类型 字符串 → 选项」那一行（选项值差异 + 类型差异同时出现）
    data = sheets([field("contract", "项目类别", "字符串",
                         **{"字段编码": "projCategory", "选项值": "基建,技改,大修"})],
                  bos=[bo_row(关联项目="是")])
    proj, errors = project_of(data)
    assert not errors, errors
    bo = proj.bo("contract")
    own = bo.field_by_code("projCategory")
    # 确认版 Excel / 存量包会把业务码带回来，这里手填模拟
    own.dict_items = [{"key": "01", "name": "基建", "sn": 1},
                      {"key": "02", "name": "技改", "sn": 2},
                      {"key": "04", "name": "大修", "sn": 3}]
    bo.project_link = True
    snap = _dic_snapshot()
    vm = projlink.analyze(proj, snap)
    diff = next(d for d in vm["diffs"] if d["fieldCode"] == "projCategory")
    assert diff["targetCode"] == "projectType", diff
    # 每个选项值一行，状态可分辨
    states = [(o["name"], o["state"]) for o in diff["options"]]
    assert states == [("基建", "changed"), ("技改", "changed"),
                      ("检修", "only_project"), ("大修", "only_mine")], states
    base = {o["name"]: o for o in diff["options"]}
    assert (base["基建"]["mineKey"], base["基建"]["projectKey"]) == ("01", "1"), base["基建"]
    assert base["检修"]["mineKey"] is None and base["检修"]["projectKey"] == "3", base["检修"]
    assert base["大修"]["projectKey"] is None and base["大修"]["mineKey"] == "04", base["大修"]
    # 「类型」这一行必须在（字符串 → 选项），否则用户以为只是换了几个候选值
    type_row = next(c for c in diff["changes"] if c["key"] == "type")
    assert (type_row["mine"], type_row["project"]) == ("字符串", "选项"), type_row

    # 未确认时**不动模型**（差异只是给用户看的）
    projlink.apply(proj, None, snap=snap)
    assert proj.bo("contract").field_by_code("projectType") is None, \
        [f.code for f in proj.bo("contract").fields]
    # 勾了「按项目覆盖」才落到模型上：编码/字典/候选值全换成项目侧
    projlink.apply(proj, {"version": 1, "objects": {"contract": {
        "align": {"projCategory": True}}}}, snap=snap)
    after = proj.bo("contract").field_by_code("projectType")
    assert after is not None, [f.code for f in proj.bo("contract").fields]
    assert after.dict_ref == "project_type", after.dict_ref
    assert [i["name"] for i in after.dict_items] == ["基建", "技改", "检修"], after.dict_items


@case("PL50", "projlink", "项目里没有的新字段：不覆盖、不进差异弹窗，按原规则生成",
      "P0", ["新字段不弹窗", "code/type 保留我方", "newFields 计数"])
def _():
    data = sheets([field("contract", "签署地点", "字符串", **{"字段编码": "signPlace"})],
                  bos=[bo_row(关联项目="是")])
    with using():
        res = webui.parse_excel(xlsx(data))
        body = build(data)
    vm = res["projectAlign"]
    item = vm["items"][0]
    skipped = {s["fieldCode"]: s["kind"] for s in item["skipped"]}
    assert skipped.get("signPlace") == "no_match", item["skipped"]
    assert "signPlace" not in {d["fieldCode"] for d in vm["diffs"]}, vm["diffs"]
    assert [x["fieldCode"] for x in vm["newFields"]] == ["signPlace"], vm["newFields"]
    assert vm["newFieldCount"] == 1, vm["newFieldCount"]
    # 我方定义原样保留（编码/类型都不被项目侧影响）
    fields = {f["code"]: f for f in res["objects"][0]["fields"]}
    assert fields["signPlace"]["type"] == "varchar", fields["signPlace"]
    row = attr_row(pick(body, "bo/"), "signPlace")
    assert row["dataTypeNew"] == "varchar", row
    # 链接字段照旧补（勾了对象就该有「所属项目」）
    assert 'code="projectId"' in pick(body, "bo/")


@case("PL51", "projlink", "Excel 没有「所属项目」→ 只在勾了「关联项目」时自动补，并进 addedLinks",
      "P0", ["addedLinks 报备", "不勾就不补", "不重复补"])
def _():
    data = _linked_data()          # Excel 里没有「关联项目」列
    with using():
        linked = webui.parse_excel(xlsx(data), pl_header({"contract": {"linked": True}}))
        plain = webui.parse_excel(xlsx(data))
    # ① 勾了：addedLinks 1 条，产物里有 projectId
    vm = linked["projectAlign"]
    assert vm["addedLinkCount"] == 1, vm["addedLinks"]
    link = vm["addedLinks"][0]
    assert link == {"objectCode": "contract", "objectName": "合同",
                    "fieldName": "所属项目", "code": "projectId"}, link
    assert "projectId" in {f["code"] for f in linked["objects"][0]["fields"]}
    # ② 没勾：一条都不报，产物里也没有
    assert plain["projectAlign"]["addedLinks"] == [], plain["projectAlign"]["addedLinks"]
    assert "projectId" not in {f["code"] for f in plain["objects"][0]["fields"]}
    # ③ 已经有链接字段（Excel 里就写了 projectId）→ 不再算「自动补」
    proj = linked
    assert proj is not None
    have = sheets([field("contract", "所属项目", "对象",
                         **{"字段编码": "projectId", "关联对象/字典": "project"})],
                  bos=[bo_row(关联项目="是")])
    with using():
        again = webui.parse_excel(xlsx(have))
    assert again["projectAlign"]["addedLinks"] == [], again["projectAlign"]["addedLinks"]


@case("PL52", "projlink", "相近字段确认弹窗：三组分开列、默认不勾、全选/全不选、只发勾上的",
      "P0", ["node 探针真跑 projlink.js", "默认保留我方", "collect 只发 true"])
def _():
    import json as _json
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        skip("没装 node：确认弹窗探针跳过（后端 PL48/PL53-PL58 仍然生效）")
    static = Path(webui.STATIC)
    probe = Path(__file__).resolve().parent / "fe_pl_diff_probe.js"
    assert probe.is_file(), f"缺少前端探针 {probe}"
    proc = subprocess.run(
        [node, str(probe), str(static / "projlink.js"), str(static / "index.html")],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"node 探针失败：\n{proc.stderr[-1200:]}"
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    assert lines, "node 探针没有输出"
    got = _json.loads(lines[-1])
    # ① 弹窗标题与摘要：一眼看出「自动覆盖几个、要你确认几个、无关几个」
    assert got["M_opened"] is True, got
    assert "要按项目定义覆盖吗" in got["M_title"], got["M_title"]
    assert got["M_titleOk"] and got["M_summary"], got
    # ② 三组分开（语义相近 / 可能相关 / 需确认），自动的行不列
    assert all(got["M_groups"]), got["M_groups"]
    assert all(got["M_groupKeys"]), got["M_groupKeys"]
    assert got["M_hidesAuto"] is True, "没有差异的自动行也列进弹窗了：等于又让用户选一遍"
    # ②a 只有直接对应（E0）有差异、没有待确认项：也要弹，只读展示改动，不给勾
    assert got["A_direct"] == 1, got
    assert got["A_opened"] is True, "只有 E0 差异时没弹：用户看不到不一致"
    assert got["A_autoGroup"] and got["A_autoField"], got
    assert got["A_autoNoPick"] is True, "E0 自动覆盖行给了勾选框"
    assert "已自动对齐" in got["A_title"], got["A_title"]
    assert got["A_primaryDisabled"] is True, "没有待确认项时「按勾选覆盖」还能点"
    # ②c 短名「预算」：pending 但有候选（总预算）也要给下拉，选了发编码
    assert got["C_opened"] is True, got
    assert got["C_select"] is True, "预算/总预算没有候选下拉"
    assert got["C_pick"] == "totalBudget", got
    assert got["C_collect"] == {"contract": {"yusuan": "totalBudget"}}, got
    assert got["C_isPicked"] is True, got
    # ②b 无关字段：默认收起，但**能展开看到每一个字段名**（用户问的就是「哪些字段无关」）
    assert got["M_hasUnrelatedToggle"] is True, "无关字段没有可展开的清单"
    assert got["M_unrelatedNames"] is True, "展开后看不到具体字段名"
    assert got["M_unrelatedCollapsed"] is True, "无关清单默认就展开了：大对象会淹掉待确认项"
    # ③ 每行：你的字段 + 分类 chip + 项目字段 + 依据 + 差异摘要 + 附注
    assert got["M_rowFields"] and got["M_chip"], got
    assert got["M_basis"] and got["M_diff"], got
    assert got["M_note"] is True, "可疑快照没有提示"
    # ④ 多候选给下拉；冲突只说原因、不给勾（给了也覆盖不了）
    assert got["M_candSelect"] is True, got
    assert got["M_conflictLocked"] is True, got    # ⑤ **默认一个都不勾**（默认保留我方）
    assert got["M_defaultUnchecked"] is True, "勾选框默认是勾上的：等于又变成「一律覆盖」"
    assert got["M_pickBoxes"] == 4, got["M_pickBoxes"]
    assert got["M_hasFooterHint"] is True, got
    # ⑥ 全选只勾「能勾的行」（冲突行永不勾），collect 只发 true
    assert got["S_all"] == 4, got
    assert got["S_collect"] == {"contract": {"projCategory": True, "yuSuanJin": True,
                                             "projectName": True, "projectState": True}}, got
    assert got["S_none"] == 0 and got["S_collectAfterNone"] == {}, got
    assert got["S_noConflict"] is True, "冲突行被勾上了"
    # ⑦ 页签：关系表（字段/状态/项目字段/依据/处理）+ 差异卡，且页签里没有勾选框
    assert got["R_hasTable"] and got["R_headers"], got
    assert got["R_noCheckbox"] is True, got
    assert got["R_autoShown"] is True and got["R_summaryLine"] is True, got
    assert got["R_unrelatedToggle"] is True, "页签里无关字段只有数字，没有可展开的清单"
    assert got["R_unrelatedNames"] is True, "页签展开后看不到具体字段名"
    assert got["R_unrelatedCollapsed"] is True, got
    assert got["R_badge"] == "5", got["R_badge"]
    # ⑧ 没有待确认项（纯 E0 + 无关）→ 不弹
    assert got["Z_pending"] == 0 and got["Z_rows"] == 0, got
    assert got["Z_notOpened"] is True, "没有待确认项还是弹了遮罩"
    assert got["Z_tabStillRenders"] is True, got
    # ⑨ 底部四个出口：全选 / 全不选 / 保留我的 / 按勾选覆盖
    assert got["B_pickAll"] and got["B_pickNone"], got
    assert got["B_confirm"] is True, "弹窗没有「按勾选覆盖」按钮"
    assert got["B_keepMine"] is True, "弹窗没有「保留我的」按钮"
    assert got["B_noRejectAll"] is True, got
    assert got["B_step2"] is True and got["B_title"] is True, got


# ---------------------------------------------------------------- PL53–PL58
# 2026-11-15 口径的六条铁律：直接对应自动、其余待确认、可疑/跨族也待确认、
# 无关不弹、多候选不猜、冲突保留我方。每条都断言**真实产物**（预览 + 包里的
# attribute），不只看中间变量。


def _mix_snapshot() -> projlink.Snapshot:
    """四种关系各一条的最小快照：E0 同族 / S2 编辑距离 / S1 包含 / 无关。"""
    fields = [
        _pf("code", "项目编号", "varchar", 64, is_index=True, sn=1),
        _pf("budget", "预算金额", "number", 22, sn=2, precision=2),
        _pf("planStart", "计划开始时间", "datetime", None, sn=3),
        _pf("notes", "备注", "clob", None, sn=4),
        _pf("name", "项目名称", "varchar", 200, sn=5),
    ]
    return projlink.Snapshot(
        source="target_db", generated_at="2026-11-15T09:00:00+08:00",
        project={"code": "project", "name": "项目", "tbl": "pp_project"},
        fields=fields, system_codes=set(projlink.SYSTEM_CODES),
        warnings=[], declared_total=len(fields))


@case("PL53", "projlink", "E0 自动覆盖；S2/S1 只是「可能相关」，默认保留我方、勾了才覆盖",
      "P0", ["E0 auto", "S2 pending", "S1 pending", "align=true 才落地"])
def _():
    data = sheets([
        field("contract", "项目编号", "字符串"),                       # E0
        field("contract", "预算金", "数字", **{"字段编码": "yuSuanJin"}),   # S2
        field("contract", "备注说明", "字符串", **{"字段编码": "beiZhu"}),  # S1
        field("contract", "签署地点", "字符串", **{"字段编码": "signPlace"}),
    ], bos=[bo_row(关联项目="是")])
    snap = _mix_snapshot()
    proj, errors = project_of(data)
    assert not errors, errors
    vm = projlink.analyze(proj, snap)
    item = vm["items"][0]
    rel = {r["fieldName"]: r for r in item["relations"]}

    # ① 直接对应（E0 同族唯一）→ 自动
    assert rel["项目编号"]["status"] == "auto", rel["项目编号"]
    assert rel["项目编号"]["rule"] == "E0", rel["项目编号"]
    assert rel["项目编号"]["action"] == "已自动覆盖", rel["项目编号"]
    # ② S2「预算金 ↔ 预算金额」：只有一个字不同 → 可能相关，不自动
    assert rel["预算金"]["rule"] == "S2", rel["预算金"]
    assert rel["预算金"]["status"] == "pending", rel["预算金"]
    assert rel["预算金"]["confidence"] == "medium", rel["预算金"]
    assert "只差 1 个字" in rel["预算金"]["basis"], rel["预算金"]["basis"]
    # ③ S1「备注说明 ↔ 备注」：包含关系 → 也不自动，且通用字段名要提示
    assert rel["备注说明"]["rule"] == "S1", rel["备注说明"]
    assert rel["备注说明"]["status"] == "pending", rel["备注说明"]
    # ④ 无关字段 → 按原规则生成
    assert rel["签署地点"]["status"] == "unrelated", rel["签署地点"]
    assert rel["签署地点"]["action"] == "按原规则生成", rel["签署地点"]
    assert item["pendingCount"] == 2, item["relationSummary"]

    # ⑤ 产物：只有 E0 那条被覆盖，两条 pending 原样保留
    with using(snap):
        plain = build(sheets([
            field("contract", "项目编号", "字符串"),
            field("contract", "预算金", "数字", **{"字段编码": "yuSuanJin"}),
            field("contract", "备注说明", "字符串", **{"字段编码": "beiZhu"}),
        ], bos=[bo_row(关联项目="是")]))
        picked = webui.build_zip(
            xlsx(sheets([
                field("contract", "项目编号", "字符串"),
                field("contract", "预算金", "数字", **{"字段编码": "yuSuanJin"}),
                field("contract", "备注说明", "字符串", **{"字段编码": "beiZhu"}),
            ], bos=[bo_row(关联项目="是")])),
            projlink_header=pl_header(
                {"contract": {"align": {"yuSuanJin": True}}}))[0]
    bo_text = pick(plain, "bo/")
    codes = {attrs(a).get("code") for a in all_tags(bo_text, "attribute")}
    assert "code" in codes, codes                      # E0 已覆盖
    assert "yuSuanJin" in codes, codes                 # S2 保留我方
    assert "beiZhu" in codes, codes                    # S1 保留我方
    assert "budget" not in codes and "notes" not in codes, codes
    # ⑥ 勾了「按项目覆盖」的那条真的覆盖了（另一条仍保留）
    picked_codes = {attrs(a).get("code")
                    for a in all_tags(pick(picked, "bo/"), "attribute")}
    assert "budget" in picked_codes, picked_codes
    assert "beiZhu" in picked_codes, picked_codes
    assert attr_row(pick(picked, "bo/"), "budget")["length"] == "22"


@case("PL54", "projlink", "E0 但可疑（项目名称）/ 跨族（字符串→日期）都不自动，进「需确认」",
      "P0", ["suspect → pending", "跨族 → pending", "同族才自动"])
def _():
    data = sheets([
        field("contract", "项目名称", "字符串", **{"字段编码": "projectName"}),
        field("contract", "计划开始时间", "字符串", **{"字段编码": "kaiShi"}),
    ], bos=[bo_row(关联项目="是")])
    snap = _mix_snapshot()
    proj, errors = project_of(data)
    assert not errors, errors
    vm = projlink.analyze(proj, snap)
    rel = {r["fieldName"]: r for r in vm["items"][0]["relations"]}
    # 可疑名单：同名但可能只是抄了一份项目名 → 不自动
    assert rel["项目名称"]["rule"] == "E0", rel["项目名称"]
    assert rel["项目名称"]["status"] == "pending", rel["项目名称"]
    assert "可疑" in rel["项目名称"]["note"], rel["项目名称"]["note"]
    assert rel["项目名称"]["confidence"] == "high", rel["项目名称"]
    # 跨族：字符串 → 日期，覆盖会改物理类型 → 不自动
    assert rel["计划开始时间"]["rule"] == "E0", rel["计划开始时间"]
    assert rel["计划开始时间"]["status"] == "pending", rel["计划开始时间"]
    assert "跨族" in rel["计划开始时间"]["note"], rel["计划开始时间"]["note"]
    # 两条都不在 aligned 里（预览与产物都不改）
    with using(snap):
        res = webui.parse_excel(xlsx(data))
        body = build(data)
    item = res["projectAlign"]["items"][0]
    assert not item["exact"] and not item["similar"], item
    assert item["pendingCount"] == 2, item["relationSummary"]
    codes = {attrs(a).get("code") for a in all_tags(pick(body, "bo/"), "attribute")}
    assert "planStart" not in codes, codes
    assert "projectName" in codes, codes      # 我方定义原样保留


@case("PL55", "projlink", "项目里没有的字段 → unrelated：不覆盖、不算待确认、不弹窗",
      "P0", ["unrelated 不弹", "按原规则生成", "summary 计数"])
def _():
    data = sheets([field("contract", "签署地点", "字符串", **{"字段编码": "signPlace"})],
                  bos=[bo_row(关联项目="是")])
    snap = _mix_snapshot()
    proj, errors = project_of(data)
    assert not errors, errors
    vm = projlink.analyze(proj, snap)
    item = vm["items"][0]
    rel = item["relations"][0]
    assert rel["status"] == "unrelated", rel
    assert rel["target"] is None and rel["rule"] == "", rel
    # 不弹窗的判据：pendingCount / needsConfirm 都是 0
    assert item["pendingCount"] == 0, item["relationSummary"]
    assert vm["pendingCount"] == 0, vm["relationSummary"]
    assert vm["relationSummary"]["unrelated"] == 1, vm["relationSummary"]
    assert [f["fieldCode"] for f in vm["newFields"]] == ["signPlace"], vm["newFields"]
    # 我方定义原样保留
    with using(snap):
        body = build(data)
    assert attr_row(pick(body, "bo/"), "signPlace")["dataTypeNew"] == "varchar"


@case("PL56", "projlink", "同一字段撞上两个同分候选 → ambiguous：不猜、列候选、不应用",
      "P0", ["多候选不自动", "candidates ≥2", "等用户挑"])
def _():
    # 项目侧真有两个都叫「项目状态」的字段（历史遗留），谁也不比谁更对
    snap = projlink.Snapshot(
        source="target_db", generated_at="2026-11-15T09:00:00+08:00",
        project={"code": "project", "name": "项目", "tbl": "pp_project"},
        fields=[_pf("stateId", "项目状态", "object", 64, "project_state", sn=1),
                _pf("stateOldId", "项目状态", "object", 64, "project_state", sn=2)],
        system_codes=set(projlink.SYSTEM_CODES), warnings=[], declared_total=2)
    data = sheets([field("contract", "项目状态", "对象",
                         **{"关联对象/字典": "project_state"})],
                  bos=[bo_row(关联项目="是")])
    proj, errors = project_of(data)
    assert not errors, errors
    vm = projlink.analyze(proj, snap)
    rel = vm["items"][0]["relations"][0]
    assert rel["status"] == "ambiguous", rel
    assert rel["rule"] == "E0", rel
    assert len(rel["candidates"]) >= 1, rel
    assert {rel["target"]["code"], *(c["code"] for c in rel["candidates"])} == \
        {"stateId", "stateOldId"}, rel
    assert rel["confidence"] == "low", rel
    # 不应用：模型里还是我方的字段
    projlink.apply(proj, None, snap=snap)
    assert proj.bo("contract").field_by_code("stateId") is None
    assert proj.bo("contract").field_by_code("stateOldId") is None
    assert proj.bo("contract").field_by_code("xiangMuZhuangTai") is not None


@case("PL57", "projlink", "两个字段抢同一个项目字段 → 第二个 conflict：保留我方，勾了也不覆盖",
      "P0", ["target_taken", "冲突不给覆盖", "编码不重复"])
def _():
    data = sheets([
        field("contract", "项目状态", "对象", **{"字段编码": "projectStateA",
                                                 "关联对象/字典": "project_state"}),
        field("contract", "项目 状态", "对象", **{"字段编码": "projectStateB",
                                                  "关联对象/字典": "project_state"}),
    ], bos=[bo_row(关联项目="是")])
    proj, errors = project_of(data)
    assert not errors, errors
    snap = fake_snapshot()
    vm = projlink.analyze(proj, snap)
    item = vm["items"][0]
    rel = {r["fieldCode"]: r for r in item["relations"]}
    assert rel["projectStateA"]["status"] == "auto", rel["projectStateA"]
    assert rel["projectStateB"]["status"] == "conflict", rel["projectStateB"]
    assert rel["projectStateB"]["action"] == "保留我方", rel["projectStateB"]
    assert rel["projectStateB"]["note"], rel["projectStateB"]
    assert item["conflicts"] and item["conflicts"][0]["kind"] == "target_taken"
    # 冲突行即使被勾了「按项目覆盖」也不应用（撞车）
    report = projlink.apply(proj, {"version": 1, "objects": {"contract": {
        "align": {"projectStateB": True}}}}, snap=snap)
    bo = proj.bo("contract")
    assert bo.field_by_code("projectStateB") is not None, [f.code for f in bo.fields]
    codes = [f.code for f in bo.fields]
    assert len(codes) == len(set(codes)), codes
    assert [c["kind"] for c in report["conflicts"]] == ["target_taken"], report["conflicts"]


@case("PL58", "projlink", "关系表：每个字段一行、依据说人话、summary 按状态计数",
      "P0", ["relations 每字段一行", "basis 人话", "relationSummary"])
def _():
    data = sheets([
        field("contract", "项目编号", "字符串"),                              # auto
        field("contract", "项目类别", "选项", **{"字段编码": "projCategory",
                                                 "选项值": "基建"}),         # S4 pending
        field("contract", "签署地点", "字符串", **{"字段编码": "signPlace"}),  # unrelated
        field("contract", "名称", "字符串"),                                  # unknown（本对象名称）
    ], bos=[bo_row(关联项目="是")])
    proj, errors = project_of(data)
    assert not errors, errors
    vm = projlink.analyze(proj, fake_snapshot())
    item = vm["items"][0]
    # ① 每个字段一行（4 个业务字段 → 4 行），顺序与 Excel 一致
    assert [r["fieldName"] for r in item["relations"]] == \
        ["项目编号", "项目类别", "签署地点", "名称"], item["relations"]
    for r in item["relations"]:
        assert set(("fieldCode", "fieldName", "status", "rule", "basis", "confidence",
                    "target", "candidates", "diff", "action", "note")) <= set(r), r
    # ② 依据是人话，不是规则码
    rel = {r["fieldName"]: r for r in item["relations"]}
    assert rel["项目编号"]["basis"] == "字段名完全一致", rel["项目编号"]["basis"]
    assert rel["项目类别"]["basis"].startswith("同义词"), rel["项目类别"]["basis"]
    assert rel["签署地点"]["basis"] == "", rel["签署地点"]
    assert rel["名称"]["status"] == "unknown", rel["名称"]
    assert "不参与" in rel["名称"]["action"], rel["名称"]
    # ③ 汇总计数（对象内 + 全局扁平）
    rs = item["relationSummary"]
    assert (rs["total"], rs["auto"], rs["pending"], rs["unrelated"], rs["unknown"]) == \
        (4, 1, 1, 1, 1), rs
    assert rs["needsConfirm"] == 1, rs
    assert vm["relationSummary"]["total"] == 4, vm["relationSummary"]
    flat = {(r["fieldCode"], r["status"]) for r in vm["relations"]}
    assert ("xiangMuBianHao", "auto") in flat and ("signPlace", "unrelated") in flat, flat
    assert vm["pendingCount"] == 1, vm["pendingCount"]
    assert vm["summary"]["auto"] == 1 and vm["summary"]["pending"] == 1, vm["summary"]


@case("PL59", "projlink", "多候选可以在 align 里写项目字段编码：按指定的那个覆盖，不在候选里就忽略",
      "P0", ["align 支持 targetCode", "换指后重算差异", "坏候选只忽略不报错"])
def _():
    # 多候选时「最匹配」只是排序第一，可能不是用户要的。前端下拉选中的项目字段
    # 编码会原样进 align；服务端只当**选择**用（规则/差异自己重算），并校验它
    # 确实是本字段的候选之一。
    snap = projlink.Snapshot(
        source="target_db", generated_at="2026-11-15T09:00:00+08:00",
        project={"code": "project", "name": "项目", "tbl": "pp_project"},
        fields=[_pf("stateId", "项目状态", "object", 64, "project_state", sn=1),
                _pf("stateOldId", "项目状态", "object", 64, "project_state", sn=2)],
        system_codes=set(projlink.SYSTEM_CODES), warnings=[], declared_total=2)
    data = sheets([field("contract", "项目状态", "对象",
                         **{"关联对象/字典": "project_state"})],
                  bos=[bo_row(关联项目="是")])

    def run(align_value):
        proj, errors = project_of(data)
        assert not errors, errors
        dec = {"version": 1, "objects": {"contract": {
            "linked": True, "align": {"xiangMuZhuangTai": align_value}}}}
        vm = projlink.analyze(proj, snap, dec)
        rel = vm["items"][0]["relations"][0]
        projlink.apply(proj, dec, snap=snap)
        codes = [f.code for f in proj.bo("contract").fields]
        return rel, codes, vm

    # ① 不勾：两条都留着候选，不应用
    rel0, codes0, _ = run(False)
    assert rel0["status"] == "pending" or rel0["status"] == "ambiguous", rel0
    assert "xiangMuZhuangTai" in codes0, codes0
    # ② true：按最匹配（sn 小的 stateId）覆盖
    rel1, codes1, _ = run(True)
    assert rel1["status"] == "applied" and rel1["target"]["code"] == "stateId", rel1
    assert "stateId" in codes1 and "xiangMuZhuangTai" not in codes1, codes1
    # ③ 指定编码：换成 stateOldId（覆盖目标、差异、依据都要跟着换）
    rel2, codes2, vm2 = run("stateOldId")
    assert rel2["status"] == "applied", rel2
    assert rel2["target"]["code"] == "stateOldId", rel2
    assert "指定" in rel2["note"] or "stateOldId" in rel2["note"], rel2["note"]
    assert "stateOldId" in codes2 and "stateId" not in codes2, codes2
    diff = next(d for d in vm2["diffs"] if d["fieldCode"] == "xiangMuZhuangTai")
    assert diff["targetCode"] == "stateOldId", diff["targetCode"]
    # ④ 指定一个不存在的编码：忽略（不 400、不乱覆盖），并留一条能看懂的 note
    rel3, codes3, vm3 = run("nope")
    assert rel3["status"] == "ambiguous", rel3
    assert "nope" in rel3["note"], rel3["note"]
    assert vm3["items"][0]["warnings"], "坏候选没有留下提示"
    assert "xiangMuZhuangTai" in codes3, codes3
    # ⑤ 换指会撞车时按新目标判冲突（不能绕过 target/code 占用检查）
    clash = sheets([field("contract", "项目状态", "对象",
                          **{"关联对象/字典": "project_state"}),
                    field("contract", "旧状态字段", "字符串",
                          **{"字段编码": "stateOldId"})],
                   bos=[bo_row(关联项目="是")])
    proj4, errors = project_of(clash)
    assert not errors, errors
    vm4 = projlink.analyze(proj4, snap, {"version": 1, "objects": {"contract": {
        "linked": True, "align": {"xiangMuZhuangTai": "stateOldId"}}}})
    rel4 = {r["fieldCode"]: r for r in vm4["items"][0]["relations"]}
    assert rel4["xiangMuZhuangTai"]["status"] == "conflict", rel4["xiangMuZhuangTai"]
    assert [c["kind"] for c in vm4["items"][0]["conflicts"]] == ["code_taken"], \
        vm4["items"][0]["conflicts"]

    # ⑥ 预览=zip：同一条决策走真实出包路径，包里也必须是 stateOldId
    hdr = __import__("json").dumps({"version": 1, "objects": {"contract": {
        "linked": True, "align": {"xiangMuZhuangTai": "stateOldId"}}}})
    with using(snap):
        preview = webui.parse_excel(xlsx(data), hdr)
        body = webui.build_zip(xlsx(data), projlink_header=hdr)[0]
    assert "stateOldId" in {f["code"] for f in preview["objects"][0]["fields"]}, \
        [f["code"] for f in preview["objects"][0]["fields"]]
    bo_text = pick(body, "bo/")
    assert 'code="stateOldId"' in bo_text, [a["code"] for a in all_tags(bo_text, "attribute")]
    assert 'code="stateId"' not in bo_text


@case("PL60", "projlink", "决策头校验：align 只收 true/false/项目字段编码，空串和数字都 400",
      "P1", ["不信任前端", "坏值不静默", "文案可读"])
def _():
    import json as _json

    ok = projlink.parse_decisions(_json.dumps({"version": 1, "objects": {
        "contract": {"linked": True, "align": {"a": True, "b": "stateOldId", "c": False}}}}))
    assert ok["objects"]["contract"]["align"] == {"a": True, "b": "stateOldId",
                                                 "c": False}, ok
    for bad in ("", 1, None, [], {}):
        raw = _json.dumps({"version": 1, "objects": {
            "contract": {"linked": True, "align": {"a": bad}}}})
        try:
            projlink.parse_decisions(raw)
        except ValueError as exc:
            assert "true/false" in str(exc), str(exc)
        else:
            raise AssertionError(f"align 值 {bad!r} 应当被拒绝")

@case("PL61", "projlink", "多候选指定另一个项目字段（align:\"gradeId\"）：应用的是 gradeId，不是最匹配的 stateId",
      "P0", ["指定候选真的生效", "不是最匹配", "预览=zip"])
def _():
    # 用户最反感的就是「强制二选一」：下拉里选哪个，就必须按哪个覆盖。
    # 这里把两个候选换成 stateId / gradeId（线上真会出现同名不同含义的字段）。
    snap = projlink.Snapshot(
        source="target_db", generated_at="2026-11-15T09:00:00+08:00",
        project={"code": "project", "name": "项目", "tbl": "pp_project"},
        fields=[_pf("stateId", "项目状态", "object", 64, "project_state", sn=1),
                _pf("gradeId", "项目状态", "object", 64, "project_grade", sn=2)],
        system_codes=set(projlink.SYSTEM_CODES), warnings=[], declared_total=2)
    data = sheets([field("contract", "项目状态", "对象",
                         **{"关联对象/字典": "project_state"})],
                  bos=[bo_row(关联项目="是")])
    proj, errors = project_of(data)
    assert not errors, errors
    dec = {"version": 1, "objects": {"contract": {
        "linked": True, "align": {"xiangMuZhuangTai": "gradeId"}}}}
    vm = projlink.analyze(proj, snap, dec)
    rel = vm["items"][0]["relations"][0]
    assert rel["status"] == "applied", rel
    assert rel["target"]["code"] == "gradeId", rel["target"]
    assert rel["target"]["code"] != "stateId", rel["target"]
    assert "gradeId" in rel["note"], rel["note"]
    report = projlink.apply(proj, dec, snap=snap)
    assert [r["newCode"] for r in report["aligned"]] == ["gradeId"], report["aligned"]
    assert proj.bo("contract").field_by_code("gradeId") is not None
    assert proj.bo("contract").field_by_code("stateId") is None
    # 出包路径同源：包里的 objectType 是 project_grade（跟着 gradeId 走）
    with using(snap):
        body = webui.build_zip(xlsx(data), projlink_header=__import__("json").dumps(dec))[0]
    row = attr_row(pick(body, "bo/"), "gradeId")
    assert row["objectType"] == "project_grade", row
    # 不在候选里的编码一律忽略，并留一条能看懂的 note
    bad = {"version": 1, "objects": {"contract": {
        "linked": True, "align": {"xiangMuZhuangTai": "notACandidate"}}}}
    proj2, _ = project_of(data)
    vm2 = projlink.analyze(proj2, snap, bad)
    rel2 = vm2["items"][0]["relations"][0]
    assert rel2["status"] == "ambiguous", rel2
    assert "notACandidate" in rel2["note"], rel2["note"]
    projlink.apply(proj2, bad, snap=snap)
    assert proj2.bo("contract").field_by_code("xiangMuZhuangTai") is not None


@case("PL62", "projlink", "出包后回第 1 步改 Excel：同一个文件再选一次要能重新解析、重新弹确认",
      "P1", ["同一个文件", "file input reset", "返回修改 Excel"])
def _():
    # 用户实测：上传 → 出包 → 返回上一步 → 改 Excel → 再选同一个文件，没反应，
    # 以为是流程卡死。根因是 <input type=file> 的 value 没清空，同名文件再选
    # 不触发 change。这里同时锁住「返回第 1 步清空 + 允许再次弹确认」和弹窗里
    # 的「返回修改 Excel」入口，避免以后又有人把这段删掉。
    from pathlib import Path
    app = (Path(webui.STATIC) / "app.js").read_text(encoding="utf-8")
    html = (Path(webui.STATIC) / "index.html").read_text(encoding="utf-8")
    assert re.search(r'fileInput"\)\.onchange\s*=\s*\(e\)\s*=>\s*\{[\s\S]{0,300}?'
                     r'e\.target\.value = ""', app), "fileInput 选择后没有清空 value"
    m = re.search(r'function backToInput\(\)\s*\{[\s\S]{0,700}?\n\}', app)
    assert m, "backToInput 不见了"
    body = m.group(0)
    assert "fileInput" in body, "返回第 1 步没有清空文件选择框"
    assert "plModalShown = false" in body, "返回第 1 步没有允许再次弹确认"
    assert 'id="plBackEdit"' in html, "确认弹窗没有「返回修改 Excel」入口"
    assert "plBackEdit" in app and "backToInput" in app, "返回修改 Excel 没有绑到第 1 步"


@case("PL63", "projlink", "AI 首屏返回后再点「开始生成」：走全新生成并进第 2 步，不能当增量修改",
      "P0", ["返回后能再进第 2 步", "fresh 生成", "/api/ai/bo 不是 refine"])
def _():
    # 用户实测：第 1 步「描述 → 开始生成」进第 2 步，点「回去接着说」改描述，
    # 再点开始生成就停在第 1 步。根因：sendAiChat 只看 state.aiData 判 first，
    # 旧结果还在 → 走 /api/ai/refine，而 refine 不 goStep(2)。
    # 修法：首屏发送显式带 fresh=true，first = fresh || !state.aiData。
    from pathlib import Path
    app = (Path(webui.STATIC) / "app.js").read_text(encoding="utf-8")
    assert re.search(r"async function sendAiChat\(text,\s*\{\s*fresh = false\s*\}\s*=\s*\{\}\)",
                     app), "sendAiChat 没有 fresh 参数"
    assert "const first = !!fresh || !state.aiData" in app, "first 仍然只看 aiData"
    assert "sendAiChat(msg, { fresh })" in app, "aiChatSendFrom 没把 fresh 传下去"
    # 首屏三个入口（按钮 / Enter / 快捷 chip）都必须 fresh
    assert 'aiChatSendFrom("aiChatText1", "aiChatLog1", { fresh: true })' in app, \
        "首屏发送没有标 fresh"
    # 预览页仍是增量修改，不能误改成全新生成
    assert 'aiChatSendFrom("aiChatText", "aiChatLog")' in app, "预览页发送被改坏了"
    assert 'btn.textContent = first ? "开始生成" : "发送修改"' in app, \
        "按钮文案仍按 state.aiData 判断，回到第 1 步会显示「发送修改」"


@case("PL64", "projlink", "预算 → 预算状态/总预算：两个候选都列出来，手选总预算能落",
      "P0", ["预算状态是最匹配", "总预算也在候选里", "选总预算按 C 规则应用"])
def _():
    # 用户口径（2026-11-15）：短名「预算」在项目里有「预算状态」「总预算」时，
    # 不要只给一个最匹配，要把相关的都列出来让他选。
    snap = projlink.Snapshot(
        source="target_db", generated_at="2026-11-15T09:00:00+08:00",
        project={"code": "project", "name": "项目", "tbl": "pp_project"},
        fields=[_pf("budgetStatus", "预算状态", "varchar", 64, sn=1),
                _pf("totalBudget", "总预算", "number", 20, sn=2),
                _pf("name", "项目名称", "varchar", 200, sn=3)],
        system_codes=set(projlink.SYSTEM_CODES), warnings=[], declared_total=3)
    data = sheets([field("contract", "预算", "字符串", 字段编码="yusuan")],
                  bos=[bo_row(关联项目="是")])
    proj, errors = project_of(data)
    assert not errors, errors
    # 默认：最匹配是「预算状态」（S1），「总预算」作为候选列出，不自动选
    vm = projlink.analyze(proj, snap, {"version": 1, "objects": {
        "contract": {"linked": True}}})
    rel = vm["items"][0]["relations"][0]
    assert rel["status"] == "pending", rel
    assert rel["target"]["code"] == "budgetStatus", rel["target"]
    cands = {c["code"] for c in rel["candidates"]}
    assert "totalBudget" in cands, rel["candidates"]
    # 手选总预算：similar() 对「预算/总预算」是 None（PL04 的金额/总金额规则），
    # 但用户明确选了，就得按 C（手动指定）落，不能忽略
    dec = {"version": 1, "objects": {"contract": {
        "linked": True, "align": {"yusuan": "totalBudget"}}}}
    vm2 = projlink.analyze(proj, snap, dec)
    rel2 = vm2["items"][0]["relations"][0]
    assert rel2["status"] == "applied", rel2
    assert rel2["target"]["code"] == "totalBudget", rel2["target"]
    assert rel2["rule"] == "C", rel2
    assert "手动指定" in rel2["basis"], rel2["basis"]
    assert "totalBudget" in (rel2["note"] or ""), rel2["note"]
    report = projlink.apply(proj, dec, snap=snap)
    assert [r["newCode"] for r in report["aligned"]] == ["totalBudget"], report["aligned"]


@case("PL65", "projlink", "分析完先弹确认，确认/保留后才渲染预览（不再先渲染再盖遮罩）",
      "P0", ["handleFile 不提前 render/goStep3", "先弹确认", "finish 后才出结果页"])
def _():
    import json as _json
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        skip("没装 node：延迟渲染探针跳过")
    static = Path(webui.STATIC)
    probe = Path(__file__).resolve().parent / "fe_pl_pre_probe.js"
    proc = subprocess.run(
        [node, str(probe), str(static / "app.js"), str(static / "index.html")],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"node 探针失败：\n{proc.stderr[-1200:]}"
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    got = _json.loads(lines[-1])
    assert got["0_static"]["importBarOnTop"] is True, \
        "普通模式的「关联项目」选项不在 AI 卡上方：会被长卡片挤到折叠线下，用户看不见"
    # 三个页面（普通 / 流程 / 存量包）的选项栏必须同一套位置、标题、选项文案
    assert got["0_static"]["threeBars"] is True, "三个页面没有同款选项栏"
    assert got["0_static"]["barsAtTop"] is True, \
        "有页面的选项栏不在导入卡片上方（普通/流程/存量包要一致）"
    assert got["0_static"]["sameTitle"] is True, "三个页面的标题不一致"
    assert got["0_static"]["sameHint"] is True, "三个页面的说明文案不一致"
    assert got["0_static"]["patchAllLabel"] is True, \
        "存量包页的选项没有和普通/流程统一成「关联」"
    assert got["0_static"]["patchOffLabel"] is True, \
        "存量包页的选项没有和普通/流程统一成「不关联」"
    assert got["0_static"]["noAllPrefix"] is True, \
        "选项标签又加了「全部」：控件本身就是对整份 Excel 的批量设置，不需要重复"
    assert got["0_static"]["btnPreviewLabel"] is True, \
        "AI 预览默认（不关联）不是「生成预览」：用户会以为没关联还要核对字段"
    assert got["0_static"]["btnCheckDynamic"] is True, \
        "按钮没有按 state.plPre 在「生成预览 / 核对字段」之间切换"
    assert got["0_static"]["btnToPackNoGenerate"] is True, \
        "AI 预览仍出现「下一步：生成导入包」"
    assert got["0_static"]["btnPackLabel"] is True, \
        "结果页的「下载导入包 zip」被改坏了（真正的生成入口）"
    assert got["0_static"]["aiSheetAcceptsDoc"] is True, \
        "AI 文件入口的 accept 里没有 .pdf/.docx"
    assert got["0_static"]["aiDocEntry"] is True, \
        "前端没有接 /api/extract 或桌面 pick_source"
    # 返回按钮位置统一：第 2/3 步都在页头 .file-row，底部操作条不再有返回
    assert got["0_static"]["backButtonsTop"] is True, \
        "第 2 步或第 3 步的返回按钮不在页头 .file-row 里（一上一下，用户来回找）"
    assert got["0_static"]["backBeforeAiResult"] is True, \
        "第 2 步的返回按钮跑到 AI 结果区下面去了，应该在页头"
    assert got["0_static"]["backLabelUnified"] is True, \
        "两个返回按钮的文案没统一成「← 返回上一步」"
    assert got["0_static"]["noOldBackLabel"] is True, \
        "旧文案「← 回去接着说」还在"
    assert got["0_static"]["stepFootKeepsTwo"] is True, \
        "第 2 步底部操作条丢了「导出 Excel 线下改」或「下一步：生成预览」"
    d = got["5b_defer"]
    assert d["deferred"] is True, d
    assert d["shown"] is True, d
    assert d["steps"] == [], f"分析完直接 goStep 了：{d['steps']}"
    assert d["opened"] == [True], f"分析完没先弹确认：{d['opened']}"
    assert d["renders"] == 0, f"分析完先 renderAll 了：{d['renders']}"
    f = got["5b_finish"]
    assert f["finished"] is True, f
    assert f["steps"] == [3], f
    assert f["renders"] == 1, f
    assert f["deferred"] is False, f
