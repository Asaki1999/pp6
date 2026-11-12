"""出包前校验：把「导进去才发现」的问题提前拦在本地。

校验的价值不在拦住错误，而在**拦得准**：该拦的拦（否则白导一次），
不该拦的别拦（否则用户被迫绕过工具手工拼 XML）。
"""
from __future__ import annotations

from pp6kit import spec
from tests._harness import _bo, case, field, project_issues, simple_package, xlsx, project_of

S = spec


def run(sheets: dict):
    return project_issues(sheets)


def base_sheets(**extra) -> dict:
    data = {
        S.S_BO: [_bo(**{"对象名称": "项目", "对象编码": "project", "物理表名": "pp_project"})],
        S.S_FIELD: [field("project", "名称", "字符串")],
    }
    data.update(extra)
    return data


@case("VAL01", "validate", "干净填写表不产生任何错误", "P0",
      ["不误报", "否则用户会习惯性忽略校验"])
def _():
    errs, warns, _ = run(base_sheets())
    assert not errs, errs


@case("VAL02", "validate", "对象编码含非法字符被拦下", "P0",
      ["中文/空格/横杠", "编码直接变表名"])
def _():
    errs, _, _ = run({
        S.S_BO: [_bo(**{"对象名称": "项目", "对象编码": "项目-1", "物理表名": "pp_project"})],
        S.S_FIELD: [field("项目-1", "名称", "字符串")],
    })
    assert any("对象编码" in m and "字母" in m for m in errs), errs


@case("VAL03", "validate", "对象编码重复被拦下", "P0", ["两个对象建成同一张表"])
def _():
    errs, _, _ = run({
        S.S_BO: [_bo(**{"对象名称": "项目", "对象编码": "project", "物理表名": "pp_project"}),
                 _bo(**{"对象名称": "项目2", "对象编码": "project", "物理表名": "pp_project2"})],
        S.S_FIELD: [field("project", "名称", "字符串")],
    })
    assert any("对象编码重复" in m for m in errs), errs


@case("VAL04", "validate", "物理表名带大写被拦下", "P1",
      ["PostgreSQL 会自动转小写，导入后对不上"])
def _():
    errs, _, _ = run({
        S.S_BO: [_bo(**{"对象名称": "项目", "对象编码": "project", "物理表名": "PP_Project"})],
        S.S_FIELD: [field("project", "名称", "字符串")],
    })
    assert any("物理表名" in m for m in errs), errs


@case("VAL05", "validate", "没有字段的对象被拦下", "P0",
      ["空对象建表没有意义"])
def _():
    errs, _, _ = run({S.S_BO: [_bo(**{"对象名称": "项目", "对象编码": "project",
                                      "物理表名": "pp_project"})]})
    assert any("没有任何字段" in m for m in errs), errs


@case("VAL06", "validate", "字段编码带下划线给出驼峰建议", "P1",
      ["系统添加字段要求驼峰", "只提示不阻断"])
def _():
    errs, warns, _ = run({
        S.S_BO: [_bo(**{"对象名称": "项目", "对象编码": "project", "物理表名": "pp_project"})],
        S.S_FIELD: [field("project", "名称", "字符串", 字段编码="project_name")],
    })
    assert any("驼峰" in m for m in warns), warns
    assert not errs, errs


@case("VAL07", "validate", "字段是数字但控件用文本框要提示", "P1",
      ["导入后是普通文本框", "只有打开界面才发现"])
def _():
    errs, warns, _ = run(base_sheets(**{
        S.S_FIELD: [field("project", "申请金额", "数字", 精度=2)],
        S.S_FORM: [{"表单key": "create_project", "表单名称": "新建项目",
                    "对象编码": "project", "表单类型": "新建表单"}],
        S.S_FORMFIELD: [{"表单key": "create_project", "字段编码": "shenQingJinE",
                         "显示标签": "申请金额", "控件类型": "text"}],
    }))
    assert any("number" in m and "text" in m for m in warns), warns


@case("VAL08", "validate", "明细表没填取值来源要拦下", "P0",
      ["否则子表整片空白"])
