"""Excel 填写表 → 内存模型：解析层的容错与自动补全。

守的是「用户乱填也别崩、填得对就别多嘴」。这一层出的错会一路带到 XML，
所以在进生成器之前就要拦住。
"""
from __future__ import annotations

from pp6kit import spec
from pp6kit import webui
from tests._harness import case, project_of, xlsx


def parse(sheets: dict) -> dict:
    return webui.parse_excel(xlsx(sheets))


def errors_of(result: dict) -> list[str]:
    return [i["message"] for i in result.get("issues", []) if i["level"] == "error"]


def warnings_of(result: dict) -> list[str]:
    return [i["message"] for i in result.get("issues", []) if i["level"] == "warn"]


def bo(**kw) -> dict:
    base = {"对象类型": "object", "记录变更日志": "是", "流程驱动": "否", "关系类型": "PARENT"}
    base.update(kw)
    return base


def field(bo_code: str, name: str, type_: str, **kw) -> dict:
    base = {"对象编码": bo_code, "字段名称": name, "数据类型": type_,
            "允许空": "是", "建索引": "否"}
    base.update(kw)
    return base


@case("RDR01", "reader", "最小填写表能解析出对象与字段", "P0",
      ["1 对象 2 字段", "tbl 自动补 pp_"])
def _():
    r = parse({
        spec.S_BO: [bo(**{"对象名称": "项目", "对象编码": "project"})],
        spec.S_FIELD: [field("project", "名称", "字符串"), field("project", "金额", "数字")],
    })
    assert r["ok"], r.get("error")
    assert len(r["objects"]) == 1
    obj = r["objects"][0]
    assert obj["code"] == "project" and obj["tbl"] == "pp_project"
    assert [f["name"] for f in obj["fields"]] == ["名称", "金额"]


@case("RDR02", "reader", "对象编码留空按名称自动生成", "P0",
      ["项目立项→project_create", "note 里告知已生成"])
def _():
    r = parse({
        spec.S_BO: [bo(**{"对象名称": "项目立项"})],
        spec.S_FIELD: [field("project_create", "名称", "字符串")],
    })
    assert r["objects"][0]["code"] == "project_create"
    assert any("自动生成" in n for n in r["notes"]), r["notes"]


@case("RDR03", "reader", "物理表名留空自动补 pp_ 前缀", "P0",
      ["不再逼用户手填", "note 里告知"])
def _():
    r = parse({
        spec.S_BO: [bo(**{"对象名称": "风险登记", "对象编码": "risk_register"})],
        spec.S_FIELD: [field("risk_register", "名称", "字符串")],
    })
    assert r["objects"][0]["tbl"] == "pp_risk_register"
    assert any("物理表名" in n for n in r["notes"]), r["notes"]


@case("RDR04", "reader", "对象编码重复被判定为错误", "P0",
      ["否则后一条会静默覆盖前一条"])
def _():
    r = parse({
        spec.S_BO: [bo(**{"对象名称": "项目", "对象编码": "project"}),
                    bo(**{"对象名称": "项目2", "对象编码": "project"})],
        spec.S_FIELD: [field("project", "名称", "字符串")],
    })
    assert any("对象编码重复" in m for m in errors_of(r)), errors_of(r)


@case("RDR05", "reader", "字段编码留空按名称自动生成", "P0",
      ["申请金额→shenQingJinE", "与界面添加字段的驼峰一致"])
def _():
    r = parse({
        spec.S_BO: [bo(**{"对象名称": "项目", "对象编码": "project"})],
        spec.S_FIELD: [field("project", "申请金额", "数字")],
    })
    assert r["objects"][0]["fields"][0]["code"] == "shenQingJinE"


@case("RDR06", "reader", "字段序号留空按行序连续编号", "P1",
      ["sn 从 1 开始", "不跳号"])
def _():
    proj, _, _ = project_of(xlsx({
        spec.S_BO: [bo(**{"对象名称": "项目", "对象编码": "project"})],
        spec.S_FIELD: [field("project", "甲", "字符串"), field("project", "乙", "字符串"),
                       field("project", "丙", "字符串")],
    }))
    sns = [f.sn for f in proj.bos[0].fields]
    assert sns == [1, 2, 3], f"实际 {sns}"


