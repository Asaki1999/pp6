"""字段类型字典：中文名 <-> dataTypeNew <-> 存储类型。

系统后台「添加字段」里给的是中文名（字符串/数字/日期时间/大文本/用户/部门/
附件列表/国际化/选项/对象/用户组/表格/自动编号/公式），
而 XML 里写的是 dataTypeNew 与 datatype。这层负责两边互转。

字典放在 assets/fieldtypes.json，可用 `bin/pp6 learn` 从真实导出包校准。
"""
from __future__ import annotations

import json
from functools import lru_cache

from .resources import resource_root

# 未验证类型的兜底存储方式
_FALLBACK = {"datatype": "varchar", "length": 200}

# dataTypeNew -> **字段层**在系统界面上的叫法。
#
# 权威来源：用户从系统「添加字段 › 字段类型」下拉里截图给的 14 项（2026-10-28）：
#     1 Str.字符串 2 数字 3 日期时间 4 大文本 5 用户 6 部门 7 附件列表
#     8 国际化   9 选项 10 对象  11 用户组 12 表格 13 自动编号 14 公式
# 用户标注常用的是 1~6、9、10、12（缺 7/8/11/13/14）。
#
# 两处曾经踩过的坑：
#  1. `dic` 不叫「数据字典」——那是**表单控件层**的叫法，字段层只有「选项」。
#     用户原话：「只有 选项，没有 数据字典」。
#  2. `object` 不叫「下拉框」——字段层叫「对象」，意思是「选择另一个业务对象」。
#     「下拉框」是控件层对 select 控件的叫法。
#  字典 / 数据集同理，都是控件层的取数方式，不是字段类型。
UI_LABELS: dict[str, str] = {
    "varchar": "字符串",
    "number": "数字",
    "date": "日期",
    "datetime": "日期时间",
    "clob": "大文本",
    "user": "用户",
    "department": "部门",
    "attachment": "附件列表",
    "file": "文件",
    "i18n": "国际化",
    "dic": "选项",
    "object": "对象",
    "userGroupCate": "用户组",
    "table": "表格",
    "autoNumber": "自动编号",
    "formula": "公式",
}

# 控件层叫法（表单设计器里才有），只用于读旧文件时做识别，**不往下拉里放**。
UI_LABELS_CTRL: dict[str, str] = {
    "dic": "数据字典",
    "object": "下拉框",
    "varchar": "单行文本",
    "clob": "多行文本",
    "datetime": "日期控件",
}

# 控件名 -> dataTypeNew。用户直接填系统界面上的控件名也能认出来。
CTRL_TO_TYPE: dict[str, str] = {
    "text": "varchar",
    "textarea": "clob",
    "editor": "clob",
    "number": "number",
    "inputNumber": "number",
    "datePicker": "datetime",
    "select": "object",
    "dictionary": "dic",
    "selector": "object",
    "linkdata": "object",
    "dropSelector": "user",
    "dropSelectOrg": "department",
    "dropSelectGroup": "userGroupCate",
    "attachment": "attachment",
    "autoNumber": "autoNumber",
    "formula": "formula",
    "table": "table",
    "hidden": "varchar",
}


@lru_cache(maxsize=1)
def _load() -> dict:
    path = resource_root() / "assets" / "fieldtypes.json"
    if not path.exists():
        return {"types": {}}
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 表格字段的子列
#
# 「表格」字段的子列类型是**另一套下拉**（系统里在表格字段的「编辑列」弹窗里），
# 取值是 inputType，和字段层的 dataTypeNew 同源但不完全同名（没有 attachment/
# object/autoNumber…）。真实样本里出现过的：varchar / number / date / clob / dic。
# 多给了 datetime / user / department —— 它们的 inputType 与字段层完全同名，
# 加进来不会造出系统不认的值。
COLUMN_LABELS: dict[str, str] = {
    "varchar": "字符串",
    "number": "数字",
    "date": "日期",
    "datetime": "日期时间",
    "clob": "大文本",
    "dic": "选项",
    "user": "用户",
    "department": "部门",
}

_COLUMN_ALIASES: dict[str, str] = {
    "文本": "varchar", "单行文本": "varchar", "string": "varchar",
    "多行文本": "clob", "长文本": "clob", "text": "clob",
    "整数": "number", "小数": "number", "numeric": "number",
    "选项值": "dic", "字典": "dic", "数据字典": "dic",
    "人员": "user", "组织": "department", "部门选择": "department",
}


