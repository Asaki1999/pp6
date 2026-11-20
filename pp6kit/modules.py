"""导入模块：PowerProject 里每一类配置各自有导入菜单，但也支持一个 zip 装多个模块。

## 两种导法（开发 2026-10-28 确认）

**一把导入**：一个 zip 里同时放 bo/ form/ data_template/，
系统按目录识别，一次全部导入。系统自己导出的
`多个对象整体导出.zip` 就是这个结构：

    bo/001-test_case_base_category.xml
    bo/002-sjzcscd.xml
    bo/003-pp_equipment.xml
    form/004-project_create_start.xml
    data_template/005-customer_list.xml      ← 序号跨目录连续

**按模块导入**：各模块各自成包，逐个菜单上传。只想更新某一块、
或整包导入报错要排查时用这个。

## 各模块的目录与菜单

    业务对象    管理 > 业务对象 > 导入        目录 bo/            文件名 001-{编码}.xml
    表单        管理 > 表单     > 导入        目录 form/          文件名 001-{表单key}.xml
    列表        管理 > 列表     > 导入        目录 data_template/ 文件名 001-{列表key}.xml
    数据集      管理 > 数据集   > 导入        目录 data_template/ 文件名 001-{key}.xml
    选择器      管理 > 数据集   > 导入        目录 data_template/ 文件名 001-{key}.xml
    筛选器      管理 > 筛选器   > 导入        目录 data_template/ 文件名 001-{key}.xml
    流程        管理 > 流程设计 > 导入        裸 XML（不进 zip）  文件名 {流程名}.xml

## 术语分层（用户 2026-11-20 口径，别把两层混成一层）

| 层 | 叫什么 | 长什么样 | 在哪 |
|---|---|---|---|
| 模块（导入/勾选单位） | **选择器** | `Module(code="value", label="选择器")`，勾选树/统计里就显示「选择器」 | 本文件、`webui.mod_payload` |
| 实例（一行一份） | **`<xxx>值来源`** | 名称「采购申请值来源」「项目类型值来源」；`key=<对象名翻英文>_value`（采购申请 → `purchase_apply_value`）；`datasetKey=<对象编码>` | Excel `6.1_值来源` 一行；包里一个 `data_template/*.xml` |

Excel 那张表所以仍叫 `6.1_值来源`：一行就是一个**实例**（每个都叫「xx值来源」）。
模块（.zip 名、勾选项）才叫「选择器」，因为系统里那 200 多个都是「给某个字段挑数据」
用的，用户点的时候说的是「加个选择器」。

**值来源为什么单独一个模块**：它和数据集在系统里是同一个导入入口、同一个目录，但
**配置项完全不同**（主键/名称/唯一列、返回列、过滤列、`datasetKey` 指向对象编码），
而且每个对象默认都有一份「自身的」——混在「数据集」里既看不清也勾不掉。包里仍然写
`data_template/`（平台只认这个目录，见上表）。

**序号在一个 zip 内必须唯一**：系统导出的包里每个 XML 前三位是流水号，
同目录顺着数、跨目录接着数（bo/001、bo/002、bo/003、form/004、data_template/005）。
生成器出「按模块」包时同样从 001 起逐条编号（真实包 `项目列表.zip` 单条就是
`data_template/001-project_list_table.xml`），绝不能多条共用同一个 001——
zip 允许同名条目，导入端按名字取文件，重号等于静默丢内容。
编号逻辑见 `builder.entry_slug()` / `builder.unique_entry()`。

流程仍然单独走：它是裸 XML，不参与整包。

`data_template/` 下三位是同一类东西（数据集），系统里却按「列表 / 数据集 / 筛选器」
三个菜单分开导入，所以模型里就把它们拆成 `lists` 与 `datasets(ds_type)`。
值来源同理：同一个目录、同一个导入入口，但配置项是另一套，勾选树上也单独一项。

依赖关系（导入顺序）：业务对象 → 数据集/值来源/筛选器 → 表单 → 列表。
系统里真正卡顺序的是「业务对象必须最先」——表单和列表都要按表名去找对象。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import Project

# 数据集子类型（model.DataSet.ds_type 的取值）
DS_LIST = "default"        # 列表数据集
DS_FILTER = "filter"       # 筛选器
DS_VALUE = "valueSource"   # 值来源


@dataclass(frozen=True)
class Module:
    code: str              # bo / form / list / dataset / filter / workflow
    label: str             # 界面上的名字
    folder: str            # 系统目录
    menu: str              # 系统里点哪儿导入
    order: int             # 导入顺序
    note: str = ""
    # 前端做勾选树用的分组名，与 /api/parse 返回的 stats 键一致
    stat_key: str = ""
    zipped: bool = True    # 流程是裸 XML，不打 zip


MODULES: tuple[Module, ...] = (
    Module("bo", "业务对象", "bo", "管理 › 业务对象 › 导入", 10,
           "表结构。必须最先导入，表单/列表都按表名找它。", "业务对象"),
    Module("dataset", "数据集", "data_template", "管理 › 数据集 › 导入", 20,
           "列表/统计用的数据集（不含值来源，值来源是单独一项）。", "数据集"),
    Module("filter", "筛选器", "data_template", "管理 › 筛选器 › 导入", 20,
           "列表上方的筛选条件预设。", "筛选器"),
    # 值来源与数据集同一个系统入口、同一个目录，order 也放在同一档（20/25 之间
    # 没别的模块，包内条目顺序由 builder 的生成顺序决定，见 _pack_all 注释）。
    Module("value", "选择器", "data_template", "管理 › 数据集 › 导入", 25,
           "下拉框/关联数据控件取数的选择器，含每个对象默认那份自身的。", "选择器"),
    Module("form", "表单", "form", "管理 › 表单 › 导入", 30,
           "新建/详情/条件专用等录入界面。", "表单"),
    Module("list", "列表", "data_template", "管理 › 列表 › 导入", 40,
           "列表页展示的列与视图。", "列表"),
    Module("workflow", "流程", "workflow", "管理 › 流程设计 › 导入", 50,
           # 流程定义已从工具下线：固定三步，只出流程对象包，不再生成流程 XML。
           "固定流程 开始→审核→通过；只出流程对象包，不生成流程 XML。",
           "流程", zipped=False),
)

BY_CODE: dict[str, Module] = {m.code: m for m in MODULES}


def of(code: str) -> Module:
    return BY_CODE[code]


def label(code: str) -> str:
    mod = BY_CODE.get(code)
    return mod.label if mod else code


def ordered(codes) -> list[Module]:
    """按系统导入顺序排（同时按 MODULES 声明顺序稳定排序）。"""
    want = set(codes)
    return [m for m in sorted(MODULES, key=lambda x: (x.order, x.code)) if m.code in want]


def item_module(item) -> str:
    """一条模型条目（Bo / Form / ListDef / DataSet）属于哪个模块。"""
    kind = type(item).__name__
    if kind == "Bo":
        return "bo"
    if kind == "Form":
        return "form"
    if kind == "ListDef":
        return "list"
    if kind == "DataSet":
        return module_of_dataset(item.ds_type)
    return "bo"


# Artifact.kind 用的是界面上的中文叫法，这里给个反查
_KIND_CODE = {
    "业务对象": "bo", "表单": "form", "列表": "list",
    "数据集": "dataset", "选择器": "value", "值来源": "value",
    "筛选器": "filter", "流程": "workflow",
}


def code_of_kind(kind: str) -> str:
    """产物标题（中文）→ 模块编码。"""
    return _KIND_CODE.get((kind or "").strip(), "bo")


def ds_type_of(code: str) -> str:
    """模块编码 → 数据集子类型（模块与 ds_type 的对照，只此一处）。"""
    if code == "filter":
        return DS_FILTER
    if code == "value":
        return DS_VALUE
    return DS_LIST


def resolve(codes) -> list[Module]:
    """前端传 `["form","list"]` 或全量；`all` / 空 = 全部。"""
    if not codes or "all" in codes:
        return list(MODULES)
    return ordered([c for c in codes if c in BY_CODE])


def module_of_dataset(ds_type: str) -> str:
    """数据集子类型 → 模块编码（选择器 / 筛选器各自独立，其余算数据集）。

    这是「一份 DataSet 该写进哪个模块的包」的唯一判据：builder 写
    data_template 时按它判 plan.has(...)，统计与勾选树也按它分类。
    以前把 DS_VALUE 也算成 dataset，结果值来源既勾不掉也数不清。
    """
    if ds_type == DS_FILTER:
        return "filter"
    if ds_type == DS_VALUE:
        return "value"
    return "dataset"


@dataclass
class Plan:
    """一次生成计划：选中哪些模块、每个模块多少条。"""
    codes: list[str] = field(default_factory=list)

    @property
    def modules(self) -> list[Module]:
        return resolve(self.codes)

    def has(self, code: str) -> bool:
        return code in {m.code for m in self.modules}

    def summary(self, project: Project) -> dict[str, int]:
        out: dict[str, int] = {}
        for m in self.modules:
            if m.code == "bo":
                out[m.code] = len(project.bos)
            elif m.code == "form":
                out[m.code] = len(project.forms)
            elif m.code == "list":
                out[m.code] = len(project.lists)
            elif m.code in ("dataset", "value", "filter"):
                # 三者的数据源都是 project.datasets，靠 ds_type 分流（见 module_of_dataset）
                out[m.code] = sum(1 for d in project.datasets
                                  if module_of_dataset(d.ds_type) == m.code)
            else:
                out[m.code] = 0
        return out