@case("RDR07", "reader", "字段编码在同一对象内不重复", "P1",
      ["同名中文→同一编码时自动错开"])
def _():
    proj, _, _ = project_of(xlsx({
        spec.S_BO: [bo(**{"对象名称": "项目", "对象编码": "project"})],
        spec.S_FIELD: [field("project", "名称", "字符串"), field("project", "名称", "字符串")],
    }))
    codes = [f.code for f in proj.bos[0].fields]
    assert len(codes) == len(set(codes)), f"编码重复：{codes}"


@case("RDR08", "reader", "未知数据类型不中断，按字符串生成并提示", "P1",
      ["不抛异常", "warn 里说明已降级"])
def _():
    r = parse({
        spec.S_BO: [bo(**{"对象名称": "项目", "对象编码": "project"})],
        spec.S_FIELD: [field("project", "怪字段", "神仙类型")],
    })
    assert r["ok"], r.get("error")
    assert any("未知数据类型" in m and "字符串" in m
               for m in warnings_of(r)), warnings_of(r)
    # 生成侧按 varchar 落库，具体断言在 BO 模块（落 XML 的那一层）
    assert r["objects"][0]["fields"][0]["length"] == 200


@case("RDR09", "reader", "字段挂错对象时归位到唯一对象并提示", "P1",
      ["单对象场景自动纠正", "必须在提示里说出来"])
def _():
    r = parse({
        spec.S_BO: [bo(**{"对象名称": "项目", "对象编码": "project"})],
        spec.S_FIELD: [field("nope", "孤儿字段", "字符串")],
    })
    assert len(r["objects"][0]["fields"]) == 1, "字段被静默丢弃了"
    assert any("已自动归到唯一对象" in n for n in r["notes"]), r["notes"]


@case("RDR10", "reader", "多对象时挂错对象必须报错", "P0",
      ["无法判断该归到谁", "静默归位会挂错对象"])
def _():
    r = parse({
        spec.S_BO: [bo(**{"对象名称": "项目", "对象编码": "project"}),
                    bo(**{"对象名称": "需求类型", "对象编码": "requirement_type"})],
        spec.S_FIELD: [field("project", "名称", "字符串"),
                       field("nope", "孤儿字段", "字符串")],
    })
    assert any("不存在" in m for m in errors_of(r)), errors_of(r)


@case("RDR11", "reader", "选项值支持逗号与换行两种写法", "P0",
      ["进行中,已结束", "Alt+Enter 换行"])
def _():
    r = parse({
        spec.S_BO: [bo(**{"对象名称": "项目", "对象编码": "project"})],
        spec.S_FIELD: [
            field("project", "状态", "选项", 选项值="进行中,已结束"),
            field("project", "级别", "选项", 选项值="高\n中\n低"),
        ],
    })
    got = {f["name"]: f for f in r["objects"][0]["fields"]}
    assert got["状态"]["type"] == "dic"
    # parse 的 objects 快照不带 choices，改看 Project 里的真实解析
    proj, _, _ = project_of(xlsx({
        spec.S_BO: [bo(**{"对象名称": "项目", "对象编码": "project"})],
        spec.S_FIELD: [field("project", "状态", "选项", 选项值="进行中,已结束"),
                       field("project", "级别", "选项", 选项值="高\n中\n低")],
    }))
    choices = {f.name: f.choices for f in proj.bos[0].fields}
    assert choices["状态"] == ["进行中", "已结束"], choices
    assert choices["级别"] == ["高", "中", "低"], choices


@case("RDR12", "reader", "「标准全套」展开成新建/详情/条件三张表单", "P0",
      ["一条配置出三张", "key 按规范命名"])
def _():
    r = parse({
        spec.S_BO: [bo(**{"对象名称": "项目", "对象编码": "project"})],
        spec.S_FIELD: [field("project", "名称", "字符串")],
        spec.S_FORM: [{"表单key": "bundle_x", "表单名称": "项目表单",
                       "对象编码": "project", "表单类型": "标准全套"}],
        spec.S_FORMFIELD: [{"表单key": "bundle_x", "字段编码": "mingCheng",
                            "显示标签": "名称", "控件类型": "text"}],
    })
    kinds = sorted(f["kind"] for f in r["forms"])
    assert kinds == ["condition", "create", "detail"], kinds
    keys = {f["key"] for f in r["forms"]}
    assert keys == {"create_project", "detail_project",
                    "fetch_basic_project_information"}, keys
    assert any("标准全套" in n for n in r["notes"]), r["notes"]


