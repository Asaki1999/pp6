"""值来源：独立模块（6.1_值来源）+ 每个对象默认一份「自身的值来源」。

用户 2026-11-19 口径：
  1. 值来源是**独立模块**（Excel 单独一张表、勾选树单独一项），不再混在「数据集」里；
  2. 除了「关联对象的全部值来源」之外，**默认再给每个对象一份指向自己的值来源**，
     返回结果默认包含主键 + 该对象的全部自建字段；
  3. 用户可以配置单个或多个值来源（加行即可），显式配置优先。

这组用例守的就是这三条 + 取消勾选后不留悬空引用（linkdata 指向不存在的 key）。

数据侧的事实（判据只在一处，别在这里另写一套）：
  · ``modules.module_of_dataset`` 决定一份 DataSet 归哪个模块；
  · ``blueprint.ensure_self_value_sources`` 生成自身那份，返回列只是打开
    ``DataSet.result_all``，实际清单由 ``links.result_list(all_fields=True)`` 展开。
"""
from __future__ import annotations

import json
import re

from pp6kit import spec as S
from pp6kit import webui
from pp6kit.reader import read_workbook
from tests._harness import case, field, read_xlsx_rows, texts, xlsx
from tests.test_projlink import bo_row, pl_header, project_of, using


# ---------------------------------------------------------------- 小工具

def _cai_gou_sheets(**bo_kw) -> dict:
    """一个最小可出包对象：采购申请（cai_gou）+ 新建/详情表单。"""
    return {
        S.S_BO: [bo_row("采购申请", "cai_gou", **bo_kw)],
        S.S_FIELD: [
            field("cai_gou", "申请单号", "字符串"),
            field("cai_gou", "申请金额", "数字", 精度=2),
            field("cai_gou", "紧急程度", "选项", 选项值="普通,紧急"),
            field("cai_gou", "备注", "多行文本"),
        ],
        S.S_FORM: [{"表单key": "create_caigou", "表单名称": "新建采购申请",
                    "对象编码": "cai_gou", "表单类型": "新建表单"}],
        S.S_FORMFIELD: [{"表单key": "create_caigou", "字段编码": "shenQingDanHao",
                         "显示标签": "申请单号", "控件类型": "text"}],
    }


def _value_entries(body: bytes) -> dict[str, str]:
    """包内所有值来源 XML：{key: 原文}。"""
    out = {}
    for name, text in texts(body).items():
        if not name.startswith("data_template/"):
            continue
        got = re.search(r'<dataTemplateDef\b[^>]*\bkey="([^"]+)"', text)
        if not got:
            continue
        if 'type="valueSource"' in text:
            out[got.group(1)] = text
    return out


def _columns(xml: str) -> list[str]:
    got = re.search(r'resultColumns="([^"]*)"', xml)
    if not got:
        return []
    raw = got.group(1).replace("&quot;", '"').replace("&amp;", "&")
    return [c["name"] for c in json.loads(raw)]


def _notice(body: bytes) -> str:
    import io
    import zipfile

    return zipfile.ZipFile(io.BytesIO(body)).comment.decode("utf-8", "replace")


def _read_via_confirm(excel: bytes):
    """Excel -> 出包 -> 确认版 Excel -> 再读一次（模拟用户「导出/回传」一圈）。"""
    from pp6kit import builder, template

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "in.xlsx"
        src.write_bytes(excel)
        proj, errors, _ = read_workbook(src)
        assert not errors, errors
        builder.build(proj, webui._snippets(), td / "o1")
        confirm = td / "confirm.xlsx"
        template.build_workbook(proj).save(confirm)
        return confirm.read_bytes()


def _save(d: dict) -> bytes:
    return xlsx(d)


# ---------------------------------------------------------------- 用例

@case("VAL01", "value", "默认出包：每个对象自带一份「自身的值来源」", "P0",
      ["用户口径 2026-11-19", "返回主键 + 全部自建字段", "无 filterConditions"])
def _():
    """不勾关联项目、没有任何 6_数据集 行，包里也要有 <对象编码>_value。

    断言的是用户能感知的形态：下拉框能选到本对象的数据，且返回结果带着
    整行的字段（联动才有东西可带），并且**没有**过滤条件（默认全量）。
    """
    body = webui.build_zip(xlsx(_cai_gou_sheets()))[0]
    values = _value_entries(body)
    assert "purchase_apply_value" in values, sorted(values)
    xml = values["purchase_apply_value"]

    assert 'type="valueSource"' in xml
    assert 'boCode="cai_gou"' in xml
    # datasetKey = **目标对象编码**，key 才是这份值来源自己的标识。
    # 依据：线上 203 条选择器 + 样本 out/ref-20261119/data_template/005-zlyglxmlx.xml
    # （datasetKey="project" key="zlyglxmlx" boCode="project"）。
    assert 'datasetKey="cai_gou"' in xml, xml[:400]
    assert 'key="purchase_apply_value"' in xml and 'datasetKey="purchase_apply_value"' not in xml
    assert 'name="采购申请值来源"' in xml
    # 底层 dataSet 的 key/name/from 与 datasetKey 对齐（样本：key="project" name="项目"）
    assert ('<dataSet id="' in xml and 'key="cai_gou"' in xml
            and 'name="采购申请"' in xml and 'from="pp_cai_gou"' in xml), xml[-500:]
    # 取数的是本对象自己的物理表（三处必须同值：dataSet.from / 表名行 / 列 objName）
    assert 'from="pp_cai_gou"' in xml
    assert '<dataTemplateField id="' in xml and 'name="pp_cai_gou"' in xml
    # 主键 + 名称 + 全部自建字段；系统字段（租户/时间/版本）一个都不能漏进来
    assert 'unique="id_"' in xml and 'uniqueField="id_"' in xml
    cols = _columns(xml)
    assert cols[:2] == ["id_", "name_"], cols
    for want in ("shen_qing_dan_hao_", "shen_qing_jin_e_", "jin_ji_cheng_du_", "bei_zhu_"):
        assert want in cols, (want, cols)
    assert not any(c.startswith(("create_time_", "update_time_", "tenant_id_", "version_"))
                   for c in cols), cols
    # 默认不加过滤（自身值来源是「本对象的全部数据」）
    assert "filterConditions" not in xml, xml
    # 提示里要说清「返回主键 + N 个自建字段」，让人能核对
    assert "已生成自身选择器" in _notice(body), _notice(body)[:500]


@case("VAL02", "value", "与链接目标的值来源共存：project_value 与 <对象>_value 各一份", "P0",
      ["勾关联项目", "不重复、不串配置"])
def _():
    data = _cai_gou_sheets(关联项目="是")
    with using():
        body = webui.build_zip(xlsx(data),
                               projlink_header=pl_header({"cai_gou": {"linked": True}}))[0]
    values = _value_entries(body)
    assert set(values) == {"project_value", "purchase_apply_value"}, sorted(values)
    # 链接目标那份仍是「全部业务字段 + 无过滤」（2026-11-19 已定稿口径，不许动）
    assert len(_columns(values["project_value"])) > 2, _columns(values["project_value"])
    assert "filterConditions" not in values["project_value"]
    # 自身那份只返回本对象的字段（含链接字段 project_id_），
    # 不会把项目的 38 列混进来
    cols = _columns(values["purchase_apply_value"])
    assert "shen_qing_dan_hao_" in cols and "project_id_" in cols, cols
    assert not any(c in ("zhuan_ye_", "he_tong_jin_e_") for c in cols), cols


@case("VAL03", "value", "对象自己就是链接目标时：project_value 只有一份，配置不被覆盖", "P0",
      ["复用而不是新建", "用户配置优先"])
