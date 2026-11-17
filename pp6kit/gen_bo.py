"""业务对象 -> bo/001-{code}.xml"""
from __future__ import annotations

import hashlib
import json
import re

from .ids import new_id
from .model import PROCESS_STATE_CODES, Bo, camel_to_snake
from .xmlw import DECL_PLAIN, cdata, el, json_attr, wrap

BO_NS = 'xmlns:tns="http://www.ankepower.com/iform-bo-def" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
BO_SCHEMA = 'xsi:schemaLocation="http://www.ankepower.com/iform-bo-def ../xsd/iform-bo-def.xsd"'

# 导出样例中的属性顺序，照抄以保证一致
ATTR_ORDER = [
    "code", "fieldName", "name", "desc", "type", "datatype", "dataTypeNew",
    "objectType", "options", "isnull", "isindex", "sn", "defaultvalue",
    "format", "length", "precision",
]


def _attr_line(field, bo: Bo | None = None) -> str:
    p = field.resolved()
    ordered = {}
    for key in ATTR_ORDER:
        if key not in p:
            continue
        val = p[key]
        if val is None:
            continue
        if key == "options":
            # 属性侧的级联声明 + selectOptions 冗余。
            # 导入真正读的是 dictionary.dependValues（2026-11-17 平台修复包确认），
            # 这里必须在序列化前注入 —— model.resolved() 拿不到 bo，算不出父选项短码。
            if bo is not None:
                sel = _select_options(field, bo)
                if sel:
                    val = {**val, "selectOptions": sel}
            # options 内的键序照系统导出包的顺序原样写。
            # 依据：用户 2026-11-12 导出的 config-sync 包，带级联的 dic 字段是
            #   isMultiple, isDefault, uniqueCheck, optionSource, columns,
            #   identities, format, formula, cascadeEnabled, cascadeParentCode
            # 无级联的字段是 isMultiple, optionSource, isDefault。
            # JSON 对象本身无序，但导出/导入往返及结构 diff 会因此产生假差异，
            # 而且设计器按原序回写时顺序不同，包就永远对不齐。
            val = _ordered_options(val)
        ordered[key] = json_attr(val) if key == "options" else val
    return el("tns:attribute", ordered, indent=2)


# options 的键序。系统导出包里同一个键在两种字段上位置不同，实测规律：
#   无级联 / 级联链上的普通字段：isMultiple, optionSource, [cascade两键], isDefault
#   带「完整 options」的级联字段（如 productClass）：
#       isMultiple, isDefault, uniqueCheck, optionSource, columns,
#       identities, format, formula, cascadeEnabled, cascadeParentCode
# 所以键序不能只靠一张表，按「有没有那 5 个配套空键」分两套。
_OPTION_KEY_ORDER_FULL = [
    "isMultiple", "isDefault", "uniqueCheck", "optionSource",
    "columns", "identities", "format", "formula",
    "cascadeEnabled", "cascadeParentCode",
]
_OPTION_KEY_ORDER_LITE = [
    "isMultiple", "optionSource", "cascadeEnabled", "cascadeParentCode",
    "isDefault",
]


def _ordered_options(opts: dict) -> dict:
    order = _OPTION_KEY_ORDER_FULL if "uniqueCheck" in opts else _OPTION_KEY_ORDER_LITE
    out = {k: opts[k] for k in order if k in opts}
    for k, v in opts.items():
        out.setdefault(k, v)
    return out


def _parent_code(label: str, field: Bo, bo: Bo) -> str:
    """父选项在本对象父字段字典里的短码（= 父字段字典项的 key_）。"""
    parent = bo.field_by_code(field.cascade_parent)
    scope = parent.dict_key() if parent else f"{bo.code}_{field.cascade_parent}"
    return _choice_code(label, 0, None, scope=scope)