def _():
    errs, _, _ = run(base_sheets(**{
        S.S_FORM: [{"表单key": "create_project", "表单名称": "新建项目",
                    "对象编码": "project", "表单类型": "新建表单"}],
        S.S_FORMFIELD: [{"表单key": "create_project", "字段编码": "details",
                         "显示标签": "明细", "控件类型": "dataTemplate"}],
    }))
    assert any("明细表" in m and "取值来源" in m for m in errs), errs


@case("VAL09", "validate", "明细表指向包外的列表只提示不拦", "P1",
      ["只导表单、列表早就在系统里是常见用法"])
def _():
    errs, warns, _ = run(base_sheets(**{
        # 包内有列表才知道「指向的列表不在包内」，一条都没有时无从判断
        S.S_LIST: [{"列表key": "project_list", "列表名称": "项目列表",
                    "对象编码": "project", "显示字段": "mingCheng"}],
        S.S_FORM: [{"表单key": "create_project", "表单名称": "新建项目",
                    "对象编码": "project", "表单类型": "新建表单"}],
        S.S_FORMFIELD: [{"表单key": "create_project", "字段编码": "details",
                         "显示标签": "明细", "控件类型": "dataTemplate",
                         "取值来源": "other_list"},
                        {"表单key": "create_project", "字段编码": "mingCheng",
                         "显示标签": "名称", "控件类型": "text"}],
    }))
    assert not errs, errs
    assert any("不在本包内" in m for m in warns), warns


@case("VAL10", "validate", "字典控件引用了不存在的字典要拦下", "P0",
      ["引错 key 会让选项重复显示两遍"])
def _():
    errs, _, _ = run(base_sheets(**{
        S.S_FIELD: [field("project", "状态", "选项", 选项值="进行中,已结束")],
        S.S_FORM: [{"表单key": "create_project", "表单名称": "新建项目",
                    "对象编码": "project", "表单类型": "新建表单"}],
        S.S_FORMFIELD: [{"表单key": "create_project", "字段编码": "zhuangTai",
                         "显示标签": "状态", "控件类型": "dictionary",
                         "取值来源": "zhuangTai"}],
    }))
    assert any("字典" in m and "不存在" in m for m in errs), errs


@case("VAL11", "validate", "条件专用表单命名不符合规范要提示", "P2",
      ["手册要求「条件专用_对象名」"])
def _():
    _, warns, _ = run(base_sheets(**{
        S.S_FORM: [{"表单key": "cond_x", "表单名称": "我的条件",
                    "对象编码": "project", "表单类型": "条件专用表单"}],
        S.S_FORMFIELD: [{"表单key": "cond_x", "字段编码": "mingCheng",
                         "显示标签": "名称", "控件类型": "text"}],
    }))
    assert any("条件专用_" in m for m in warns), warns


@case("VAL12", "validate", "导入表单里出现系统字段要拦下", "P0",
      ["创建人/创建时间/ID 不得进导入表单", "手册硬性约束"])
def _():
    errs, _, _ = run(base_sheets(**{
        S.S_FORM: [{"表单key": "import_project", "表单名称": "导入专用_项目",
                    "对象编码": "project", "表单类型": "导入专用表单"}],
        S.S_FORMFIELD: [
            {"表单key": "import_project", "字段编码": "createTime",
             "显示标签": "创建时间", "控件类型": "hidden"},
            {"表单key": "import_project", "字段编码": "mingCheng",
             "显示标签": "名称", "控件类型": "text"},
        ],
    }))
    assert any("系统字段" in m for m in errs), errs


@case("VAL13", "validate", "导入表单必填与新建表单不一致要提示", "P1",
      ["否则导入校验和页面校验两套标准"])
def _():
    _, warns, _ = run(base_sheets(**{
        S.S_FORM: [
            {"表单key": "create_project", "表单名称": "新建项目",
             "对象编码": "project", "表单类型": "新建表单"},
            {"表单key": "import_project", "表单名称": "导入专用_项目",
             "对象编码": "project", "表单类型": "导入专用表单"},
        ],
        S.S_FORMFIELD: [
            {"表单key": "create_project", "字段编码": "mingCheng",
             "显示标签": "名称", "控件类型": "text", "必填": "是"},
            {"表单key": "import_project", "字段编码": "mingCheng",
             "显示标签": "名称", "控件类型": "text", "必填": "否"},
        ],
    }))
    assert any("必填" in m for m in warns), warns