def _():
    """本包里既有「指向 project 的关联字段」又有 project 对象本身。

    两份逻辑都想用 ``project_value`` 这个 key：链接字段先建（返回全部业务字段、
    无过滤），自身值来源必须**复用**它，不能再建一份、也不能把它改回「只返回本
    对象字段」。用户手写在 6.1_值来源 的配置更是字面优先。
    """
    data = {
        S.S_BO: [bo_row("项目", "project"),
                 bo_row("采购申请", "cai_gou")],
        S.S_FIELD: [field("project", "项目名称", "字符串"),
                    field("cai_gou", "申请单号", "字符串"),
                    field("cai_gou", "所属项目", "关联对象", **{"关联对象/字典": "project"})],
        S.S_VALUE: [{"值来源key": "project_value", "名称": "项目值来源（我配的）",
                     "对象编码": "project", "返回字段": "id,name", "自动": "否"}],
    }
    with using():
        body = webui.build_zip(xlsx(data))[0]
    values = _value_entries(body)
    assert "project_value" in values, sorted(values)
    # 只有一份（zip 里不允许两条同 key 的 dataTemplate：导入端按名字取文件）
    names = [n for n, t in texts(body).items()
             if 'key="project_value"' in t and n.startswith("data_template/")]
    assert len(names) == 1, names
    # 用户显式写的返回列优先（不给默认口径盖回去）
    assert _columns(values["project_value"]) == ["id_", "name_"], _columns(values["project_value"])
    assert 'name="项目值来源（我配的）"' in values["project_value"]


@case("VAL04", "value", "Excel 往返：6.1_值来源 一份一行、自动=是，读回再出包幂等", "P0",
      ["预览=产物", "自动列", "往返不改配置"])
def _():
    excel = xlsx(_cai_gou_sheets())
    _ = webui.build_zip(excel)             # 先出一次包（生成自身值来源）
    confirm = _read_via_confirm(excel)

    rows = [r for r in read_xlsx_rows(confirm, S.S_VALUE)
            if str(r.get("值来源key") or "").strip()
            and not str(r.get("值来源key")).startswith("#")]
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["值来源key"] == "purchase_apply_value", row
    assert row["名称"] == "采购申请值来源", row
    # 列口径：对象编码（不是物理表名）+ 主键/名称/唯一 + 返回 `*` + 过滤「无」
    assert row["对象编码"] == "cai_gou", row
    assert row["主键字段"] == "id_" and row["名称字段"] == "name_", row
    assert row["唯一字段"] == "id_", row
    assert row["返回字段"] == "*", row
    assert row["过滤字段"] == "无", row
    assert row["自动"] == "是", row

    # 读回再出包 → 值来源数量与配置完全一致（幂等）
    body1 = webui.build_zip(excel)[0]
    body2 = webui.build_zip(confirm)[0]
    v1, v2 = _value_entries(body1), _value_entries(body2)
    assert sorted(v1) == sorted(v2) == ["purchase_apply_value"], (sorted(v1), sorted(v2))
    assert _columns(v1["purchase_apply_value"]) == _columns(v2["purchase_apply_value"]), (
        _columns(v1["purchase_apply_value"]), _columns(v2["purchase_apply_value"]))


@case("VAL05", "value", "多值来源：6.1_值来源 加两行就出两份 XML，配置照写", "P0",
      ["用户加单个或多个自身/其它对象的值来源", "过滤字段/过滤目标分别落到列"])
def _():
    data = _cai_gou_sheets()
    data[S.S_VALUE] = [
        # ① 本对象，但按「项目类型」过滤（写 字段=目标列 的形态）
        {"值来源key": "cai_gou_by_type", "名称": "采购申请（按类型）",
         "对象编码": "cai_gou", "返回字段": "id,name", "过滤字段": "jinJiChengDu=urgency_"},
        # ② 另一个对象（不在本包里 → 走离线兜底，仍要出包）
        {"值来源key": "cust_value", "名称": "客户值来源",
         "对象编码": "customer", "返回字段": "*", "过滤字段": "无", "自动": "否"},
    ]
    body = webui.build_zip(xlsx(data))[0]
    values = _value_entries(body)
    assert {"purchase_apply_value", "cai_gou_by_type", "cust_value"} <= set(values), sorted(values)

    # 显式返回列优先（不是默认的全部字段）
    assert _columns(values["cai_gou_by_type"]) == ["id_", "name_"], \
        _columns(values["cai_gou_by_type"])
    # 显式过滤条件：字段 + 目标列都照写
    assert "filterConditions" in values["cai_gou_by_type"], values["cai_gou_by_type"]
    assert "urgency_" in values["cai_gou_by_type"]

    # 另一对象：`*` = 全部字段口径（离线兜底只有主键/名称，能出包不报错就是底线）
    assert 'from="pp_customer"' in values["cust_value"], values["cust_value"]
    assert "filterConditions" not in values["cust_value"]


@case("VAL06", "value", "老 Excel 兼容：值来源写在 6_数据集 里照样读、能出包、不重复建", "P0",
      ["向后兼容", "同一 key 两处都有时以 6.1 为准"])
def _():
    data = _cai_gou_sheets()
    data[S.S_DATASET] = [
        {"数据集key": "purchase_apply_value", "数据集名称": "采购申请值来源（老的写法）",
         "主表名": "pp_cai_gou", "类型": "valueSource",
         "返回字段": "id,name,shenQingDanHao"},
        {"数据集key": "cai_gou_list_ds", "数据集名称": "列表数据集",
         "主表名": "pp_cai_gou", "类型": "default"},
    ]
    body = webui.build_zip(xlsx(data))[0]
    values = _value_entries(body)
    # 老写法读进来就是「用户显式配置」：不会被自身值来源再建一份
    assert [k for k in values if k == "purchase_apply_value"] == ["purchase_apply_value"], sorted(values)
    assert 'name="采购申请值来源（老的写法）"' in values["purchase_apply_value"]
    assert _columns(values["purchase_apply_value"]) == ["id_", "name_", "shen_qing_dan_hao_"], \
        _columns(values["purchase_apply_value"])
    # 列表数据集仍在「数据集」模块里，不能因为新表被漏掉
    entries = [n for n in texts(body) if n.startswith("data_template/")]
    assert any("cai_gou_list_ds" in n for n in entries), entries


@case("VAL07", "value", "取消勾选「选择器」：包里没有值来源，linkdata 降级成对象选择器", "P0",
      ["不留悬空引用", "selectObject + notice"])
def _():
    data = _cai_gou_sheets(关联项目="是")
    with using():
        body = webui.build_zip(xlsx(data), codes=["bo", "form"],
                               projlink_header=pl_header({"cai_gou": {"linked": True}}))[0]
    values = _value_entries(body)
    assert not values, sorted(values)                 # 包里一份值来源都没有
    # 包里任何一个控件都不许再挂 linkdata（它的 linkdata 键指向的是一份
    # 不存在于包里的值来源）；原「项目选择」控件必须换成对象选择器。
    frags = [f for n, t in texts(body).items() if n.startswith("form/")
             for f in re.findall(r"<formField\b[^>]*>", t)]
    assert frags, "包里没有表单控件"
    assert not [f for f in frags if 'fieldType="linkdata"' in f], \
        [f for f in frags if 'fieldType="linkdata"' in f][:2]
    chosen = [f for f in frags if 'name="projectId"' in f]
    assert chosen and all('fieldType="selectObject"' in f for f in chosen), chosen[:2]
    assert "没有勾选「选择器」模块" in _notice(body), _notice(body)[:600]