@case("RDR13", "reader", "列表配置能解析出视图与显示字段", "P1",
      ["grid,board", "columns 逗号分隔"])
def _():
    r = parse({
        spec.S_BO: [bo(**{"对象名称": "项目", "对象编码": "project"})],
        spec.S_FIELD: [field("project", "名称", "字符串"), field("project", "金额", "数字")],
        spec.S_LIST: [{"列表key": "project_list", "列表名称": "项目列表",
                       "对象编码": "project", "视图类型": "grid,board",
                       "显示字段": "mingCheng,jinE"}],
    })
    assert r["lists"], "列表没解析出来"
    item = r["lists"][0]
    assert item["key"] == "project_list"


@case("RDR14", "reader", "数据集缺主表名要报错", "P1", ["主表是取数根，不能空"])
def _():
    r = parse({
        spec.S_BO: [bo(**{"对象名称": "项目", "对象编码": "project"})],
        spec.S_FIELD: [field("project", "名称", "字符串")],
        spec.S_DATASET: [{"数据集key": "ds_x", "数据集名称": "取项目",
                          "主表名": "", "类型": "valueSource"}],
    })
    assert any("主表名" in m for m in errors_of(r)), errors_of(r)


@case("RDR15", "reader", "隐藏规则能解析出条件与清空开关", "P1",
      ["等于/不等于/为空/不为空", "clear_on_hide"])
def _():
    proj, _, _ = project_of(xlsx({
        spec.S_BO: [bo(**{"对象名称": "项目", "对象编码": "project"})],
        spec.S_FIELD: [field("project", "状态", "选项", 选项值="进行中,已结束"),
                       field("project", "原因", "字符串")],
        spec.S_FORM: [{"表单key": "create_project", "表单名称": "新建项目",
                       "对象编码": "project", "表单类型": "新建表单"}],
        spec.S_FORMFIELD: [
            {"表单key": "create_project", "字段编码": "zhuangTai",
             "显示标签": "状态", "控件类型": "dictionary", "取值来源": "project_zhuangTai"},
            {"表单key": "create_project", "字段编码": "yuanYin",
             "显示标签": "原因", "控件类型": "text"},
        ],
        spec.S_HIDE: [{"表单key": "create_project", "隐藏控件": "yuanYin",
                       "依赖控件": "zhuangTai", "判断条件": "等于",
                       "条件值": "已结束", "隐藏时清空": "否"}],
    }))
    rules = proj.forms[0].hide_rules
    assert len(rules) == 1, rules
    rule = rules[0]
    assert rule.target == "yuanYin" and rule.source == "zhuangTai"
    assert rule.value == "已结束"
    assert rule.clear_on_hide is False


@case("RDR16", "reader", "项目设置覆盖默认租户与前缀", "P1",
      ["租户ID", "文件前缀"])
def _():
    proj, _, _ = project_of(xlsx({
        spec.S_SETTINGS: [{"配置项": "租户ID", "值": "42"},
                          {"配置项": "文件前缀", "值": "007"}],
        spec.S_BO: [bo(**{"对象名称": "项目", "对象编码": "project"})],
        spec.S_FIELD: [field("project", "名称", "字符串")],
    }))
    assert proj.tenant_id == "42"
    assert proj.file_prefix == "007"


@case("RDR17", "reader", "缺少可选 sheet 不报错", "P2",
      ["只有业务对象和字段也能出包"])
def _():
    r = parse({
        spec.S_BO: [bo(**{"对象名称": "项目", "对象编码": "project"})],
        spec.S_FIELD: [field("project", "名称", "字符串")],
    })
    assert r["ok"], r.get("error")
    assert not errors_of(r), errors_of(r)


@case("RDR18", "reader", "空行不产生空对象", "P2", ["尾部空行常被 Excel 带出来"])
def _():
    r = parse({
        spec.S_BO: [bo(**{"对象名称": "项目", "对象编码": "project"}), {}],
        spec.S_FIELD: [field("project", "名称", "字符串"), {}],
    })
    assert len(r["objects"]) == 1, [o["code"] for o in r["objects"]]