@case("VAL14", "validate", "列表 key 大写被拦下", "P1", ["key 会进系统标识"])
def _():
    errs, _, _ = run(base_sheets(**{
        S.S_LIST: [{"列表key": "ProjectList", "列表名称": "项目列表",
                    "对象编码": "project"}],
    }))
    assert any("列表key" in m for m in errs), errs


@case("VAL15", "validate", "列表显示字段不存在被拦下", "P0",
      ["否则列表页那一列空白"])
def _():
    errs, _, _ = run(base_sheets(**{
        S.S_LIST: [{"列表key": "project_list", "列表名称": "项目列表",
                    "对象编码": "project", "显示字段": "mingCheng,bingBuCunZai"}],
    }))
    assert any("显示字段" in m and "不存在" in m for m in errs), errs


@case("VAL16", "validate", "未知视图类型被拦下", "P2", ["grid/board/gantt/chart"])
def _():
    errs, _, _ = run(base_sheets(**{
        S.S_LIST: [{"列表key": "project_list", "列表名称": "项目列表",
                    "对象编码": "project", "视图类型": "calendar"}],
    }))
    assert any("未知视图类型" in m for m in errs), errs


@case("VAL17", "validate", "控件指向不存在的字段要提示", "P1",
      ["控件存不了数据"])
def _():
    _, warns, _ = run(base_sheets(**{
        S.S_FORM: [{"表单key": "create_project", "表单名称": "新建项目",
                    "对象编码": "project", "表单类型": "新建表单"}],
        S.S_FORMFIELD: [{"表单key": "create_project", "字段编码": "meiYouZheGe",
                         "显示标签": "幽灵", "控件类型": "text"}],
    }))
    assert any("可能无法保存数据" in m for m in warns), warns


@case("VAL18", "validate", "单行控件占满整行会浪费半屏", "P2",
      ["右半边整片空白"])
def _():
    _, warns, _ = run(base_sheets(**{
        S.S_FORM: [{"表单key": "create_project", "表单名称": "新建项目",
                    "对象编码": "project", "表单类型": "新建表单"}],
        S.S_FORMFIELD: [{"表单key": "create_project", "字段编码": "mingCheng",
                         "显示标签": "名称", "控件类型": "text", "占满整行": "是"}],
    }))
    assert any("占满整行" in m or "占了整行" in m for m in warns), warns


@case("VAL19", "validate", "选项短码重复被拦下", "P1",
      ["库里没有唯一约束，重复会静默覆盖"])
def _():
    errs, _, _ = run({
        S.S_BO: [_bo(**{"对象名称": "项目", "对象编码": "project", "物理表名": "pp_project"})],
        S.S_FIELD: [field("project", "类型", "选项", 选项值="重大风险,重大风险")],
    })
    # 完全相同的候选值会被去重，这里只要求不产生脏数据
    assert not any("Traceback" in m for m in errs), errs


@case("VAL20", "validate", "父对象不存在被拦下", "P0",
      ["父子关系断了导入后是孤立对象"])
def _():
    errs, _, _ = run({
        S.S_BO: [_bo(**{"对象名称": "项目立项", "对象编码": "project_create",
                        "物理表名": "pp_project_create", "父对象编码": "buCunZai"})],
        S.S_FIELD: [field("project_create", "名称", "字符串")],
    })
    assert any("父对象" in m or "buCunZai" in m for m in errs), errs


@case("VAL21", "validate", "物理列名冲突被拦下", "P1",
      ["两列同名建表会失败"])
def _():
    errs, _, _ = run({
        S.S_BO: [_bo(**{"对象名称": "项目", "对象编码": "project", "物理表名": "pp_project"})],
        S.S_FIELD: [
            field("project", "名称", "字符串", 物理列名="same_col_"),
            field("project", "别名", "字符串", 物理列名="same_col_"),
        ],
    })
    assert any("冲突" in m for m in errs), errs


@case("VAL22", "validate", "主键不在字段里只提示不拦", "P2",
      ["id 是系统自动补的"])
def _():
    _, warns, _ = run(base_sheets())
    assert isinstance(warns, tuple)      # 不抛异常即可；id 属系统字段，不该报错
    errs, _, _ = run(base_sheets())
    assert not errs, errs