@case("VAL08", "value", "统计与模块清单：选择器独立一项，数据集不再包含它", "P1",
      ["/api/parse stats", "modules[] 勾选树"])
def _():
    data = _cai_gou_sheets()
    data[S.S_DATASET] = [{"数据集key": "cai_gou_list_ds", "数据集名称": "列表数据集",
                          "主表名": "pp_cai_gou", "类型": "default"}]
    res = webui.parse_excel(xlsx(data))
    stats = res["stats"]
    assert stats["选择器"] == 1, stats          # 只有自身那份
    assert stats["数据集"] == 1, stats          # 只剩列表数据集
    mods = {m["code"]: m for m in res["modules"]}
    assert mods["value"]["count"] == 1, mods["value"]
    assert mods["value"]["label"] == "选择器", mods["value"]
    assert mods["value"]["folder"] == "data_template", mods["value"]
    assert mods["dataset"]["count"] == 1, mods["dataset"]
    # 预览的数据源清单要能区分「值来源」与「数据集」
    kinds = {d["key"]: d["module"] for d in res["datasets"]}
    assert kinds["purchase_apply_value"] == "value", kinds
    assert kinds["cai_gou_list_ds"] == "dataset", kinds


@case("VAL09", "value", "用户改自动生成那份的配置：出包按他的来（返回列 + 过滤）", "P0",
      ["自动=是 也能改", "显式配置优先"])
def _():
    data = _cai_gou_sheets()
    # 「过滤字段」只写本对象字段编码时，目标列要靠「谁在用这份值来源」回查表单
    # 推导（links.filter_of）；这份值来源没有任何控件引用 → 推导不出来。
    # 所以用户自己加的值来源要么写 `本字段=目标列`，要么在「过滤目标」列里指明。
    data[S.S_VALUE] = [{"值来源key": "purchase_apply_value", "名称": "采购申请值来源",
                        "对象编码": "cai_gou", "返回字段": "id,name,shenQingDanHao",
                        "过滤字段": "jinJiChengDu", "过滤目标": "jin_ji_cheng_du_",
                        "自动": "是"}]
    body = webui.build_zip(xlsx(data))[0]
    xml = _value_entries(body)["purchase_apply_value"]
    assert _columns(xml) == ["id_", "name_", "shen_qing_dan_hao_"], _columns(xml)
    # 过滤字段 / 过滤目标填了就要真过滤（不再走「主键+名称」的默认口径）
    assert "filterConditions" in xml, xml
    assert "jin_ji_cheng_du_" in xml, xml


@case("VAL10", "value", "重复出包不重复提示：第二遍读回自带的那行不再刷「已生成」", "P1",
      ["幂等提示", "确认版 Excel 再出包"])
def _():
    excel = xlsx(_cai_gou_sheets())
    first = _notice(webui.build_zip(excel)[0])
    assert "已生成自身选择器" in first, first[:400]
    confirm = _read_via_confirm(excel)
    second = _notice(webui.build_zip(confirm)[0])
    assert "已生成自身选择器" not in second, second[:400]


@case("VAL11", "value", "驼峰对象编码：key 按对象名翻英文（purchase_apply_value），往返仍能出包", "P0",
      ["validate.KEY_RE 只收小写", "确认版 Excel 再上传不能报错", "名字优先、编码兜底"])
def _():
    """对象编码允许驼峰（caiGou），dataTemplate 的 key 只允许小写且**用英文**。

    两层都要成立：
      1. 有对象名时走 ``naming.object_code``（英文词典优先）——
         「采购申请」→ purchase_apply → key ``purchase_apply_value``。
         这是用户 2026-11-20 的口径：key 不要拼音（见
         ``model.value_source_key_base`` 里为什么不用 ``to_english``）。
      2. 只有编码、没有名字时退回「编码小写」：caiGou → ``caigou_value``。
         直接拼 ``caiGou_value`` 会让确认版 Excel 再上传时被自己的校验拦住
         （「数据集key 必须小写字母开头」），用户从此出不了包。
    """
    data = {
        S.S_BO: [{"对象名称": "采购申请", "对象编码": "caiGou", "物理表名": "pp_cai_gou"}],
        S.S_FIELD: [field("caiGou", "申请单号", "字符串")],
        S.S_FORM: [{"表单key": "create_caigou", "表单名称": "新建采购申请",
                    "对象编码": "caiGou", "表单类型": "新建表单"}],
        S.S_FORMFIELD: [{"表单key": "create_caigou", "字段编码": "shenQingDanHao",
                         "显示标签": "申请单号", "控件类型": "text"}],
    }
    from pp6kit import blueprint
    from pp6kit.model import value_source_key
    from pp6kit.validate import validate

    # ① 没有名字 → 退编码小写（老口径，保证合法）
    assert value_source_key("caiGou") == "caigou_value", value_source_key("caiGou")
    # ② 有名字 → 英文（新口径）
    assert value_source_key("caiGou", bo_name="采购申请") == "purchase_apply_value", \
        value_source_key("caiGou", bo_name="采购申请")

    # 驼峰对象编码 + 本工具不参与 validate 的既有行为（表单 key 也是驼峰），
    # 所以这里直接看模型与校验结论，不走出包（出包会在表单 key 那条报错，
    # 那是另一个既有问题，与值来源无关）。
    proj, errors = project_of(data)
    assert not errors, errors
    blueprint.ensure_self_value_sources(proj)
    keys = [d.key for d in proj.datasets]
    assert keys == ["purchase_apply_value"], keys
    bad = [i for i in validate(proj) if i.where.startswith("数据集 ")]
    assert not bad, [(i.where, i.message) for i in bad]

    # 读回来（用户把这一行改了再上传）也认这个 key，不会被当成新的一份
    again, errors2 = project_of(data)
    again.datasets.append(proj.datasets[0])
    blueprint.ensure_self_value_sources(again)
    assert [d.key for d in again.datasets] == ["purchase_apply_value"], \
        [d.key for d in again.datasets]


@case("VAL12", "value", "同一对象多份：key/名称都留空时自动编号 _2 / _3，各出一份 XML", "P0",
      ["key 不让用户填", "用途后缀 _全部 / _过滤xxx", "datasetKey 都是对象编码"])
