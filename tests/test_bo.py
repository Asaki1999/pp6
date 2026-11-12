"""业务对象 XML：字段属性、字典、关系、系统字段。

这一层是「导入后能不能建表、字段能不能用」的最终决定者，
所以断言都直接读 zip 里的 XML，不看中间变量。
"""
from __future__ import annotations

import re

from pp6kit import spec as S
from pp6kit import webui
from tests._harness import (all_tags, attrs, attr_of, case, field, pick,
                            sample_zip, texts, unzip, xlsx, xml_ok)


def build(sheets: dict) -> bytes:
    return webui.build_zip(xlsx(sheets))[0]


def bo_xml(rows: list[dict], bo: dict | None = None) -> str:
    obj = bo or {"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract"}
    return pick(build({
        S.S_BO: [obj],
        S.S_FIELD: rows,
    }), "bo/")


@case("BO01", "bo", "生成的业务对象 XML 结构合法", "P0",
      ["XML 可被解析", "只有一个对象定义"])
def _():
    x = bo_xml([field("contract", "名称", "字符串")])
    xml_ok(x)
    assert x.count("<tns:bodefentity") == 1
    assert "http://www.ankepower.com/iform-bo-def" in x


@case("BO02", "bo", "表名/编码/名称按填写进 XML", "P0",
      ["tblName=pp_contract", "isCreateTable=Y"])
def _():
    x = bo_xml([field("contract", "名称", "字符串")])
    head = attrs(all_tags(x, "bodefentity")[0])
    assert head["tblName"] == "pp_contract"
    assert head["code"] == "contract"
    assert head["name"] == "合同"
    assert head["isCreateTable"] == "Y"
    assert head["pk"] == "id"


@case("BO03", "bo", "业务字段序号从 1 连续递增", "P0",
      ["sn 连续", "系统字段接在后面"])
def _():
    x = bo_xml([field("contract", "甲", "字符串"), field("contract", "乙", "字符串"),
                field("contract", "丙", "字符串")])
    sns = [int(attrs(t)["sn"]) for t in all_tags(x, "attribute")]
    assert sns == list(range(1, len(sns) + 1)), sns


@case("BO04", "bo", "系统字段自动补齐", "P0",
      ["id/tenantId/version/deleted", "缺了导进去没有主键"])
def _():
    x = bo_xml([field("contract", "名称", "字符串")])
    codes = {attrs(t)["code"] for t in all_tags(x, "attribute")}
    for need in ("id", "tenantId", "version", "deleted", "createTime", "updateTime"):
        assert need in codes, f"缺少系统字段 {need}"


@case("BO05", "bo", "字段物理列名以下划线结尾", "P0",
      ["手册要求", "列名错位会导致数据写不进去"])
def _():
    x = bo_xml([field("contract", "合同金额", "数字")])
    col = attr_of(x, "attribute", "heTongJinE", "fieldName")
    assert col.endswith("_"), col
    assert re.match(r"^[a-z][a-z0-9_]*$", col), col


@case("BO06", "bo", "自定义日期字段不被平台隐藏", "P0",
      ["isDefault=false", "format 到分钟",
       "isDefault=true 时整条字段在属性页消失"])
def _():
    x = bo_xml([field("contract", "签订日期", "日期时间（datetime）")])
    frag = all_tags(x, "attribute")
    item = next(t for t in frag if attrs(t)["code"] == "qianDingRiQi")
    a = attrs(item)
    assert a["dataTypeNew"] == "datetime", a
    assert a.get("format") == "yyyy-MM-dd HH:mm", a.get("format")
    assert "&quot;isDefault&quot;:false" in a["options"], a["options"]


@case("BO07", "bo", "日期字段填了默认值才标 isDefault", "P1",
      ["新建记录自动填时间"])
def _():
    x = bo_xml([field("contract", "归档日期", "日期时间（datetime）", 默认值="2026-01-01")])
    item = next(t for t in all_tags(x, "attribute")
                if attrs(t)["code"] == "guiDangRiQi")
    assert "&quot;isDefault&quot;:true" in attrs(item)["options"]


