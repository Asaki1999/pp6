"""业务对象 -> bo/001-{code}.xml"""
from __future__ import annotations

import hashlib
import json
import re

from .ids import new_id
from .model import Bo, camel_to_snake
from .xmlw import DECL_PLAIN, cdata, el, json_attr, wrap

BO_NS = 'xmlns:tns="http://www.ankepower.com/iform-bo-def" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
BO_SCHEMA = 'xsi:schemaLocation="http://www.ankepower.com/iform-bo-def ../xsd/iform-bo-def.xsd"'

# 导出样例中的属性顺序，照抄以保证一致
ATTR_ORDER = [
    "code", "fieldName", "name", "desc", "type", "datatype", "dataTypeNew",
    "objectType", "options", "isnull", "isindex", "sn", "defaultvalue",
    "format", "length", "precision",
]


def _attr_line(field) -> str:
    p = field.resolved()
    ordered = {}
    for key in ATTR_ORDER:
        if key not in p:
            continue
        val = p[key]
        if val is None:
            continue
        ordered[key] = json_attr(val) if key == "options" else val
    return el("tns:attribute", ordered, indent=2)


def _dict_blocks(bo: Bo) -> tuple[str, str]:
    """生成字典类型 + 字典选项两块 XML。

    对照线上系统真实格式（PP6/业务对象/项目.zip）：
        <dictionaryTypeList>
          <dictionaryType typeKey="report_type" name="项目汇报类型" struType="1" sn="1" typeScope="GLOBAL"/>
        </dictionaryTypeList>
        <dictionaryList>
          <dictionary typeKey="report_type" key="MonthlyReportModules" name="月报" sn="146" disabled="false"/>
        </dictionaryList>

    选项值跟着业务对象一起导入，用户不用去后台单独配字典。

    线上实测（2026-10-29，iform_cat_type / iform_cat_dic 对照）：
    系统「添加字段 › 选项来源」有两档——
        全局选项   type_scope_='GLOBAL'  type_key_ 是共享字典 key（sf / invoice_type）
        自定义     type_scope_='BO_ATTR' type_key_ 带对象/字段痕迹（defect_category）
                                        字典名带「选项」后缀（缺陷类型选项）
                                        候选值 key_ 是**英文短码**（630ddeea / 67ca25ca）

    新表单的字段都是从文档新建的，选项来源必须是**自定义**：
      - 写 GLOBAL 等于宣称"引用共享选项库"，系统会去找一个已存在的字典，
        找不到就落成空下拉（用户截图里「全局选项」那条路径）
      - 候选值 key 写中文虽然系统能存，但界面「选项值」一列显示的就是中文，
        且与线上真实数据（英文短码）不一致，比对/导入导出容易对不上
    """
    types: list[str] = []
    dics: list[str] = []
    seen_type: set[str] = set()
    # 每个字典内部单独去重：key_ 只要求同字典内唯一（库里没有 unique 索引，
    # 跨字典重复很常见，见 _choice_code 注释）
    codes_by_type: dict[str, set[str]] = {}

    for f in sorted(bo.fields, key=lambda x: x.sn):
        if f.type_new != "dic" or not f.choices:
            continue
        key = f.dict_key()
        if not key:
            continue
        used = codes_by_type.setdefault(key, set())
        if key not in seen_type:
            seen_type.add(key)
            types.append(el("tns:dictionaryType", {
                "typeKey": key,
                # 自定义选项的字典名 = 字段名 + 「选项」后缀，**无条件追加**。
                # 依据用户导出的 PP6/业务对象/选项.zip：字段叫「这是一个选项」，
                # 字典名是「这是一个选项选项」——系统不判重，就是直接拼。
                "name": f"{f.name}选项",
                # BO_ATTR + struType=0 = 自定义（字段级）选项。
                #
                # 2026-10-29 靠两份真实样例才分清（踩了两次弯路）：
                #   选项.zip（手动点"自定义"填值后导出）：
                #       typeScope="BO_ATTR" struType="0"
                #       typeKey="csxxz_zheShiYiGeXuanXiang"（对象编码_字段编码）
                #       字段 options: "optionSource":"BO_ATTR_DIC_TYPE"
                #       → 界面显示「选项来源=自定义」
                #   需求.zip（引用已有共享字典）：
                #       typeScope="GLOBAL" struType="1"
                #       字段 options: "optionSource":"DIC_TYPE"
                #       → 界面显示「选项来源=全局选项」
                # 新表单的字段都是从文档新建的，没有现成字典可引用，
                # 所以必须走 BO_ATTR（自定义）。
                "struType": "0",
                "sn": len(types) + 1,
                "typeScope": "BO_ATTR",
            }, indent=2))
        for i, label in enumerate(f.choices, start=1):
            dics.append(el("tns:dictionary", {
                "typeKey": key,
                # 选项值用英文短码，对标线上真实数据；
                # 界面显示的是 name（中文），两列各司其职
                "key": _choice_code(label, i, used, scope=key),
                "name": label,
                "sn": i,
                "disabled": "false",
            }, indent=2))

    type_list = wrap("tns:dictionaryTypeList", types, indent=1) if types \
        else el("tns:dictionaryTypeList", "", None, indent=1)
    dict_list = wrap("tns:dictionaryList", dics, indent=1) if dics \
        else el("tns:dictionaryList", "", None, indent=1)
    return type_list, dict_list