def _():
    """用户口径：一个对象可以配多个选择器，但 key 由工具生成。

    表里加行时两列（值来源key / 名称）都可以留空——留空就按行序排
    ``<code>_value`` / ``_2`` / ``_3``，名称带用途后缀（_全部 / _过滤项目类型），
    设计器的列表里能一眼分开；三份 XML 的 datasetKey 都是同一个对象编码。
    """
    import json as _json
    import re as _re

    data = _cai_gou_sheets()
    data[S.S_VALUE] = [
        # ① 两列全空：key 拿到 <code>_value，名称就是「采购申请值来源」
        {"对象编码": "cai_gou"},
        # ② 只给过滤：key 自动排到 _2，名称带用途「过滤紧急程度」
        {"对象编码": "cai_gou", "过滤字段": "jinJiChengDu=jin_ji_cheng_du_"},
        # ③ 只要主键 + 名称的那份：key _3，名称带「全部」
        {"对象编码": "cai_gou", "返回字段": "id,name"},
    ]
    body = webui.build_zip(xlsx(data))[0]
    values = _value_entries(body)
    # 三行 + 「自身那份」：自身那份的 key 已被第①行占用 → 复用它，不再加第四份
    assert sorted(values) == ["purchase_apply_value", "purchase_apply_value_2", "purchase_apply_value_3"], \
        sorted(values)

    names = {k: _re.search(r'<dataTemplateDef\b[^>]*\bname="([^"]*)"', v).group(1)
             for k, v in values.items()}
    assert names["purchase_apply_value"] == "采购申请值来源", names
    assert names["purchase_apply_value_2"] == "采购申请值来源_过滤紧急程度", names
    assert names["purchase_apply_value_3"] == "采购申请值来源_全部", names

    # 三份都挂在同一个对象（datasetKey = 对象编码），各自有独立的 key
    for key, xml in values.items():
        assert 'datasetKey="cai_gou"' in xml, (key, xml[:300])
        assert f'key="{key}"' in xml, (key, xml[:300])
    # ② 那份真的带过滤；① ③ 不带
    assert "filterConditions" in values["purchase_apply_value_2"]
    assert "filterConditions" not in values["purchase_apply_value"]
    assert "filterConditions" not in values["purchase_apply_value_3"]

    # 往返一圈：编号/名称都被写回 Excel，再出包不再新增
    confirm = _read_via_confirm(xlsx(data))
    rows = [r for r in read_xlsx_rows(confirm, S.S_VALUE)
            if str(r.get("值来源key") or "").strip()
            and not str(r.get("值来源key")).startswith("#")]
    assert [r["值来源key"] for r in rows] == ["purchase_apply_value", "purchase_apply_value_2",
                                              "purchase_apply_value_3"], rows
    assert sorted(_value_entries(webui.build_zip(confirm)[0])) == sorted(values)


@case("VAL13", "value", "常驻口径：值来源的 datasetKey == boCode（目标对象编码），且 != key", "P0",
      ["线上 203 条没有一条 datasetKey==key", "样本 005-zlyglxmlx：datasetKey=project key=zlyglxmlx",
       "反解只剩表名时按 pp_ 前缀推导并出 notice"])
def _():
    """这条是**结构断言**，不是某个场景的断言：任何一份值来源 XML 都必须满足

        datasetKey == boCode == 目标对象编码
        datasetKey != key          （key 是这份值来源自己的标识）

    以前 datasetKey 写成 ds.key（指向自己），线上设计器里这份选择器挂不到真正的
    数据集上，而界面上看不出任何异常。以后若有人把 key 改成等于对象编码，这条会
    立刻提醒他：那是**有意的例外**，得先确认平台能接受。
    """
    import re as _re

    from pp6kit.model import Bo, BoField, Project

    def check(body: bytes, label: str) -> None:
        vals = _value_entries(body)
        assert vals, f"{label}: 包里没有值来源"
        for key, xml in vals.items():
            got = {k: _re.search(rf'\b{k}="([^"]*)"', xml).group(1)
                   for k in ("datasetKey", "boCode", "key")}
            assert got["key"] == key, (label, key, got)
            assert got["datasetKey"] == got["boCode"], (label, key, got)
            assert got["datasetKey"] != got["key"], (label, key, got)
            # 空值/表名都不许出现在 datasetKey 上
            assert got["datasetKey"] and not got["datasetKey"].startswith("pp_"), (label, key, got)

    check(webui.build_zip(xlsx(_cai_gou_sheets()))[0], "默认出包")
    with using():
        linked = webui.build_zip(
            xlsx(_cai_gou_sheets(关联项目="是")),
            projlink_header=pl_header({"cai_gou": {"linked": True}}))[0]
    check(linked, "关联项目包")

    # 驼峰对象编码：datasetKey / boCode 保持驼峰（对象编码原样），key 用 lower
    proj = Project()
    bo = Bo(code="caiGou", name="采购申请", tbl_name="pp_cai_gou")
    bo.fields = [BoField(bo_code="caiGou", code="shenQingDanHao", name="申请单号",
                         type_new="varchar", sn=1)]
    proj.bos = [bo]
    from pp6kit import blueprint as _bp
    _bp.ensure_self_value_sources(proj)
    got = proj.datasets[0]
    assert (got.key, got.name) == ("purchase_apply_value", "采购申请值来源"), (got.key, got.name)

    # 只剩物理表名（反解包 / 手写）→ 按 pp_ 前缀推导成对象编码，并出 notice
    data = _cai_gou_sheets()
    data[S.S_VALUE] = [{"值来源key": "unknown_value", "名称": "未知对象值来源",
                        "对象编码": "pp_unknown_thing"}]
    body = webui.build_zip(xlsx(data))[0]
    xml = _value_entries(body)["unknown_value"]
    assert 'datasetKey="unknown_thing"' in xml, xml[:400]
    assert 'boCode="unknown_thing"' in xml, xml[:400]
    note = _notice(body)
    assert "由物理表名 pp_unknown_thing 推导为 unknown_thing" in note, note[-600:]


@case("VAL14", "value", "列表数据集不被值来源的口径带偏：勾不勾选择器，列表 XML 一字不差", "P0",
      ["datasetKey 的新口径只对 type=valueSource 生效", "样本 004-lllkk：列表的 datasetKey 是底层 dataSet 的 key"])
def _():
    """``render_dataset`` 的 datasetKey 从 ``ds.key`` 改成了「目标对象编码」。

    这条改动只该作用在值来源上：列表（``render_list``）的 datasetKey 是它底层
    ``<dataSet>`` 的 key —— 样本 ``004-lllkk.xml`` 里 ``dataTemplateDef`` 是
    ``key="lllkk" datasetKey="glxmd1111"``、底下 ``<dataSet key="glxmd1111"
    from="zgsglxmdptdx">``，列表 key 与 datasetKey **本来就不相等**。
    所以「datasetKey == 列表自己的 key」不是平台口径；真正要守的是
    「datasetKey == 底层 dataSet 的 key」以及「勾了选择器不会把列表改掉」。
    """
    import re as _re

    data = _cai_gou_sheets()
    data[S.S_LIST] = [{"列表key": "caigou_list", "列表名称": "采购列表",
                       "对象编码": "cai_gou", "视图类型": "grid"}]

    def norm(text: str) -> str:
        """抹掉随机 id / 时间戳：产物里这些每次都变，不抹掉比较没有意义。"""
        text = _re.sub(r'(id|templateId|createTime|updateTime)="[^"]*"', "", text)
        return _re.sub(r'&quot;(id|parentId)&quot;:&quot;[^&]*&quot;', "", text)

    def list_xml(body: bytes) -> str:
        for name, text in texts(body).items():
            if name.startswith("data_template/") and 'type="default"' in text:
                return text
        raise AssertionError(f"包里没有列表 XML：{sorted(texts(body))}")

    only_list = webui.build_zip(xlsx(data), codes=["bo", "list"])[0]
    with_value = webui.build_zip(xlsx(data))[0]

    # 勾了选择器 → 多出一份值来源；列表本身一字不差（新口径没串到列表上）
    assert "purchase_apply_value" in _value_entries(with_value), sorted(_value_entries(with_value))
    assert not _value_entries(only_list), sorted(_value_entries(only_list))
    assert norm(list_xml(only_list)) == norm(list_xml(with_value)), \
        "勾上「选择器」后列表 XML 变了——datasetKey 的新口径串到 render_list 上了"

    xml = list_xml(with_value)
    head = _re.search(r"<dataTemplateDef\b[^>]*\btype=\"default\"[^>]*>", xml) \
        or _re.search(r"<dataTemplateDef\b[^>]*>", xml)
    got = dict(_re.findall(r'([\w:.-]+)="([^"]*)"', head.group(0)))
    ds = _re.search(r"<dataSet\b[^>]*?/?>", xml)
    ds_attrs = dict(_re.findall(r'([\w:.-]+)="([^"]*)"', ds.group(0))) if ds else {}
    # 列表仍是 type=default，key 仍是「列表」自己的（reader 按对象编码归一到
    # <对象编码>_list）；datasetKey 跟底层 dataSet 走（= 对象编码）
    assert got["type"] == "default", got
    assert got["key"].endswith("_list") and got["key"] != "purchase_apply_value", got
    assert ds_attrs.get("key") == got["datasetKey"] == "cai_gou", (got, ds_attrs)
    assert ds_attrs.get("from") == "pp_cai_gou", ds_attrs

    # 值来源那份同一个不变量：datasetKey == 底层 dataSet 的 key
    vxml = _value_entries(with_value)["purchase_apply_value"]
    vdt = dict(_re.findall(r'([\w:.-]+)="([^"]*)"',
                           _re.search(r"<dataTemplateDef\b[^>]*>", vxml).group(0)))
    vds = dict(_re.findall(r'([\w:.-]+)="([^"]*)"',
                           _re.search(r"<dataSet\b[^>]*?/?>", vxml).group(0)))
    assert vdt["datasetKey"] == vds["key"] == "cai_gou", (vdt, vds)
    # 两份 XML 不能撞 key（导入端按 key 建模板，撞了互相覆盖）
    assert vdt["key"] != got["key"], (vdt["key"], got["key"])