@case("RDR19", "reader", "以 # 开头的行按注释跳过", "P2",
      ["方便在模板里写说明", "不产生对象"])
def _():
    r = parse({
        spec.S_BO: [{"对象编码": "# 这一行是说明，不是对象"},
                    bo(**{"对象名称": "项目", "对象编码": "project"})],
        spec.S_FIELD: [field("project", "名称", "字符串")],
    })
    assert len(r["objects"]) == 1, [o["code"] for o in r["objects"]]


@case("RDR20", "reader", "缺少必需列直接报出来", "P1",
      ["列名被改过时能一眼看出"])
def _():
    from openpyxl import Workbook
    import io

    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet(spec.S_BO)          # 只写「对象编码」一列
    ws.append(["对象编码"])
    ws.append(["project"])
    buf = io.BytesIO()
    wb.save(buf)
    r = webui.parse_excel(buf.getvalue())
    text = " ".join(errors_of(r)) + str(r.get("error") or "")
    assert "缺少必需列" in text or "对象名称" in text, text


@case("RDR21", "reader", "文本框里的数字也能当精度用", "P2",
      ["Excel 里 2 常被读成 '2'"])
def _():
    proj, _, _ = project_of(xlsx({
        spec.S_BO: [bo(**{"对象名称": "项目", "对象编码": "project"})],
        spec.S_FIELD: [field("project", "金额", "数字", 精度="2", 长度="20")],
    }))
    f = proj.bos[0].fields[0]
    assert f.precision == 2 and f.length == 20, (f.precision, f.length)


@case("RDR22", "reader", "流程驱动对象自动补 6 个流程字段", "P1",
      ["流程定义/实例/节点/处理人/名称/编号", "和线上 pp_project_create 一致"])
def _():
    from pp6kit import builder

    proj, _, _ = project_of(xlsx({
        spec.S_BO: [bo(**{"对象名称": "项目立项", "对象编码": "project_create",
                          "流程驱动": "是"})],
        spec.S_FIELD: [field("project_create", "名称", "字符串")],
    }))
    added = builder.ensure_process_fields(proj.bos[0], ["开始", "审核", "通过"])
    codes = {f.code for f in proj.bos[0].fields}
    assert len(added) == 6, added
    assert {"processDefId", "processInstanceId", "processNode",
            "processHandler", "processName", "processCode"} <= codes
    node = proj.bos[0].field_by_code("processNode")
    assert node.type_new == "dic" and node.length == 1000
    assert node.choices == ["开始", "审核", "通过"]


@case("RDR23", "reader", "同一次解析里对象编码自动去重", "P2",
      ["两个「项目」不会都叫 project"])
def _():
    r = parse({
        spec.S_BO: [bo(**{"对象名称": "项目"}), bo(**{"对象名称": "项目"})],
        spec.S_FIELD: [field("project", "名称", "字符串")],
    })
    codes = [o["code"] for o in r["objects"]]
    assert len(codes) == 2
    assert len(set(codes)) == 2, f"编码没去重：{codes}"


@case("RDR24", "reader", "「关联项目」列：填是才勾、留空/老模板按否、非法值交给校验",
      "P0", ["缺列容忍", "留空=零变化", "非法值按否"])
def _():
    yes = parse({
        spec.S_BO: [bo(**{"对象名称": "合同", "对象编码": "contract", "关联项目": "是"})],
        spec.S_FIELD: [field("contract", "合同编号", "字符串")],
    })
    assert yes["objects"][0]["projectLink"] is True

    # 老模板没有这一列 → get() 返回 None → 否（产物与改造前逐字节一致）
    blank = parse({
        spec.S_BO: [bo(**{"对象名称": "合同", "对象编码": "contract"})],
        spec.S_FIELD: [field("contract", "合同编号", "字符串")],
    })
    assert blank["objects"][0]["projectLink"] is False

    # 填了认不出的值：按否处理，并让 validate 明说（不静默吞掉）
    weird = parse({
        spec.S_BO: [bo(**{"对象名称": "合同", "对象编码": "contract", "关联项目": "可能"})],
        spec.S_FIELD: [field("contract", "合同编号", "字符串")],
    })
    assert weird["objects"][0]["projectLink"] is False
    msgs = warnings_of(weird)
    assert any("按「否」处理" in m for m in msgs), msgs