def choice_conflicts(bo: Bo) -> list[str]:
    """出包前自检：同一字典内的选项 key 不能重复。

    数据库 iform_cat_dic 只有主键 id_ 和 key_ 的**普通索引**，
    没有唯一约束兜底——重复了服务端不报错，只会静默覆盖/错乱，比失败更难查。
    这里按字段重新算一遍，把冲突列出来（validate 阶段会调）。
    """
    bad: list[str] = []
    seen: dict[str, dict[str, str]] = {}      # 字典 key -> {短码: 选项名}
    for f in sorted(bo.fields, key=lambda x: x.sn):
        if f.type_new != "dic" or not f.choices:
            continue
        key = f.dict_key()
        bucket = seen.setdefault(key, {})
        for i, label in enumerate(f.choices, start=1):
            # 空集合 + 固定 scope：算出"最朴素"的那个码，
            # 不受自愈逻辑影响，才能看出真的撞没撞
            code = _choice_code(label, i, set(), scope=key)
            if code in bucket and bucket[code] != label:
                bad.append(
                    f"{bo.code}.{f.code}：字典 {key} 里「{bucket[code]}」和「{label}」"
                    f"算出了同一个选项值 {code!r}，导入后会互相覆盖")
            bucket[code] = label
    return bad


def _choice_code(label: str, sn: int, used: set[str] | None = None,
                 scope: str = "") -> str:
    """选项值（key）→ 8 位十六进制短码，形如 630ddeea / 67ca25ca。

    这是 iform_cat_dic.key_ 的取值。线上真实数据（2026-10-29 实测）：
        630ddeea / 67ca25ca / 34b73428 / a8a7b508 …
        长度分布里 8 位居首（114 条），就是这套 hex 短码。

    约束（实测）：
      - iform_cat_dic **没有唯一索引**，key_ 只有普通索引 idx_c_d_key
      - 同一字典内 key_ 零重复；跨字典重复很常见
        （3184e2aa 出现在 17 个不同字典里）
      → 只要求「同一字典内唯一」，不用跟库里全局查重。

    为什么是**确定性**哈希而不是随机（用户 2026-10-29 确认）：
      同一份 Excel 重复出包，短码必须一致——否则第二次导入时，
      系统会把它当成一批**新选项**，而不是更新原来那几个，
      结果就是选项越导越多、旧值变成孤儿数据。
      所以用 md5(字典 key + 选项名) 取前 8 位：同样的输入永远同样的结果。
    """
    text = (label or "").strip()
    if used is None:
        used = set()

    def _hash(seed: str) -> str:
        return hashlib.md5(seed.encode("utf-8")).hexdigest()[:8]

    code = _hash(f"{scope}\x00{text}")
    # 碰撞（8 位 hex 空间里不同选项撞上的概率很低，但不是零）时
    # 追加序号再哈希，保证依然确定
    n = 2
    while code in used:
        code = _hash(f"{scope}\x00{text}\x00{n}")
        n += 1
    used.add(code)
    return code