@case("VAL15", "value", "「返回字段」留空 = 全部自建字段（默认档）；最小列要显式写 id,name", "P0",
      ["用户口径：返回结果默认含主键 + 全部自建字段", "6.1 与 6_数据集 同一口径",
       "加一行只填对象编码不会把默认那份顶替成 2 列"])
def _():
    """三档必须能表达：留空 / `*` = 全字段；显式清单 = 按填的来；`id,name` = 最小列。

    为什么值得单列一条：留空与 `*` 是同一档（用户口径的默认），而「最小列」必须
    显式写 `id,name` —— 两档抢同一格的话，往返一次口径就翻（留空写回、读回变
    全字段）。这里同时钉住两张表口径一致，以及「只填对象编码的那行不会把自动
    生成的默认那份顶替成只有主键+名称」。
    """
    import re as _re

    def cols_of(body: bytes, key: str = "purchase_apply_value") -> list[str]:
        return _columns(_value_entries(body)[key])

    # ① 默认（不加 6.1 行）：全字段
    base = webui.build_zip(xlsx(_cai_gou_sheets()))[0]
    default_cols = cols_of(base)
    assert len(default_cols) > 2, default_cols

    # ② 6.1 加一行只填「对象编码」，返回字段留空 → **与默认逐列相同**
    data = _cai_gou_sheets()
    data[S.S_VALUE] = [{"对象编码": "cai_gou"}]
    body = webui.build_zip(xlsx(data))[0]
    values = _value_entries(body)
    assert sorted(values) == ["purchase_apply_value"], sorted(values)   # 只有一份，没被拆成两份
    assert cols_of(body) == default_cols, (cols_of(body), default_cols)

    # ③ 显式 id,name → 只要最小列（"id,name" 也写回 Excel，往返不翻）
    data = _cai_gou_sheets()
    data[S.S_VALUE] = [{"对象编码": "cai_gou", "返回字段": "id,name"}]
    body = webui.build_zip(xlsx(data))[0]
    assert cols_of(body) == ["id_", "name_"], cols_of(body)

    # ④ 老 6_数据集 的值来源行、返回字段留空 → 同一口径（全字段）
    data = _cai_gou_sheets()
    data[S.S_DATASET] = [{"数据集key": "purchase_apply_value", "数据集名称": "采购申请值来源",
                          "主表名": "pp_cai_gou", "类型": "valueSource"}]
    body = webui.build_zip(xlsx(data))[0]
    assert cols_of(body) == default_cols, cols_of(body)

    # ⑤ 最小列那份要显式写回 Excel（否则读回来会被当成「全字段」）
    data = _cai_gou_sheets()
    data[S.S_VALUE] = [{"对象编码": "cai_gou", "返回字段": "id,name"}]
    confirm = _read_via_confirm(xlsx(data))
    rows = [r for r in read_xlsx_rows(confirm, S.S_VALUE)
            if str(r.get("值来源key") or "").strip()
            and not str(r.get("值来源key")).startswith("#")]
    assert rows[0]["返回字段"] == "id,name", rows[0]
    assert _columns(_value_entries(webui.build_zip(confirm)[0])["purchase_apply_value"]) == \
        ["id_", "name_"], "往返后最小列口径被改成了全字段"


@case("VAL16", "value", "未勾「选择器」：notice 不再出现「已自动创建 / 返回 N 列」，也不带 markdown 星号", "P0",
      ["同一段提示不能既说『不含』又说『已创建』", "纯文本 notice 不出现 **"])
def _():
    """没勾选择器时，模型里仍然会有值来源（表单接线要用），但它们不进包、
    控件还会降级。此时再报「已自动创建值来源数据集…」「→ 值来源…返回 38 列」
    就是自相矛盾——用户会去包里翻那 38 列。

    于是模型层那几句由 ``announce_value_sources=False`` 关掉，只留两条结论：
    「本次包里不含任何值来源」「控件改用对象选择器」。
    """
    with using():
        body = webui.build_zip(xlsx(_cai_gou_sheets(关联项目="是")), codes=["bo", "form"],
                               projlink_header=pl_header({"cai_gou": {"linked": True}}))[0]
    note = _notice(body)
    assert "不含任何值来源" in note, note[-800:]
    assert "改用对象选择器" in note, note[-800:]
    for banned in ("已自动创建值来源数据集", "引用系统已有数据集", "返回 38 列",
                   "按「XX选择」口径返回全部业务字段"):
        assert banned not in note, (banned, note[-1200:])
    # P2-2：纯文本 notice 里不许有 markdown 强调
    assert "**" not in note, [ln for ln in note.splitlines() if "**" in ln]


@case("VAL17", "value", "跨模块 key 撞车（列表 vs 选择器）→ 校验与出包都拦住，报错指出两张表", "P0",
      ["导入端按 key 建模板，撞车=后一条静默覆盖", "错误里要能看出改哪张表"])
def _():
    data = _cai_gou_sheets()
    # 用户习惯把列表 key 写成 <对象>_value —— 正好撞上自动生成的那份选择器
    data[S.S_LIST] = [{"列表key": "purchase_apply_value", "列表名称": "采购列表",
                       "对象编码": "cai_gou", "视图类型": "grid"}]

    from pp6kit import blueprint
    from pp6kit.validate import validate
    proj, errors = project_of(data)
    assert not errors, errors
    blueprint.ensure_self_value_sources(proj)
    hits = [i.message for i in validate(proj) if i.level == "error"]
    assert hits, "validate 没报跨模块 key 撞车"
    assert any("5_列表" in m and "6.1_值来源" in m and "purchase_apply_value" in m for m in hits), hits

    with using():
        try:
            webui.build_zip(xlsx(data))
        except ValueError as exc:
            msg = str(exc)
        else:
            raise AssertionError("撞车了还能出包：包里会有两条 data_template/purchase_apply_value.xml")
    assert "5_列表" in msg and "6.1_值来源" in msg, msg
    assert "data_template/purchase_apply_value.xml" in msg, msg


@case("VAL18", "value", "6.1_值来源 同表内 key 重复：保留后者，并说清被覆盖的是第几行", "P1",
      ["跨表覆盖(6_数据集→6.1)是设计，同表重复是填错", "不留静默行为"])