@case("BO08", "bo", "选项字段同时生成字典类型与候选值", "P0",
      ["dictionaryType + dictionaryList", "只有一处会变成空下拉"])
def _():
    x = bo_xml([field("contract", "状态", "选项", 选项值="草稿,生效,作废")])
    type_key = f"contract_zhuangTai"
    assert f'typeKey="{type_key}"' in x, "没有字典类型"
    names = re.findall(r'<tns:dictionary typeKey="%s"[^>]*name="([^"]+)"' % type_key, x)
    assert names == ["草稿", "生效", "作废"], names


@case("BO09", "bo", "选项候选值顺序与 Excel 一致", "P1",
      ["第一项是默认值", "顺序即界面顺序"])
def _():
    x = bo_xml([field("contract", "级别", "选项", 选项值="高,中,低")])
    sns = re.findall(r'<tns:dictionary typeKey="contract_jiBie"[^>]*sn="(\d+)"', x)
    assert sns == ["1", "2", "3"], sns


@case("BO10", "bo", "选项短码在多次出包间保持稳定", "P0",
      ["库里已存的是 key", "每次变会让旧记录对不上"])
def _():
    rows = [field("contract", "状态", "选项", 选项值="草稿,生效")]
    k1 = re.findall(r'key="(\w+)" name="草稿"', bo_xml(rows))
    k2 = re.findall(r'key="(\w+)" name="草稿"', bo_xml(rows))
    assert k1 and k1 == k2, (k1, k2)


@case("BO11", "bo", "关联对象字段带出 objectType", "P0",
      ["空 objectType 导入后是空下拉"])
def _():
    x = build({
        S.S_BO: [{"对象名称": "项目", "对象编码": "project", "物理表名": "pp_project"},
                 {"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract"}],
        S.S_FIELD: [field("project", "名称", "字符串"),
                    field("contract", "关联项目", "对象", **{"关联对象/字典": "project"})],
    })
    text = texts(x)["bo/002-contract.xml"]
    item = next(t for t in all_tags(text, "attribute")
                if attrs(t)["code"] == "guanLianXiangMu")
    a = attrs(item)
    assert a["dataTypeNew"] == "object"
    assert a["objectType"] == "project", a.get("objectType")


@case("BO12", "bo", "识别不出的类型按字符串落库", "P1",
      ["dataTypeNew 不能写中文", "本轮修复：脏类型会让字段改不了"])
def _():
    x = bo_xml([field("contract", "手写类型", "日期")])
    item = next(t for t in all_tags(x, "attribute")
                if attrs(t)["code"] == "shouXieLeiXing")
    a = attrs(item)
    assert a["dataTypeNew"] == "varchar", a
    assert a["datatype"] == "varchar", a


@case("BO13", "bo", "字段名里的特殊字符正确转义", "P1",
      ["尖括号/引号/&", "不转义会直接坏 XML"])
def _():
    x = bo_xml([field("contract", "合同<金额>&税率", "字符串")])
    xml_ok(x)
    assert "合同&lt;金额&gt;" in x or "合同<金额>" not in x.split("desc=")[1][:80]


@case("BO14", "bo", "大文本字段不带长度与格式", "P2",
      ["clob 没有 length", "带了会建表失败"])
def _():
    x = bo_xml([field("contract", "备注", "大文本")])
    item = next(t for t in all_tags(x, "attribute")
                if attrs(t)["code"] == "beiZhu")
    a = attrs(item)
    assert a["dataTypeNew"] == "clob"
    assert "length" not in a, a


@case("BO15", "bo", "开启日志的对象生成变更日志模板", "P1",
      ["保存/删除内容带对象名"])
def _():
    x = bo_xml([field("contract", "名称", "字符串")])
    assert "boLogTemplate" in x
    assert 'boCode="contract"' in x
    assert "创建了合同" in x


@case("BO16", "bo", "关闭日志的对象不生成日志模板", "P2",
      ["多一个模板会在系统里多一条菜单"])
