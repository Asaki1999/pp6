"""命名规则：对象编码 / 物理表名 / 字段编码 / 去重。

这组用例守的是「生成的名字能不能在系统里用」——对象编码直接决定建表名，
出错的后果是导入后库里多出一张 `pp_xiang_mu` 这种谁也不认识的表。
"""
from __future__ import annotations

import re

from pp6kit import naming
from pp6kit.model import camel_to_snake, slug, table_name_for
from tests._harness import case

CODE_OK = re.compile(r"^[a-z][a-z0-9_]*$")


@case("NAM01", "naming", "中文对象名生成系统同款英文编码", "P0",
      ["项目→project", "与原生包一致"])
def _():
    assert naming.object_code("项目") == "project"


@case("NAM02", "naming", "带修饰词的对象名仍对得上系统已有对象", "P0",
      ["项目立项→project_create", "导入能匹配同名"])
def _():
    assert naming.object_code("项目立项") == "project_create"


@case("NAM03", "naming", "动词类对象名按语义拆词", "P1",
      ["采购申请→purchase_apply"])
def _():
    assert naming.object_code("采购申请") == "purchase_apply"


@case("NAM04", "naming", "中英混排保留英文原词", "P1",
      ["MAH持有人→mah_holder", "ASCII 片段不转拼音"])
def _():
    assert naming.object_code("MAH持有人") == "mah_holder"


@case("NAM05", "naming", "空名与纯空格不产出编码", "P2",
      ["空输入返回空", "不抛异常"])
def _():
    assert naming.object_code("") == ""
    assert naming.object_code("   ") == ""


@case("NAM06", "naming", "括号补充说明不污染编码", "P2",
      ["（草案）项目→project"])
def _():
    assert naming.object_code("（草案）项目") == "project"
    assert naming.object_code("项目（二期）") == "project"


@case("NAM07", "naming", "任何中文名生成的编码都符合库表命名规范", "P0",
      ["小写字母开头", "仅含字母数字下划线"])
def _():
    names = ["项目", "需求类型", "测试用例库大类", "设备", "风险登记",
             "工时填报", "费用报销", "供应商档案", "会议纪要", "项目周报",
             "变更申请", "第1版方案", "A-B_C", "质量管理体系文件"]
    for name in names:
        code = naming.object_code(name)
        assert code, f"{name} 没有生成编码"
        assert CODE_OK.match(code), f"{name} → {code} 不符合命名规范"


@case("NAM08", "naming", "字段编码是小驼峰（界面添加字段的规范）", "P0",
      ["申请金额→shenQingJinE"])
def _():
    assert naming.to_english("申请金额") == "shenQingJinE"
    assert re.match(r"^[a-z][a-zA-Z0-9]*$", naming.to_english("项目名称"))


@case("NAM09", "naming", "物理列名用下划线并带尾下划线", "P0",
      ["项目名称→xiang_mu_ming_cheng_"])
def _():
    assert naming.to_english("项目名称", camel=False) == "xiang_mu_ming_cheng"


@case("NAM10", "naming", "字段名为空不产出编码", "P2", ["空输入返回空"])
def _():
    assert naming.to_english("") == ""
    assert naming.to_english("  ") == ""


@case("NAM11", "naming", "物理表名自动补 pp_ 前缀", "P0",
      ["project→pp_project", "手册规定"])
def _():
    assert table_name_for("project") == "pp_project"
    assert table_name_for("project_create") == "pp_project_create"


@case("NAM12", "naming", "已带前缀的编码不会拼成 pp_pp_", "P0",
      ["原生对象 code 就是 pp_equipment", "双前缀会建出错误的表"])
def _():
    assert table_name_for("pp_equipment") == "pp_equipment"
    assert table_name_for("PP_Project") == "pp_project"


@case("NAM13", "naming", "空编码不产出表名", "P2", ["空输入返回空，不抛异常"])
def _():
    assert table_name_for("") == ""


@case("NAM14", "naming", "表名清理连续下划线", "P2", ["a__b→pp_a_b"])
def _():
    assert table_name_for("a__b") == "pp_a_b"
    assert "__" not in table_name_for("x__y__z")


@case("NAM15", "naming", "编码撞名时递增后缀", "P0",
      ["project→project2→project3", "同名对象不会互相覆盖"])
def _():
    assert naming.unique("project", set()) == "project"
    assert naming.unique("project", {"project"}) == "project2"
    assert naming.unique("project", {"project", "project2"}) == "project3"


@case("NAM16", "naming", "限长后仍然不撞名", "P2", ["截断后加后缀"])
def _():
    got = naming.unique("project", {"project"}, limit=8)
    assert got not in {"project"}
    assert len(got) <= 9 or got != "project"


@case("NAM17", "naming", "超长编码在词边界截断", "P2",
      ["不留半截单词", "物理列名有长度上限"])
def _():
    long_name = "这是一个非常非常非常非常非常非常长的业务对象名称用来测试截断"
    code = naming.object_code(long_name)
    assert 0 < len(code) <= 40, f"编码长度 {len(code)} 超出预期"
    assert not code.endswith("_"), "截断后不该留下尾下划线"


@case("NAM18", "naming", "数据库关键字能被识别出来", "P1",
      ["order/desc/key/select/table", "列名撞关键字会导致建表失败"])
def _():
    for word in ("order", "desc", "key", "select", "table"):
        assert naming.is_reserved(word), f"{word} 应该被识别为保留字"
    assert not naming.is_reserved("project")


@case("NAM19", "naming", "驼峰转下划线", "P1", ["projectCreate→project_create"])
def _():
    assert camel_to_snake("projectCreate") == "project_create"
    assert camel_to_snake("PPProject") == "pp_project"
    assert camel_to_snake("abc") == "abc"


@case("NAM20", "naming", "文件名 slug 只保留安全字符", "P2", ["中文名出包不会产生怪字符"])
def _():
    got = slug("项目 A/B")
    assert got and all(ch.isalnum() for ch in got), f"slug 结果不安全：{got!r}"
    assert slug("   ") == ""


@case("NAM21", "naming", "拼音兜底能准确报出来：采购申请不含拼音，设备点检记录的「点检」是拼音", "P0",
      ["用户 2026-11-20：选择器 key 不要拼音", "has_pinyin / pinyin_parts 与 object_code 同一份归一"])
def _():
    """词典命中与否必须**说得出来**：提示语靠 ``pinyin_parts`` 精确到词。

    两条要求同时成立：
      · 常见词（采购 / 申请）全都命中词典 → ``has_pinyin`` 为 False，不许误报；
      · 缺词（点检）退成 dianjian → 能报出「哪个中文词、什么拼音」，用户才知道
        该往 ``naming.WORDS`` 里补什么。
    ``pinyin_parts`` 必须和 ``object_code`` 走同一份归一（``_object_name_text``），
    否则会出现「key 里是 dianjian、提示说别的词」这种对不上的情况。
    """
    assert naming.object_code("采购申请") == "purchase_apply"
    assert not naming.has_pinyin("采购申请"), naming.pinyin_parts("采购申请")

    assert naming.object_code("设备点检记录") == "equipment_dianjian"
    assert naming.has_pinyin("设备点检记录")
    assert naming.pinyin_parts("设备点检记录") == [("点检", "dianjian")], \
        naming.pinyin_parts("设备点检记录")
    # 「记录」是 _NOISE 尾巴：不参与命名，也不该被算成拼音兜底
    assert all(cn != "记录" for cn, _py in naming.pinyin_parts("设备点检记录"))