def _():
    data = _cai_gou_sheets()
    data[S.S_VALUE] = [
        {"值来源key": "purchase_apply_value", "名称": "第一份", "对象编码": "cai_gou",
         "返回字段": "id,name"},
        {"值来源key": "purchase_apply_value", "名称": "第二份", "对象编码": "cai_gou",
         "返回字段": "*"},
    ]
    proj, errors = project_of(data)
    assert not errors, errors
    assert [d.name for d in proj.datasets] == ["第二份"], [d.name for d in proj.datasets]
    notes = getattr(proj, "auto_notes", []) or []
    assert any("「purchase_apply_value」与第" in n and "被覆盖" in n for n in notes), notes
    # 提示里要带表名，用户才知道去改哪一张
    assert any(S.S_VALUE in n for n in notes), notes


@case("VAL19", "value", "「留空=全字段」只限值来源行：列表数据集 / 筛选器留空仍是主键+名称", "P0",
      ["口径不能溢出到 default / filter", "6_数据集 三种行混在一张表里"])
def _():
    """``6_数据集`` 一张表里混着三种行：列表数据集（default）、筛选器（filter）、
    老格式的值来源行（valueSource）。「返回字段留空 = 全部自建字段」是给**值来源**
    定的口径（用户口径：返回结果默认含主键 + 全部自建字段），不能顺着同一段代码
    溢到 default/filter 上——列表模板真正取的是 displayColumns，返回列忽然多几列
    属于没人要求过的行为变化。

    这条把三条边界一起钉住：default 留空 = 2 列、filter 留空 = 2 列、
    valueSource 留空 = 全字段（老写法也照旧）。
    """
    import re as _re

    data = _cai_gou_sheets()
    data[S.S_DATASET] = [
        {"数据集key": "cai_gou_list_ds", "数据集名称": "采购列表数据集",
         "主表名": "pp_cai_gou", "类型": "default"},
        {"数据集key": "cai_gou_filter", "数据集名称": "采购筛选器",
         "主表名": "pp_cai_gou", "类型": "filter"},
        {"数据集key": "purchase_apply_value", "数据集名称": "采购申请值来源",
         "主表名": "pp_cai_gou", "类型": "valueSource"},
    ]
    body = webui.build_zip(xlsx(data))[0]
    values = _value_entries(body)
    assert "purchase_apply_value" in values, sorted(values)
    assert len(_columns(values["purchase_apply_value"])) == 6, _columns(values["purchase_apply_value"])

    # 数据集/筛选器那两条也各出了 XML（type 不是 valueSource，不在 _value_entries 里）
    others = {k: v for k, v in texts(body).items()
              if k.startswith("data_template/") and 'type="valueSource"' not in v}
    cols_of = {_re.search(r'<dataTemplateDef\b[^>]*\bkey="([^"]*)"', v).group(1):
               [c["name"] for c in __import__("json").loads(
                   _re.search(r'resultColumns="([^"]*)"', v).group(1)
                   .replace("&quot;", '"'))]
               for v in others.values()}
    assert cols_of.get("cai_gou_list_ds") == ["id_", "name_"], cols_of
    assert cols_of.get("cai_gou_filter") == ["id_", "name_"], cols_of


# ---------------------------------------------------------------- key 的命名口径
#
# 用户 2026-11-20：「你选择器的 key 是怎么命名的？是用拼音吗，如果是，请改为英文命名」
#
# 结论：key 的词干取**对象名称**的英文（``naming.object_code``：英文词典优先、
# 查不到才退拼音），编码只做兜底；``datasetKey`` / ``boCode`` 仍写对象编码。
# 这两条是两回事，后面四条用例把四个角都钉住：
#   · 常见对象批量 → 英文 key；
#   · 词典缺词 → 拼音兜底 + a notice（不静默）；
#   · 连名字都没有 → 退对象编码 + a notice；
#   · 往返一圈 → 英文 key 写回 Excel，读回不再生成。

@case("VAL20", "value", "key 用对象名的英文：项目/合同/需求/测试用例/项目阶段各一份", "P0",
      ["用户 2026-11-20：不要拼音", "naming.object_code 英文词典优先"])
def _():
    """五个常见对象一次出包，看每份值来源的 key 是不是英文。

    判据只认 ``naming.object_code``（英文词典优先、查不到才退拼音），不在这里
    另写一张对照表——词典改了，用例跟着改才是对的；而 key 与 datasetKey 的
    分工（key 英文 / datasetKey = 对象编码）在同一条里一起钉住。
    """
    import re as _re

    from pp6kit import naming

    pairs = [("项目", "xiang_mu"), ("合同", "hetong"), ("需求", "xu_qiu"),
             ("测试用例", "ce_shi_yong_li"), ("项目阶段", "xiang_mu_jie_duan")]
    data = {
        S.S_BO: [{"对象名称": name, "对象编码": code, "物理表名": f"pp_{code}"}
                 for name, code in pairs],
        S.S_FIELD: [field(code, "名称", "字符串") for _name, code in pairs],
        S.S_FORM: [{"表单key": "create_" + code, "表单名称": "新建" + name,
                    "对象编码": code, "表单类型": "新建表单"}
                   for name, code in pairs],
        S.S_FORMFIELD: [{"表单key": "create_" + code, "字段编码": "name",
                         "显示标签": "名称", "控件类型": "text"}
                        for _name, code in pairs],
    }
    body = webui.build_zip(xlsx(data))[0]
    values = _value_entries(body)
    want = {naming.object_code(name): code for name, code in pairs}
    assert set(values) == {f"{stem}_value" for stem in want}, sorted(values)

    for stem, code in want.items():
        xml = values[f"{stem}_value"]
        assert f'key="{stem}_value"' in xml, (stem, xml[:300])
        # datasetKey / boCode 仍是**对象编码**（这里故意用拼音编码，最能看出两回事）
        assert f'datasetKey="{code}"' in xml, (stem, xml[:300])
        assert f'boCode="{code}"' in xml, (stem, xml[:300])
        # 名称不翻译：仍是「<对象名>值来源」
        name = _re.search(r'<dataTemplateDef\b[^>]*\bname="([^"]*)"', xml).group(1)
        assert name.endswith("值来源"), (stem, name)
    # 具体几个（词典命中时不能出拼音）
    for key in ("project_value", "contract_value", "requirement_value",
                "test_case_value", "project_phase_value"):
        assert key in values, (key, sorted(values))


@case("VAL21", "value", "词典缺词：key 用拼音兜底，但必须出提示（说清哪个词、什么拼音）", "P0",
      ["不静默：project_budget_kemu 用户要知道该去补词", "naming.has_pinyin / pinyin_parts"])
def _():
    """「项目预算科目」里「科目」没收录 → key ``project_budget_kemu``。

    拼音兜底本身是为了「不影响出包」，但**不能静默**：用户看到 ``kemu`` 只会
    觉得名字怪，不知道是自己该去 ``naming.WORDS`` 补个词。所以提示要能读出
    「哪个中文词、退成了什么拼音」。
    """
    from pp6kit import naming

    assert naming.object_code("项目预算科目") == "project_budget_kemu"
    assert naming.has_pinyin("项目预算科目")
    assert naming.pinyin_parts("项目预算科目") == [("科目", "kemu")]
    # 常见对象名一个都不该被判成拼音
    assert not naming.has_pinyin("测试用例"), naming.pinyin_parts("测试用例")

    data = {
        S.S_BO: [{"对象名称": "项目预算科目", "对象编码": "yusuanKemu",
                  "物理表名": "pp_yusuan_kemu"}],
        S.S_FIELD: [field("yusuanKemu", "名称", "字符串")],
        S.S_FORM: [{"表单key": "create_yusuan", "表单名称": "新建预算科目",
                    "对象编码": "yusuanKemu", "表单类型": "新建表单"}],
        S.S_FORMFIELD: [{"表单key": "create_yusuan", "字段编码": "name",
                         "显示标签": "名称", "控件类型": "text"}],
    }
    body = webui.build_zip(xlsx(data))[0]
    values = _value_entries(body)
    assert "project_budget_kemu_value" in values, sorted(values)
    xml = values["project_budget_kemu_value"]
    assert 'datasetKey="yusuanKemu"' in xml, xml[:300]     # 编码原样（驼峰也不改）

    note = _notice(body)
    assert "未收录英文词典" in note, note[-800:]
    assert "已用拼音" in note and "kemu" in note, note[-800:]
    assert "「科目」" in note, note[-800:]                  # 说清是哪个中文词
    assert "naming.WORDS" in note, note[-800:]              # 说清去哪补


