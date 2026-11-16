"""表格字段（子表）支持：2.1_表格列 → options.columns → tableField 控件。

依据的真实样本（2026-11-15 逐键核对）：
  PP6/业务对象/项目立项业务对象.zip        planExcel / budgetExcel
  PP6/流程/config-sync-20261113150255.zip  planExcel / budgetExcel / riskExcel
  form/002-project_create_detail_.xml      三个 tableField 控件的 columns/configurableColumns

守三件事：
  1. 对象侧结构照抄生产包（datatype=clob + options.columns，不建物理列）；
  2. 表单侧出自 tableField 且**直挂容器**（进 grid_layout 系统不渲染）；
  3. 列 id 稳定（同一份 Excel 反复出包一致），否则导入会被当成新列、已有数据丢。
"""
from __future__ import annotations

import html
import json

from pp6kit import spec, webui
from tests._harness import (
    all_tags,
    attr_of,
    attrs,
    case,
    issues_of,
    pick,
    project_of,
    read_xlsx_rows,
    texts,
    xlsx,
)

BO = "project_create"


def _sheets(cols=None, fields=None, formkind="标准全套", formfields=None,
            extra=None) -> dict:
    """一份含「表格字段」的最小填写表。"""
    col_rows = cols if cols is not None else [
        {"对象编码": BO, "字段编码": "planExcel", "序号": 1,
         "列名": "阶段", "列类型": "字符串（varchar）", "宽度%": 10, "对齐": "中"},
        {"对象编码": BO, "字段编码": "planExcel", "序号": 2,
         "列名": "计划开始时间", "列类型": "日期（date）"},
        {"对象编码": BO, "字段编码": "planExcel", "序号": 3,
         "列名": "金额", "列类型": "数字（number）", "精度": 2},
        {"对象编码": BO, "字段编码": "planExcel", "序号": 4,
         "列名": "是否里程碑", "列类型": "选项（dic）", "选项值": "是\n否"},
    ]
    sheets = {
        spec.S_BO: [{"对象名称": "项目立项", "对象编码": BO, "对象类型": "object"}],
        spec.S_FIELD: fields if fields is not None else [
            {"对象编码": BO, "字段编码": "name", "字段名称": "项目名称",
             "数据类型": "字符串（varchar）"},
            {"对象编码": BO, "字段编码": "planExcel", "字段名称": "里程碑计划表",
             "数据类型": "表格（table）"},
        ],
        spec.S_TABLE_COL: col_rows,
        spec.S_FORM: [{"表单key": f"create_{BO}", "表单名称": "新建项目立项",
                       "对象编码": BO, "表单类型": formkind}],
    }
    if formfields is not None:
        sheets[spec.S_FORMFIELD] = formfields
    if extra:
        sheets.update(extra)
    return sheets


def _bo_attr(xml: str, code: str) -> dict:
    frag = next(f for f in all_tags(xml, "attribute") if attrs(f).get("code") == code)
    return attrs(frag)


def _opt(xml: str, code: str) -> dict:
    return json.loads(html.unescape(_bo_attr(xml, code)["options"]))


def _zip(sheets: dict) -> bytes:
    body, _name = webui.build_zip(xlsx(sheets))
    return body


def _table_widget(form_xml: str, name: str) -> tuple[dict, dict]:
    """取表格控件的 (formField 属性, fieldOptions)。"""
    for frag in all_tags(form_xml, "formField"):
        a = attrs(frag)
        if a.get("name") == name and a.get("fieldType") == "tableField":
            return a, json.loads(html.unescape(a["fieldOptions"]))
    raise AssertionError(f"表单里没有 name={name} 的 tableField 控件")


@case("TBF01", "tablefield", "表格字段按生产包结构生成：clob + options.columns", "P0",
      ["2.1_表格列", "datatype=clob", "不建物理列"])