def _select_options(field, bo: Bo) -> list[dict]:
    """字段 options 里的 selectOptions —— 级联值的**冗余副本**。

    2026-11-17 平台修复「导入 zip 不显示级联值」后导出的参考包
    （config-sync-20261117204647.zip）里，级联值只写在
    `<dictionary ... dependValues="[&quot;父key&quot;]"/>`；attribute.options
    里只有 cascadeEnabled / cascadeParentCode，没有 selectOptions。
    所以 _dict_blocks 必须写 dictionary.dependValues；这里保留 selectOptions
    是为了兼容旧前端/旧包往返，值必须与字典项 key 完全一致。

    依据（2026-11-12，两处独立证据）：

    1. 线上前端源码 `filterTableSelectOptions(options, parentValues)`：
           return options.filter(o => {
               const dep = normalize(o.dependValues);
               return dep.some(v => parentValues.includes(v));   // 与父值求交集
           })
       子项靠**自己的 dependValues** 判断属于哪些父值，空数组永远过滤不出来。
    2. 选项管理弹窗里「级联值」是 selectOptions 的独立一列（multiple select），
       保存时走 `dependValues: JSON.stringify(values)`，与 attrs 无关。

    值本身是**父选项的字典 key**（8 位短码），因为父字段下拉的 value 就是它。
    同一份 Excel 反复出包时短码稳定（_choice_code 是 md5），所以级联关系也稳定。

    没有分组的普通选项字段不写 selectOptions —— 系统对这类字段的约定是不带这个键，
    多写会让包与系统产物不一致（见 tests/test_bo.py BO22）。
    """
    if not field.choice_groups:
        return []
    items: list[dict] = []
    # value 必须与 dictionaryList 里同名字典项的 key 完全相同 —— 系统靠它
    # 把「选项显示值」和「字典项」对上。两边都用 _choice_code(scope=dict_key)，
    # 所以短码天然一致；一旦不一致，下拉会显示空白。
    codes = _dict_codes(field)
    parent_field = bo.field_by_code(field.cascade_parent or "")
    parent_codes = _dict_codes(parent_field) if parent_field is not None else {}
    for label in field.sub_choices():
        parents = [p for p, kids in field.choice_groups.items() if label in kids]
        items.append({
            "value": codes[label],
            "label": label,
            "text": label,
            "disabled": 0,
            # 父值必须在父字段字典里真实存在；查不到就**不写**，
            # 不能拿 md5 兜一个不存在的 key —— 那样导入后下拉是空的，
            # 比「没级联」更难查（2026-11-17 用户包里的悬空 dependValues）。
            "dependValues": [parent_codes[p] for p in parents if p in parent_codes],
            "data": {},
        })
    return items


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
    # GLOBAL 字典的候选值只声明一次（同一个 typeKey 可能被多个字段引用）
    seen_item: set[tuple[str, str]] = set()

    for f in sorted(bo.fields, key=lambda x: x.sn):
        if f.type_new != "dic":
            continue
        # ⚠ 就算没有候选值也要声明 dictionaryType（空字典）：
        # 表单控件引用的 key 必须在对象里存在，漏声明会让导出校验直接中止
        # （2026-11-12 用户截图：模型有一轮没给 4 个字段的候选值，
        #  整包被「引用的字典在对象里不存在」拦死）。空下拉由字段级
        #  warn 提醒用户补值，而不是让整包出不去。
        #
        # ---- GLOBAL 分支（项目对齐 R4 / 反解 P1-3）----
        # 引用项目侧的共享字典时必须写 typeScope="GLOBAL" + 原 typeKey +
        # 原候选值 key/sn，**不能**再生成 `合同_汇报类型` 这种新 key：
        # 线上真值（项目.zip）是 typeKey="report_type" struType="1"，
        # 6 个 key 是固定业务码（MonthlyReportModules/…/BiweeklyReportModules）。
        # 候选值原样照抄的原因：短码是业务常量，md5 重算出来的码系统里没有，
        # 导入后是空下拉（PL11 守的就是这条）。
        if f.dict_ref:
            key = f.dict_ref
            if key not in seen_type:
                seen_type.add(key)
                types.append(el("tns:dictionaryType", {
                    "typeKey": key,
                    "name": f.dict_name or key,
                    "struType": f.dict_stru or "1",
                    "sn": len(types) + 1,
                    "typeScope": "GLOBAL",
                }, indent=2))
            for item in f.dict_items or []:
                item_key = str(item.get("key") or "")
                if not item_key or (key, item_key) in seen_item:
                    continue
                seen_item.add((key, item_key))
                # 原生 GLOBAL 字典项**不带** attrs 子元素（项目.zip 逐字节核对），
                # 照抄即可，不要沿用 BO_ATTR 那套 tag 样式
                dics.append(el("tns:dictionary", {
                    "typeKey": key,
                    "key": item_key,
                    "name": str(item.get("name") or item_key),
                    "sn": int(item.get("sn") or len(dics) + 1),
                    "disabled": "false",
                }, indent=2))
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
        # 级联分组：本字段的字典项 = 分组里出现的全部子项（去重）。
        # 父选项由父字段自己的字典提供，不重复写在这里。
        labels = f.sub_choices() if f.choice_groups else list(f.choices)
        codes = _dict_codes(f)
        # 级联关系要写到**字典项**上（2026-11-17 平台修复后的导出格式）：
        #   <dictionary ... dependValues="[&quot;父选项key&quot;]"/>
        # 之前只写在 attribute.options.selectOptions 里，平台导入时读不到，
        # 所以级联值一直不显示。根字段（被别的字段当父级）写 []。
        is_cascade = bool(f.choice_groups) or any(
            x.cascade_parent == f.code for x in bo.fields)
        parent_field = bo.field_by_code(f.cascade_parent or "")
        parent_codes = _dict_codes(parent_field) if parent_field is not None else {}
        for i, label in enumerate(labels, start=1):
            # attrs 子元素：选项的展示样式（tag 形状/颜色）。原生「推送工作区」
            # 导出的每个 dictionary 都带这段默认样式 JSON（2026-11-11 risk 包
            # 与项目立项包逐字节核对），缺失不影响功能但会让 XML 与系统产物
            # 不一致，选项在界面上也没有 tag 外观。
            #
            # ⚠ 级联值**不能**塞进这里。2026-11-12 查线上前端源码确认：
            # 选项管理表格里「级联值」是独立的一列：
            #   {field:"dependValues", title:"级联值", type:"select", multiple:true}
            # 而 attrs 只承载颜色（getOptions 里是 data:{attrs:e.color}）。
            # 塞进 attrs 系统根本不读。真正的位置见 _select_options()。
            dic_children = el("tns:attrs", "", cdata(
                '{"backgroundColor":"","shape":"tag","effect":"light",'
                '"type":"custom","color":"#000000","boardColor":"","icon":""}'
            ), indent=3)
            d_attrs = {
                "typeKey": key,
                # 选项值用英文短码，对标线上真实数据；
                # 界面显示的是 name（中文），两列各司其职
                "key": codes[label],
                "name": label,
                "sn": i,
                "disabled": "false",
            }
            if is_cascade:
                deps: list[str] = []
                if f.choice_groups:
                    deps = [parent_codes[p]
                            for p, kids in f.choice_groups.items()
                            if label in kids and p in parent_codes]
                # 与系统导出一致：无空格压缩 JSON（[&quot;a&quot;,&quot;b&quot;]）
                d_attrs["dependValues"] = json_attr(deps)
            dics.append(el("tns:dictionary", d_attrs, dic_children, indent=2))

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
        if f.dict_ref:
            # GLOBAL 字典的 key 是系统常量（report_type 的 6 个业务码），
            # 不参与 md5 撞码检查——它们根本不是本地算出来的
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