@case("VAL22", "value", "对象名查不到：key 退回对象编码，并说明「如需英文请补对象名称」", "P0",
      ["不猜、不静默", "编码兜底仍要保证 key 合法"])
def _():
    """6.1_值来源 只填了一个包里没有、系统清单里也没有的对象编码。

    这时没有名字可翻，只能退回「对象编码小写」这条老口径（保证 key 合法、
    能出包）；但要**说清原因和改法**，否则用户只看到一条拼音 key 会以为是 bug。
    """
    data = _cai_gou_sheets()
    data[S.S_VALUE] = [{"对象编码": "zzBuCunZai"}]
    proj, errors = project_of(data)
    assert not errors, errors
    notes = [n for n in (getattr(proj, "auto_notes", []) or []) if "选择器" in n]
    assert any("找不到对象 zzBuCunZai 的名称" in n for n in notes), notes
    assert any("key 用对象编码生成" in n for n in notes), notes
    # 退回的是「编码小写」，不是把编码当名字再翻一遍（zz_bu_cun_zai 那种拐法）
    assert [d.key for d in proj.datasets] == ["zzbucunzai_value"], \
        [d.key for d in proj.datasets]

    body = webui.build_zip(xlsx(data))[0]
    values = _value_entries(body)
    # 那张行照出（key 是编码小写）；本对象的自身那份照旧存在（两个不同 key）
    assert "zzbucunzai_value" in values, sorted(values)


@case("VAL23", "value", "用户手填的 key 一律不动：往返一圈不重新生成、不翻档", "P0",
      ["显式配置优先", "确认版 Excel 读回 key 还是那一个"])
def _():
    """key 是我们生成的，但**用户填了就以他的为准**（这条从没变过）。

    新口径只影响「留空自动生成」那一档：显式写的 key（哪怕是老版本的拼音 key）
    读回来还是它，再出包也不会多出一份英文的。否则存量确认版 Excel 一升级就
    翻档，用户会以为配置丢了。
    """
    data = _cai_gou_sheets()
    data[S.S_VALUE] = [{"值来源key": "penny_value", "对象编码": "cai_gou",
                        "名称": "我自己的名字", "返回字段": "*"}]
    body = webui.build_zip(xlsx(data))[0]
    values = _value_entries(body)
    # 手填的那份 key / 名称原样；本对象的「自身那份」按新口径另出一份（两个 key
    # 不同，不冲突）——这条从老版本起就是如此：只有 key 撞上才复用。
    assert "penny_value" in values, sorted(values)
    assert 'name="我自己的名字"' in values["penny_value"]
    assert "purchase_apply_value" in values, sorted(values)

    # 往返：确认版 Excel → 再出包，还是这两份（不翻档、不新增）
    confirm = _read_via_confirm(xlsx(data))
    rows = [r for r in read_xlsx_rows(confirm, S.S_VALUE)
            if str(r.get("值来源key") or "").strip()
            and not str(r.get("值来源key")).startswith("#")]
    assert [r["值来源key"] for r in rows] == ["penny_value", "purchase_apply_value"], rows
    assert sorted(_value_entries(webui.build_zip(confirm)[0])) == sorted(values)


@case("VAL24", "value", "驼峰对象编码 caiGou：key 是英文，datasetKey / boCode / dataSet 仍是编码原样", "P0",
      ["用户 2026-11-20：key 不要拼音", "datasetKey 与 key 是两回事",
       "dataSet 的 key/from 跟 datasetKey 走"])
def _():
    """对象编码可以带大写（``caiGou`` 这类历史编码），key 只允许小写英文 slug。

    最容易被顺手改错的一处就是「既然 key 变英文了，datasetKey 是不是也跟着变」——
    不是。datasetKey / boCode 是「这份选择器取哪个对象的数据」，必须写对象编码
    （驼峰也原样保留）；底下 ``<dataSet>`` 的 key / from 与它是同一个值。
    """
    data = {
        S.S_BO: [{"对象名称": "采购申请", "对象编码": "caiGou",
                  "物理表名": "pp_cai_gou"}],
        S.S_FIELD: [field("caiGou", "申请单号", "字符串"),
                    field("caiGou", "申请金额", "数字", 精度=2)],
        S.S_FORM: [{"表单key": "create_caigou", "表单名称": "新建采购申请",
                    "对象编码": "caiGou", "表单类型": "新建表单"}],
        S.S_FORMFIELD: [{"表单key": "create_caigou", "字段编码": "shenQingDanHao",
                         "显示标签": "申请单号", "控件类型": "text"}],
    }
    body = webui.build_zip(xlsx(data))[0]
    values = _value_entries(body)
    assert sorted(values) == ["purchase_apply_value"], sorted(values)

    xml = values["purchase_apply_value"]
    head = re.search(r"<dataTemplateDef\b[^>]*>", xml)
    got = dict(re.findall(r'([\w:.-]+)="([^"]*)"', head.group(0)))
    assert got["key"] == "purchase_apply_value", got
    assert got["datasetKey"] == got["boCode"] == "caiGou", got       # 驼峰原样
    assert got["name"] == "采购申请值来源", got

    ds = re.search(r"<dataSet\b[^>]*>", xml)
    ds_attrs = dict(re.findall(r'([\w:.-]+)="([^"]*)"', ds.group(0)))
    assert ds_attrs["key"] == "caiGou", ds_attrs
    assert ds_attrs["from"] == "pp_cai_gou", ds_attrs


@case("VAL25", "value", "批量五个对象（编码全是拼音）：key 全是英文，且没有一个 key 以对象编码开头", "P0",
      ["用户 2026-11-20：不要拼音 key", "key != datasetKey", "编码只做兜底"])