def _():
    z = _zip(_sheets())
    bo = pick(z, "bo/")
    a = _bo_attr(bo, "planExcel")
    assert a["dataTypeNew"] == "table", a
    assert a["datatype"] == "clob", "表格列按 JSON 存 clob，不该是 varchar"
    assert "length" not in a and "format" not in a, f"表格字段不该带长度/格式：{a}"
    opts = _opt(bo, "planExcel")
    # 键序/键集与 project_create 的 planExcel 完全一致
    assert list(opts) == ["isMultiple", "isDefault", "uniqueCheck", "optionSource",
                          "columns", "identities", "format", "formula"], list(opts)
    assert [c["name"] for c in opts["columns"]] == ["阶段", "计划开始时间", "金额", "是否里程碑"]
    assert opts["columns"][0]["inputType"] == "varchar"
    assert opts["columns"][0]["align"] == "center"
    assert opts["columns"][0]["widthPercent"] == 10


@case("TBF02", "tablefield", "数字列带精度、选项列带 selectOptions 与级联空键", "P1")
def _():
    bo = pick(_zip(_sheets()), "bo/")
    cols = {c["name"]: c for c in _opt(bo, "planExcel")["columns"]}
    assert cols["金额"]["inputType"] == "number" and cols["金额"]["precision"] == 2, cols["金额"]
    dic = cols["是否里程碑"]
    assert dic["optionSource"] == "BO_ATTR_DIC_TYPE"
    assert dic["objectType"] is None
    assert dic["multiple"] is False
    assert dic["cascadeEnabled"] is False and dic["cascadeParentCode"] == ""
    assert [o["value"] for o in dic["selectOptions"]] == ["是", "否"]
    assert "precision" not in cols["阶段"], "非数字列不该带 precision"


@case("TBF03", "tablefield", "列类型的中文/别名写法都能归一", "P1")
def _():
    cols = [
        {"对象编码": BO, "字段编码": "planExcel", "列名": "A", "列类型": "字符串"},
        {"对象编码": BO, "字段编码": "planExcel", "列名": "B", "列类型": "数字（number）"},
        {"对象编码": BO, "字段编码": "planExcel", "列名": "C", "列类型": "多行文本"},
        {"对象编码": BO, "字段编码": "planExcel", "列名": "D", "列类型": "user"},
        {"对象编码": BO, "字段编码": "planExcel", "列名": "E", "列类型": "日期"},
    ]
    proj, errors, _ = project_of(xlsx(_sheets(cols=cols)))
    assert not errors, errors
    got = {c.name: c.input_type for c in proj.bo(BO).field_by_code("planExcel").table_columns}
    assert got == {"A": "varchar", "B": "number", "C": "clob", "D": "user", "E": "date"}, got


@case("TBF04", "tablefield", "列挂到不存在的字段上报错，而不是静默丢弃", "P1")
def _():
    cols = [{"对象编码": BO, "字段编码": "buCunZai", "列名": "阶段", "列类型": "字符串"}]
    proj, errors, _ = project_of(xlsx(_sheets(cols=cols)))
    assert any("buCunZai" in e for e in errors), errors


@case("TBF05", "tablefield", "字段类型不是「表格」时写列要拦", "P1")
def _():
    cols = [{"对象编码": BO, "字段编码": "name", "列名": "阶段", "列类型": "字符串"}]
    proj, errors, _ = project_of(xlsx(_sheets(cols=cols)))
    assert any("不是「表格」" in e for e in errors), errors


@case("TBF06", "tablefield", "表格字段一列都没有 → check 报 error", "P0")
def _():
    proj, _errors, _ = project_of(xlsx(_sheets(cols=[])))
    errs, _warns, _infos = issues_of(proj)
    assert any("没有任何列" in e for e in errs), errs


@case("TBF07", "tablefield", "表单出 tableField 且直挂容器（不进 grid_layout）", "P0",
      ["布局", "parentId"])