def column_choices() -> list[str]:
    """给 Excel 下拉用的子列类型选项（和字段层一样带括号写代码）。"""
    order = ["varchar", "number", "date", "datetime", "clob", "dic", "user", "department"]
    return [f"{COLUMN_LABELS[c]}（{c}）" for c in order]


def resolve_column_type(value: str) -> str:
    """把「字符串（varchar）」「数字」「number」归一成 inputType。"""
    text = str(value or "").strip()
    if not text:
        return "varchar"
    if "（" in text and text.endswith("）"):
        text = text.split("（")[-1][:-1].strip()
    if text in COLUMN_LABELS:
        return text
    for code, label in COLUMN_LABELS.items():
        if text == label:
            return code
    return _COLUMN_ALIASES.get(text, text)



def all_types() -> dict[str, dict]:
    return _load().get("types", {})


def specs() -> dict[str, dict]:
    """dataTypeNew -> 生成参数（datatype/length/format）。"""
    out = {}
    for code, cfg in all_types().items():
        out[code] = {
            "datatype": cfg.get("datatype", _FALLBACK["datatype"]),
            "length": cfg.get("length"),
            "format": cfg.get("format"),
        }
    return out


def labels() -> dict[str, str]:
    """dataTypeNew -> 中文名。"""
    return {code: cfg.get("label", code) for code, cfg in all_types().items()}


def choices() -> list[str]:
    """给 Excel 下拉用的选项：用**字段层的叫法**，用户不用做翻译。

    顺序照系统下拉（用户截图的 14 项），常填的排在前面：
    字符串 / 数字 / 日期时间 / 大文本 / 用户 / 部门 / 附件列表 / 国际化 /
    选项 / 对象 / 用户组 / 表格 / 自动编号 / 公式
    """
    order = ["varchar", "number", "date", "datetime", "clob", "user", "department",
             "attachment", "file", "i18n", "dic", "object", "userGroupCate", "table",
             "autoNumber", "formula"]
    return [f"{UI_LABELS[c]}（{c}）" for c in order]


def ui_label(code: str) -> str:
    """dataTypeNew -> 字段层叫法（字符串/数字/选项/对象…）。"""
    return UI_LABELS.get(code) or labels().get(code, code)


def resolve(value: str) -> str:
    """把用户填的内容归一成 dataTypeNew。

    支持四种写法，用户从下拉选、或者手抄系统叫法都能认：
        字符串（varchar）      ← 下拉选项的格式
        字符串                 ← 只填中文（字段层叫法）
        单行文本               ← 控件层叫法，旧表格里这么写也认
        varchar / text        ← 代码或控件名
    """
    text = str(value or "").strip()
    if not text:
        return "varchar"
    if "（" in text and text.endswith("）"):
        text = text.split("（")[-1][:-1].strip()

    table = all_types()
    if text in table:
        return text

    # 中文名（字段层叫法）
    for code, label in UI_LABELS.items():
        if text == label:
            return code
    # 控件层叫法：旧模板里写的是「数据字典」「下拉框」「单行文本」，读进来还认，
    # 但生成出去一律写字段层的「选项」「对象」「字符串」
    for code, label in UI_LABELS_CTRL.items():
        if text == label:
            return code
    for code, cfg in table.items():
        if cfg.get("label") == text:
            return code

    # 控件名（text/textarea/select/dictionary…）反查
    ctrl = CTRL_TO_TYPE.get(text)
    if ctrl:
        return ctrl

    return text  # 未知类型原样返回，交给校验层提示


def is_known(code: str) -> bool:
    """能不能认出这个类型。

    不能只比键名：模板下拉给的是「日期时间（datetime）」这种带括号的写法，
    用户还会手抄「日期」「选项」。这些都能被 resolve 认出来，
    若在这里判 False，校验会对着合法的下拉选项一路报「未知数据类型」。
    """
    return resolve(code) in all_types()


def is_verified(code: str) -> bool:
    return bool(all_types().get(code, {}).get("verified", False))


def note_for(code: str) -> str:
    return all_types().get(code, {}).get("note", "")