def _dict_codes(field: Bo) -> dict[str, str]:
    """本字段字典项 label → key 的完整映射。

    字典块、selectOptions、dictionary.dependValues 三处必须用**同一份**码：
    父字段的字典项 key 是子字段 dependValues 的取值，任何一处重算错位，
    导入后级联就是空的（2026-11-17 用户反馈「导入 zip 不显示级联值」）。
    """
    key = field.dict_key()
    labels = field.sub_choices() if field.choice_groups else list(field.choices)
    used: set[str] = set()
    return {label: _code_for_field(field, label, i, used, key)
            for i, label in enumerate(labels, start=1)}


def _code_for_field(field: Bo, label: str, i: int, used: set, scope: str) -> str:
    """字典项短码。``processState`` 用平台系统常量，其余走 md5。

    为什么单独一个函数：字典块（_dict_blocks）和条件值换算（dict_choice_codes）
    必须用**同一条**规则，否则对象 XML 里的码和流程 XML 里的 BusinessState 会分叉。
    """
    if field.code == "processState" and label in PROCESS_STATE_CODES:
        return PROCESS_STATE_CODES[label]
    return _choice_code(label, i, used, scope=scope)


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
        # 对象「选择模式」：流程驱动 = 流程模式（workflow），普通业务 = normal。
        # 依据线上原生导出（PP6/业务对象/项目流程.zip → project_create.xml）：
        #   <tns:bodefentity ... mode="workflow">
        # 对照普通对象（项目.zip / 需求.zip）都是 mode="normal"。
        # 写死 normal 的后果：系统把流程对象当普通业务对象，流程 XML 导进去
        # 挂不上对象，对象上也没有流程状态/审批入口。
        "mode": "workflow" if bo.flow_driven else "normal",
    }

    body = [
        el("tns:options", "", cdata(json_attr(options)), indent=2),
    ]
    for f in sorted(bo.fields, key=lambda x: x.sn):
        body.append(_attr_line(f, bo))

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
    if target.dict_ref:
        # 全局字典的候选值 key 是项目侧固定业务码，直接照抄（不走 md5）
        return {str(i.get("name") or ""): str(i.get("key") or "")
                for i in (target.dict_items or []) if i.get("key")}
    key = target.dict_key()
    if not key:
        return {}
    used: set[str] = set()
    out: dict[str, str] = {}
    # 同一字典的 used 是跨字段累加的（见 _dict_blocks），要按 sn 顺序重放一遍
    for f in sorted(bo.fields, key=lambda x: x.sn):
        if f.type_new != "dic" or not f.choices or f.dict_key() != key:
            continue
        # 按**字典里真实写出去的那批选项**迭代，必须与 _dict_blocks 同源：
        # 分组级联字段的 choices 是「父选项名 ∪ 子选项名」，而字典块只写
        # sub_choices()（子选项）。若照 choices 迭代，父选项名（中药/化药）
        # 也会被算出一个 md5 短码——字典里根本没有这个 key，于是流程条件
        # 与隐藏规则填父选项名时会指向一个不存在的选项值，而且因为
        # `value in codes` 命中了，连「不在选项里」的提示都不会报。
        # 子选项的短码不受影响：_choice_code 的 md5 只用 (scope, 选项名)，
        # 不含序号（QA 2026-11-13 实测 OTC / RX / 转增加 两条路径完全一致）。
        labels = f.sub_choices() if f.choice_groups else list(f.choices)
        codes = _dict_codes(f)
        # 级联关系要写到**字典项**上（2026-11-17 平台修复后的导出格式）：
        #   <dictionary ... dependValues="[&quot;父选项key&quot;]"/>
        # 之前只写在 attribute.options.selectOptions 里，平台导入时读不到，
        # 所以级联值一直不显示。根字段（被别的字段当父级）写 []。
        is_cascade = bool(f.choice_groups) or any(
            x.cascade_parent == f.code for x in bo.fields)
        parent_field = bo.field_by_code(f.cascade_parent or "")
        parent_codes = _dict_codes(parent_field) if parent_field is not None else {}
        for i, label in enumerate(labels, start=1):
            code = _code_for_field(f, label, i, used, key)
            if f.code == field_code:
                out[label] = code
    return out