def _():
    z = _zip(_sheets())
    form = pick(z, "form/00")
    fields = {attrs(f)["id"]: attrs(f) for f in all_tags(form, "formField")}
    wid, wopts = _table_widget(form, "planExcel")
    parent = fields.get(wid["parentId"])
    assert parent is not None, "表格控件没有父容器"
    assert parent["fieldType"] in ("card", "fieldset"), parent["fieldType"]
    # 容器里挂的就是控件本身，中间没有 grid_layout
    assert parent["fieldType"] != "grid_layout"
    owner = json.loads(html.unescape(parent["fieldOptions"]))
    assert wid["id"] in [f for c in owner["columns"] for f in c["fields"]], owner["columns"]
    assert len(wopts["columns"]) == 4 and len(wopts["configurableColumns"]) == 4
    assert wopts["dbFieldType"] == "clob"
    # 表格控件不重复出现在任何 grid_layout 里
    assert '"field_type": "tableField"' not in form.replace("&quot;", '"')


@case("TBF08", "tablefield", "对象与表单的列 id 一致，且同一份 Excel 反复出包不变", "P0",
      ["列 id 稳定", "更新导入"])
def _():
    first = _zip(_sheets())
    second = _zip(_sheets())
    for z in (first, second):
        bo = pick(z, "bo/")
        form = pick(z, "form/00")
        bo_ids = [c["id"] for c in _opt(bo, "planExcel")["columns"]]
        _wid, wopts = _table_widget(form, "planExcel")
        assert bo_ids == [c["id"] for c in wopts["columns"]], "对象与表单列 id 必须同一批"
        assert len(set(bo_ids)) == len(bo_ids), "列 id 不能重复"
    assert [c["id"] for c in _opt(pick(first, "bo/"), "planExcel")["columns"]] == \
           [c["id"] for c in _opt(pick(second, "bo/"), "planExcel")["columns"]], \
        "同一份 Excel 两次出包的列 id 必须一致"


@case("TBF09", "tablefield", "反解真实包：columns 落回 2.1_表格列 且列 id 原样保留", "P0",
      ["inspect", "项目立项业务对象.zip"])
def _():
    from pathlib import Path

    from pp6kit import inspect as insp
    from tests._harness import ROOT, skip

    src = ROOT / "PP6" / "业务对象" / "项目立项业务对象.zip"
    if not src.is_file():
        skip("缺少原生样本")
    proj, _notes = insp.inspect_path(Path(src))
    bo = proj.bo("project_create")
    assert bo is not None, [b.code for b in proj.bos]
    tab = bo.field_by_code("planExcel")
    assert tab is not None and tab.type_new == "table", tab
    # 这份原生包（业务对象导出）里是「计划开始年月」，与流程包里那份（计划开始时间）
    # 措辞不同，所以只断言列数与首列表头，别把某一版的文案钉死。
    assert len(tab.table_columns) >= 5, len(tab.table_columns)
    assert tab.table_columns[0].name == "阶段"
    assert all(c.exist_id for c in tab.table_columns), "反解要保留列 id，否则再出包会当成新列"
    # 同一份东西再出包：列 id 不变
    opts = tab.resolved()["options"]
    assert [c["id"] for c in opts["columns"]] == [c.exist_id for c in tab.table_columns]


@case("TBF10", "tablefield", "选项列引用共享字典时写 DIC_TYPE + typeKey", "P1")
def _():
    cols = [{"对象编码": BO, "字段编码": "riskExcel", "列名": "风险等级",
             "列类型": "选项（dic）", "关联字典": "risk_rating"}]
    fields = [
        {"对象编码": BO, "字段编码": "name", "字段名称": "项目名称", "数据类型": "字符串（varchar）"},
        {"对象编码": BO, "字段编码": "riskExcel", "字段名称": "风险控制", "数据类型": "表格（table）"},
    ]
    bo = pick(_zip(_sheets(cols=cols, fields=fields)), "bo/")
    col = _opt(bo, "riskExcel")["columns"][0]
    assert col["optionSource"] == "DIC_TYPE" and col["objectType"] == "risk_rating", col