def _():
    x = bo_xml([field("contract", "名称", "字符串")],
               {"对象名称": "合同", "对象编码": "contract",
                "物理表名": "pp_contract", "记录变更日志": "否"})
    assert "<tns:boLogTemplate " not in x


@case("BO17", "bo", "流程驱动对象带 4 个流程冗余字段", "P1",
      ["流程节点/处理人/定义/实例", "流程流转时反写"])
def _():
    x = build({
        S.S_BO: [{"对象名称": "项目立项", "对象编码": "project_create",
                  "物理表名": "pp_project_create", "流程驱动": "是"}],
        S.S_FIELD: [field("project_create", "名称", "字符串")],
    })
    text = texts(x)["bo/001-project_create.xml"]
    codes = {attrs(t)["code"] for t in all_tags(text, "attribute")}
    for need in ("processNode", "processHandler", "processDefId", "processInstanceId"):
        assert need in codes, need


@case("BO18", "bo", "父子关系写进 objRelationship", "P1",
      ["relField 用物理列名", "relType=PARENT"])
def _():
    x = build({
        S.S_BO: [{"对象名称": "项目", "对象编码": "project", "物理表名": "pp_project"},
                 {"对象名称": "项目立项", "对象编码": "project_create",
                  "物理表名": "pp_project_create", "父对象编码": "project"}],
        S.S_FIELD: [field("project", "名称", "字符串"),
                    field("project_create", "名称", "字符串"),
                    field("project_create", "关联项目", "对象", **{"关联对象/字典": "project"})],
    })
    text = texts(x)["bo/002-project_create.xml"]
    rel = all_tags(text, "objRelationship")
    assert rel, "没有生成关系"
    a = attrs(rel[0])
    assert a["srcObjCode"] == "project_create"
    assert a["dstObjCode"] == "project"
    assert a["relType"] == "PARENT"
    assert a["relField"].endswith("_"), a["relField"]


@case("BO19", "bo", "同样输入两次出包只差对象 id", "P1",
      ["除 id 外必须逐字节一致", "隐式随机会让出包不可复现"])
def _():
    rows = [field("contract", "名称", "字符串"),
            field("contract", "金额", "数字", 精度=2)]
    a = bo_xml(rows)
    b = bo_xml(rows)
    assert a != b, "两次 id 完全一样，说明 id 不是新生成的（导入会冲突）"
    strip = lambda s: re.sub(r'bodefentity id="\d+"', 'bodefentity id="X"', s)
    assert strip(a) == strip(b)


@case("BO20", "bo", "关联字段留空时自动指向父对象", "P2",
      ["不用手填 projectId", "按父对象编码推断"])
def _():
    x = build({
        S.S_BO: [{"对象名称": "项目", "对象编码": "project", "物理表名": "pp_project"},
                 {"对象名称": "项目立项", "对象编码": "project_create",
                  "物理表名": "pp_project_create", "父对象编码": "project"}],
        S.S_FIELD: [field("project", "名称", "字符串"),
                    field("project_create", "名称", "字符串")],
    })
    text = texts(x)["bo/002-project_create.xml"]
    rel = all_tags(text, "objRelationship")
    assert rel, "父子关系没生成"
    assert attrs(rel[0])["relField"].endswith("_")


@case("BO21", "bo", "多对象出包时每个对象各自独立成文件", "P0",
      ["bo/001-*.xml", "一个文件一个对象"])
def _():
    x = build({
        S.S_BO: [{"对象名称": "项目", "对象编码": "project", "物理表名": "pp_project"},
                 {"对象名称": "合同", "对象编码": "contract", "物理表名": "pp_contract"}],
        S.S_FIELD: [field("project", "名称", "字符串"),
                    field("contract", "名称", "字符串")],
    })
    names = [n for n in unzip(x) if n.startswith("bo/")]
    assert len(names) == 2, names
    for n in names:
        xml_ok(unzip(x)[n].decode("utf-8"))