def render(bo: Bo, bo_id: str | None = None, log_enable: bool = True,
           todo_form_key: str = "") -> str:
    bo_id = bo_id or new_id()
    options = {
        "struType": "list",
        "idKey": bo.pk,
        "pIdKey": bo.pk,
        "key": bo.pk,
        "formKey": "",
        "uniqueRules": [],
    }
    options.update(bo.options or {})

    ent_attrs = {
        "id": bo_id,
        "tblName": bo.tbl_name,
        "code": bo.code,
        "name": bo.name,
        "dataformat": "json",
        "boType": bo.bo_type or "object",
        "isCreateTable": "Y",
        "pk": bo.pk or "id",
        "typeId": bo.type_id or "",
        "dsAlias": "",
        "desc": bo.desc or "",
        "moduleId": bo.module_id or "",
        "mode": "normal",
    }

    body = [
        el("tns:options", "", cdata(json_attr(options)), indent=2),
    ]
    for f in sorted(bo.fields, key=lambda x: x.sn):
        body.append(_attr_line(f))

    entity = wrap("tns:bodefentity", body, ent_attrs, indent=1)

    log_block = el("tns:boLogTemplateList", "")
    if log_enable and bo.fields:
        log = wrap(
            "tns:boLogTemplate",
            [
                el("tns:saveContent", "", cdata(f"创建了{bo.name}:  ${{current.name}}"), indent=3),
                el("tns:updateContent", "", cdata("${changedFields}"), indent=3),
                el("tns:deleteContent", "", cdata(f"删除了{bo.name}:${{current.name}}"), indent=3),
            ],
            {"boCode": bo.code, "enable": "1"},
            indent=2,
        )
        log_block = wrap("tns:boLogTemplateList", [log], indent=1)

    # 父对象关联（Java 搭建说明：立项流程对象要设置父对象为项目对象）
    rel_block = el("tns:objRelationshipList", "", None, indent=1)
    if bo.parent_obj:
        rel = wrap("tns:objRelationship", [
            el("tns:extJson", "", cdata(json_attr({"name": "隶属对象"})), indent=3),
        ], {
            "srcObjCode": bo.code,
            "srcObjName": bo.name,
            "dstObjCode": bo.parent_obj,
            "dstObjName": bo.options.get("parentName") or bo.parent_obj,
            "relField": bo.parent_field or (camel_to_snake(bo.parent_obj) + "_id_"),
            # 默认 PARENT（父子）；PARALLEL 只能由用户显式选择（库里 69 条关系
            # 64 条是 PARENT，平行关联极少，不能替用户猜）
            "relType": (bo.rel_type or "PARENT").upper(),
        }, indent=2)
        rel_block = wrap("tns:objRelationshipList", [rel], indent=1)

    # dic 字段的选项值要跟着对象一起导入，否则系统里字段有了但没候选值
    dict_types, dict_items = _dict_blocks(bo)

    parts = [
        DECL_PLAIN,
        f"<tns:bodefs {BO_NS} {BO_SCHEMA}>",
        entity,
        log_block,
        el("tns:boListColumnList", "", None, indent=1),
        wrap("tns:boTodoTemplateList", [
            el("tns:boTodoTemplate", {
                "boCode": bo.code,
                "label": bo.name,
                "formKey": todo_form_key,
                "enable": "0",
                "source": "2",
            }, indent=2)
        ], indent=1),
        rel_block,
        el("tns:objectRoleList", "", None, indent=1),
        dict_types,
        dict_items,
        "</tns:bodefs>",
    ]
    return "\n".join(parts) + "\n"


def dict_choice_codes(bo: Bo, field_code: str) -> dict[str, str]:
    """某个 dic 字段的「选项名 -> 选项值」映射，算法与 _dict_blocks 完全一致。

    条件隐藏要填「条件值」，但选项值是 md5 出来的 8 位短码（630ddeea 这种），
    让用户在 Excel 里手填是不可能的。所以允许直接写中文选项名，
    出包时用这张表换算；换算不出来（比如那个值本来就是个短码）就原样保留。
    """
    target = bo.field_by_code(field_code)
    if target is None or target.type_new != "dic" or not target.choices:
        return {}
    key = target.dict_key()
    if not key:
        return {}
    used: set[str] = set()
    out: dict[str, str] = {}
    # 同一字典的 used 是跨字段累加的（见 _dict_blocks），要按 sn 顺序重放一遍
    for f in sorted(bo.fields, key=lambda x: x.sn):
        if f.type_new != "dic" or not f.choices or f.dict_key() != key:
            continue
        for i, label in enumerate(f.choices, start=1):
            code = _choice_code(label, i, used, scope=key)
            if f.code == field_code:
                out[label] = code
    return out