@case("TBF11", "tablefield", "4_表单控件 手填 dataTemplate 会被纠正成 tableField", "P1")
def _():
    formfields = [
        {"表单key": f"create_{BO}", "字段编码": "name", "显示标签": "项目名称",
         "控件类型": "text"},
        {"表单key": f"create_{BO}", "字段编码": "planExcel", "显示标签": "里程碑计划表",
         "控件类型": "dataTemplate"},
    ]
    res = webui.parse_excel(xlsx(_sheets(formfields=formfields)))
    # 自动补齐的提示走 notes（不是 issues）：控件类型被按对象字段纠正
    notes = list(res.get("notes") or []) + [
        i["message"] for i in res.get("issues", []) if i["level"] in ("warn", "info")]
    assert any("tableField" in n for n in notes), notes
    form = pick(_zip(_sheets(formfields=formfields)), "form/00")
    assert 'fieldType="tableField"' in form
    # 不能真的出成 dataTemplate（那会去找一个同名列表，子表是空的）
    assert 'fieldType="dataTemplate"' not in form


@case("TBF12", "tablefield", "扩展参数里手写 columns 仍生效（逃生口）", "P2")
def _():
    raw = json.dumps({"columns": [
        {"id": "11111111-1111-1111-1111-111111111111", "name": "手写列",
         "nameEn": "", "inputType": "varchar", "required": False},
    ]}, ensure_ascii=False)
    fields = [
        {"对象编码": BO, "字段编码": "planExcel", "字段名称": "里程碑计划表",
         "数据类型": "表格（table）", "扩展参数": raw},
    ]
    bo = pick(_zip(_sheets(cols=[], fields=fields)), "bo/")
    cols = _opt(bo, "planExcel")["columns"]
    assert [c["name"] for c in cols] == ["手写列"], cols
    assert cols[0]["id"] == "11111111-1111-1111-1111-111111111111"


@case("TBF13", "tablefield", "用户列在对象里是 user，表单单元格降级为文本", "P2")
def _():
    cols = [{"对象编码": BO, "字段编码": "planExcel", "列名": "责任人",
             "列类型": "用户（user）"}]
    sheets = _sheets(cols=cols)
    z = _zip(sheets)
    bo = pick(z, "bo/")
    col = _opt(bo, "planExcel")["columns"][0]
    assert col["inputType"] == "user" and col["multiple"] is False, col
    _wid, wopts = _table_widget(pick(z, "form/00"), "planExcel")
    cell = wopts["columns"][0]
    # 表格单元格没有人员选择器：降级成 text，但原始类型留在 inputType 里
    assert cell["type"] == "text" and cell["inputType"] == "user", cell


@case("TBF14", "tablefield", "示例模板自带一张子表，生成的 Excel 可原样读回", "P1",
      ["模板", "2.1_表格列"])
def _():
    import io

    from pp6kit import template

    wb = template.build_workbook(template.demo_project())
    assert spec.S_TABLE_COL in wb.sheetnames
    buf = io.BytesIO()
    wb.save(buf)
    rows = read_xlsx_rows(buf.getvalue(), spec.S_TABLE_COL)
    # 第 2 行是「# 填写说明」，reader 会跳过，这里也按同样规则过滤
    rows = [r for r in rows if not str(r["对象编码"] or "").startswith("#")]
    assert [r["列名"] for r in rows] == ["物料名称", "数量", "单价", "金额", "是否急件"], rows
    proj, errors, _ = project_of(buf.getvalue())
    assert not errors, errors
    cols = proj.bo("demo_purchase").field_by_code("lineItems").table_columns
    assert [c.name for c in cols] == ["物料名称", "数量", "单价", "金额", "是否急件"]
    assert cols[1].precision == 0 and cols[2].precision == 2
    assert cols[4].choices == ["是", "否"]
    _body, _name = webui.build_zip(buf.getvalue())