def _():
    """五个对象一次出包，编码**故意用拼音**（真实系统里的历史编码就长这样）。

    判据两条，都是「全包扫一遍」而不是抽查：key 不得以对象编码开头（那就是
    老的 ``caigou_value`` 口径又回来了），且 key 与 datasetKey 不得相等。
    """
    pairs = [("项目", "xiangmu"), ("合同", "hetong"), ("项目阶段", "xiangmujieduan"),
             ("需求", "xuqiu"), ("测试用例", "ceshiyongli")]
    data = {
        S.S_BO: [{"对象名称": name, "对象编码": code, "物理表名": f"pp_{code}"}
                 for name, code in pairs],
        S.S_FIELD: [field(code, "名称", "字符串") for _name, code in pairs],
        S.S_FORM: [{"表单key": "create_" + code, "表单名称": "新建" + name,
                    "对象编码": code, "表单类型": "新建表单"}
                   for name, code in pairs],
        S.S_FORMFIELD: [{"表单key": "create_" + code, "字段编码": "name",
                         "显示标签": "名称", "控件类型": "text"}
                        for _name, code in pairs],
    }
    body = webui.build_zip(xlsx(data))[0]
    values = _value_entries(body)
    assert sorted(values) == ["contract_value", "project_phase_value", "project_value",
                             "requirement_value", "test_case_value"], sorted(values)

    for key, xml in values.items():
        head = re.search(r"<dataTemplateDef\b[^>]*>", xml)
        got = dict(re.findall(r'([\w:.-]+)="([^"]*)"', head.group(0)))
        assert got["key"] == key, (key, got["key"])
        assert got["key"] != got["datasetKey"], (key, got)
        for _name, code in pairs:
            assert got["datasetKey"] == code or not key.lower().startswith(code.lower()), \
                (key, code)
            assert not key.lower().startswith(code.lower()), (key, code)

    # 编码与 key 的对应用一张表钉住（编码原样进 datasetKey）
    want = {"project_value": "xiangmu", "contract_value": "hetong",
            "project_phase_value": "xiangmujieduan", "requirement_value": "xuqiu",
            "test_case_value": "ceshiyongli"}
    for key, code in want.items():
        xml = values[key]
        assert f'datasetKey="{code}"' in xml, (key, code, xml[:300])


@case("VAL26", "value", "存量包不翻档：老确认版里 自动=是 的拼音 key 沿用，清空 key 才换成英文", "P0",
      ["存量包铁律：不翻档、不加第二份", "往返两轮 key 集合稳定",
       "key 留空 = 按新口径重新生成"])
def _():
    """老版本的确认版 Excel 里，自动生成的那行 key 是 ``caigou_value``（拼音）。

    用户升级后**重新上传这份确认版**，包里必须还是那一个 key —— 翻成
    ``purchase_apply_value`` 等于把设计器里已经接好的控件引用全断掉，
    而且会多出一条同名选择器。想换英文 key 的正确做法是「把那一行的
    key 清空」（留空 = 按新口径重新生成）。
    """
    legacy_cols = ["id_", "name_", "shen_qing_dan_hao_", "shen_qing_jin_e_",
                   "jin_ji_cheng_du_", "bei_zhu_"]

    def legacy(code: str, value_key: str | None) -> dict:
        data = _cai_gou_sheets()
        data[S.S_BO] = [{"对象名称": "采购申请", "对象编码": code,
                         "物理表名": "pp_cai_gou"}]
        for row in data[S.S_FIELD]:
            row["对象编码"] = code
        data[S.S_FORM] = [{"表单key": "create_caigou", "表单名称": "新建采购申请",
                           "对象编码": code, "表单类型": "新建表单"}]
        row = {"名称": "采购申请值来源", "对象编码": code, "返回字段": "*", "自动": "是"}
        if value_key:
            row["值来源key"] = value_key
        data[S.S_VALUE] = [row]
        return data

    # ① 老确认版 Excel（拼音 key + 自动=是）→ 包里只有那一个，没有英文那份
    for code in ("cai_gou", "caiGou"):
        body = webui.build_zip(xlsx(legacy(code, "caigou_value")))[0]
        values = _value_entries(body)
        assert sorted(values) == ["caigou_value"], (code, sorted(values))
        assert "purchase_apply_value" not in values, (code, sorted(values))
        # 返回列与 Excel 里那行一致（返回字段=*）
        assert _columns(values["caigou_value"]) == legacy_cols, \
            (code, _columns(values["caigou_value"]))
        assert f'key="caigou_value"' in values["caigou_value"], code

        # ② 把那一行的 key 清空 → 变成英文那一个（一条，不是两条）
        body = webui.build_zip(xlsx(legacy(code, None)))[0]
        values = _value_entries(body)
        assert sorted(values) == ["purchase_apply_value"], (code, sorted(values))

    # ③ 往返：确认版 Excel 读回再出包，两轮 key 集合都稳定
    src = xlsx(legacy("cai_gou", "caigou_value"))
    first = sorted(_value_entries(webui.build_zip(src)[0]))
    confirm = _read_via_confirm(src)
    rows = [r for r in read_xlsx_rows(confirm, S.S_VALUE)
            if str(r.get("值来源key") or "").strip()
            and not str(r.get("值来源key")).startswith("#")]
    assert [r["值来源key"] for r in rows] == ["caigou_value"], rows
    assert [r.get("自动") for r in rows] == ["是"], rows
    second = sorted(_value_entries(webui.build_zip(confirm)[0]))
    third = sorted(_value_entries(webui.build_zip(_read_via_confirm(confirm))[0]))
    assert first == second == third == ["caigou_value"], (first, second, third)


@case("VAL27", "value", "词典缺词的对象（设备点检记录）：key 退拼音，提示说清是「点检 / dianjian」", "P0",
      ["不静默：拼音 key 必须能解释", "naming.pinyin_parts 与 key 同一份归一"])
def _():
    """「设备点检」不在英文词典里 → key 只能是 ``equipment_dianjian_value``。

    这条钉的不是「退拼音」本身（那是兜底），而是**提示要说清哪个词、什么拼音、
    去哪补**——否则用户看到 ``dianjian`` 只会觉得是我们写错了。
    """
    data = {
        S.S_BO: [{"对象名称": "设备点检记录", "对象编码": "sbDianJian",
                  "物理表名": "pp_sb_dian_jian"}],
        S.S_FIELD: [field("sbDianJian", "名称", "字符串")],
        S.S_FORM: [{"表单key": "create_dianjian", "表单名称": "新建设备点检",
                    "对象编码": "sbDianJian", "表单类型": "新建表单"}],
        S.S_FORMFIELD: [{"表单key": "create_dianjian", "字段编码": "name",
                         "显示标签": "名称", "控件类型": "text"}],
    }
    body = webui.build_zip(xlsx(data))[0]
    values = _value_entries(body)
    assert sorted(values) == ["equipment_dianjian_value"], sorted(values)
    # 「记录」是 _NOISE 尾巴，不参与命名，也不该被算成兜底
    assert 'datasetKey="sbDianJian"' in values["equipment_dianjian_value"]

    note = _notice(body)
    assert "未收录英文词典" in note, note[-800:]
    assert "已用拼音" in note and "dianjian" in note, note[-800:]
    assert "「点检」" in note, note[-800:]
    assert "naming.WORDS" in note, note[-800:]


@case("VAL28", "value", "查不到对象名称的提示在「解析页」也看得到（不只 CLI 打印）", "P1",
      ["用户在设计器里就能知道 key 为什么是编码",
       "parse_excel 的 notes 与 reader.auto_notes 同源"])
def _():
    """6.1_值来源 只填编码（包内 / 系统清单都没有）时，key 只能按编码生成。

    reader 把原因记进 ``auto_notes``，出包报告与 CLI 都会打；但用户实际是在
    **设计器的解析页**看提示的——那条链路走 ``webui.parse_excel``，这里要确认
    它把同一句带出来了，否则用户在界面上只看到一条拼音 key，没有任何解释。
    """
    data = _cai_gou_sheets()
    data[S.S_VALUE] = [{"对象编码": "zzBuCunZai"}]
    view = webui.parse_excel(xlsx(data))
    assert view["ok"] and view["errorCount"] == 0, view["issues"]
    notes = [n for n in view["notes"] if "zzBuCunZai" in n]
    assert any("找不到对象 zzBuCunZai 的名称" in n for n in notes), view["notes"]
    assert any("key 用对象编码生成" in n for n in notes), view["notes"]
    assert any(d["key"] == "zzbucunzai_value" for d in view["datasets"]), view["datasets"]
