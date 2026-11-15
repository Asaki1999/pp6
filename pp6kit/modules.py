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
    筛选器      管理 > 筛选器   > 导入        目录 data_template/ 文件名 001-{key}.xml
    流程        管理 > 流程设计 > 导入        裸 XML（不进 zip）  文件名 {流程名}.xml

**序号在一个 zip 内必须唯一**：系统导出的包里每个 XML 前三位是流水号，
同目录顺着数、跨目录接着数（bo/001、bo/002、bo/003、form/004、data_template/005）。
生成器出「按模块」包时同样从 001 起逐条编号（真实包 `项目列表.zip` 单条就是
`data_template/001-project_list_table.xml`），绝不能多条共用同一个 001——
zip 允许同名条目，导入端按名字取文件，重号等于静默丢内容。
编号逻辑见 `builder.entry_slug()` / `builder.unique_entry()`。

流程仍然单独走：它是裸 XML，不参与整包。

`data_template/` 下三位是同一类东西（数据集），系统里却按「列表 / 数据集 / 筛选器」
三个菜单分开导入，所以模型里就把它们拆成 `lists` 与 `datasets(ds_type)`。

依赖关系（导入顺序）：业务对象 → 数据集/筛选器 → 表单 → 列表。
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
           "下拉框的取数来源。", "数据集"),
    Module("filter", "筛选器", "data_template", "管理 › 筛选器 › 导入", 20,
           "列表上方的筛选条件预设。", "筛选器"),
    Module("form", "表单", "form", "管理 › 表单 › 导入", 30,
           "新建/详情/条件专用等录入界面。", "表单"),
    Module("list", "列表", "data_template", "管理 › 列表 › 导入", 40,
           "列表页展示的列与视图。", "列表"),
    Module("workflow", "流程", "workflow", "管理 › 流程设计 › 导入", 50,
           # 以前这里写「解压后直接上传」——那时流程还得自己在设计器里导出。
           # 现在生成器直接出裸 XML，谈不上解压，语义改成「单独上传」。
           "流程定义是裸 XML，不进同步包；生成后到 管理 › 流程设计 › 导入 单独上传。",
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
        return "filter" if item.ds_type == DS_FILTER else "dataset"
    return "bo"


# Artifact.kind 用的是界面上的中文叫法，这里给个反查
_KIND_CODE = {
    "业务对象": "bo", "表单": "form", "列表": "list",
    "数据集": "dataset", "筛选器": "filter", "流程": "workflow",
}


def code_of_kind(kind: str) -> str:
    """产物标题（中文）→ 模块编码。"""
    return _KIND_CODE.get((kind or "").strip(), "bo")


def ds_type_of(code: str) -> str:
    return DS_FILTER if code == "filter" else DS_VALUE


def resolve(codes) -> list[Module]:
    """前端传 `["form","list"]` 或全量；`all` / 空 = 全部。"""
    if not codes or "all" in codes:
        return list(MODULES)
    return ordered([c for c in codes if c in BY_CODE])


def module_of_dataset(ds_type: str) -> str:
    return "filter" if ds_type == DS_FILTER else "dataset"


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
            elif m.code == "dataset":
                out[m.code] = sum(1 for d in project.datasets if d.ds_type != DS_FILTER)
            elif m.code == "filter":
                out[m.code] = sum(1 for d in project.datasets if d.ds_type == DS_FILTER)
            else:
                out[m.code] = 0
        return out
