"""Excel 模板的唯一真相：表名、列名、下拉项、填写说明。

模板生成与读取共用本文件，避免两边字段漂移。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import fieldtypes as _ft

S_SETTINGS = "0_项目设置"
S_BO = "1_业务对象"
S_FIELD = "2_字段"
S_FORM = "3_表单"
S_FORMFIELD = "4_表单控件"
S_LIST = "5_列表"
S_DATASET = "6_数据集"
S_SCRIPT = "7_表单脚本"
S_HIDE = "8_隐藏规则"
S_HELP = "说明"

SHEET_ORDER = [S_HELP, S_SETTINGS, S_BO, S_FIELD, S_FORM, S_FORMFIELD, S_LIST,
               S_DATASET, S_SCRIPT, S_HIDE]


@dataclass
class Col:
    header: str          # 表头（同时也是 Excel 里看到的列名）
    key: str             # 内部字段名
    width: int = 16
    required: bool = False
    choices: list[str] = field(default_factory=list)
    note: str = ""
    sample: str = ""
    wrap: bool = False   # 单元格允许换行（选项值这种一格多行用）


SETTING_KEYS = {
    "租户ID": "tenantId",
    "文件前缀": "filePrefix",
    "默认模块ID": "moduleId",
    "默认分类ID": "typeId",
    "输出目录": "outDir",
}

# 每项的中文说明与默认值：用户不该被「模块ID」这种词吓到
SETTING_HELP: dict[str, tuple[str, str]] = {
    "租户ID": ("-999", "多租户隔离用。-999 是单租户默认值，一般不用改"),
    "文件前缀": ("001", "只影响「输出目录」里的中间产物文件名（out/bo/001-project.zip 等）。"
                       "交付的导入包内条目名按系统规范重新编号 001/002…，这一项改了对交付包无影响"),
    "默认模块ID": ("", "对象归属哪个模块（项目/团队/文档…）。"
                       "填系统后台「数据 › 业务对象」里模块的真实 ID，留空则对象无归属、列表里找不到"),
    "默认分类ID": ("", "对象放在哪个分类下。填系统后台的真实分类 ID，留空则不分类"),
    "输出目录": ("out", "命令行出包的目录。界面里下载不受此影响"),
}

COLUMNS: dict[str, list[Col]] = {
    S_SETTINGS: [
        Col("配置项", "key", 18, True, note="固定项，勿改"),
        Col("值", "value", 34, note="留空则用默认值"),
        Col("说明", "note", 52, note=""),
    ],
    S_BO: [
        Col("对象编码", "code", 18, False, note="英文小写+下划线，全局唯一。留空则按对象名称自动生成（如 采购申请→purchase_apply）"),
        Col("对象名称", "name", 16, True, note="界面展示名"),
        Col("物理表名", "tbl_name", 20, note="留空自动 = PP_ + 对象编码（手册规定）"),
        Col("对象类型", "bo_type", 12, choices=["object", "list"], note="一般填 object"),
        Col("模块ID", "module_id", 22, note="留空取项目设置里的默认模块ID"),
        Col("分类ID", "type_id", 22, note="留空取默认分类ID"),
        Col("主键", "pk", 10, note="默认 id"),
        Col("记录变更日志", "with_log", 14, choices=["是", "否"], note="是否生成操作日志模板"),
        Col("流程驱动", "flow_driven", 12, choices=["是", "否"],
            note="是=自动补齐流程节点/处理人/流程定义/流程实例 4 个冗余字段"),
        Col("父对象编码", "parent_obj", 16,
            note="子对象挂到哪个对象下（如立项流程挂 project），生成父子关联。填中文名/编码都认，解析时会自动对到系统真实对象"),
        Col("关联字段", "parent_field", 16,
            note="指向父对象的字段编码，留空自动生成"),
        Col("关系类型", "rel_type", 12, choices=["PARENT", "PARALLEL"],
            note="留空默认 PARENT（父子）；PARALLEL=平行关联，极少用，不确定就留空"),
        Col("对象说明", "desc", 26, note=""),
        Col("已有对象ID", "exist_id", 22,
            note="要更新系统里已有的同名对象时，填它的 id（同名时解析提示里会给出）；留空=按新建导入，不会动系统对象。反解导出包会自动带上"),
    ],
    S_FIELD: [
        Col("对象编码", "bo_code", 18, True, note="对应 1_业务对象 的编码"),
        Col("序号", "sn", 8, note="留空按行顺序自动编号"),
        Col("字段编码", "code", 18, False, note="驼峰命名法，如 sysData、userId，不带 Id 后缀。留空按字段名称自动生成（申请金额→applyAmount）"),
        Col("物理列名", "field_name", 20, note="下划线分隔+末尾下划线，如 sys_data_；留空自动生成。不能用数据库关键字（order/desc/key 等）"),
        Col("字段名称", "name", 16, True, note="界面展示名"),
        Col("数据类型", "type_new", 22, True, choices=_ft.choices(),
            note="从下拉里选。选「选项」时，右边「选项值」列填候选值"),
        Col("关联对象/字典", "object_type", 22,
            note="类型=对象 → 填对象编码（project）、中文名（项目）、或对象表名（pp_project）都认，"
                 "出包前会按系统里真实的业务对象自动对照成编码；留空则按字段名推断（「关联项目」→ project）。"
                 "类型=选项 → 可留空，程序按字段编码自动生成字典 key"),
        Col("选项值", "options_text", 26,
            note="仅「选项」类型要填。一个选项一行（Alt+Enter 换行），或逗号分隔。顺序即界面顺序，第一项为默认", wrap=True),
        Col("级联父字段", "cascade_parent", 18,
            note="留空=不级联；填本对象里另一个字段的编码，如 品种分类 填 productCategory。"
                 "父级可以是「选项」也可以是「关联对象」。会写进字段的 "
                 "cascadeEnabled/cascadeParentCode，业务对象和表单两层都生效"),
        Col("长度", "length", 10, note="留空按类型默认；number 建议给 precision"),
        Col("精度", "precision", 8, note="小数位，仅 number 有效"),
        Col("建索引", "is_index", 10, choices=["是", "否"]),
        Col("允许空", "is_null", 10, choices=["是", "否"]),
        Col("默认值", "default_value", 12, note=""),
        Col("扩展参数", "options", 30, note='JSON，如 {"isMultiple":true}'),
        Col("字段说明", "desc", 26, note=""),
    ],
    S_FORM: [
        Col("表单key", "key", 24, True, note="小写+下划线，如 create_project"),
        Col("表单名称", "name", 20, True),
        Col("对象编码", "bo_code", 18, True, note="绑定到哪个业务对象"),
        Col("表单类型", "kind", 18, choices=["标准全套", "新建表单", "详情表单", "条件专用表单", "导入专用表单"],
            note="标准全套=一键生成 新建/详情/条件专用 三张（推荐）"),
        Col("布局", "layout", 14, choices=["一行一列", "一行两列"], note="默认一行两列"),
        Col("分类ID", "type_id", 22, note="留空取默认"),
        Col("状态", "status", 10, choices=["deploy", "draft"]),
        Col("面板宽度%", "width", 12, note="出包统一 90%，此列仅作记录"),
        Col("标签宽度px", "label_width", 12, note="新建 106 / 条件专用 160，留空按类型默认"),
        Col("补隐藏字段", "auto_hidden", 12, choices=["是", "否"],
            note="自动补 id/version 隐藏控件，新建类表单建议保留"),
    ],
    S_FORMFIELD: [
        Col("表单key", "form_key", 24, True),
        Col("字段编码", "name", 20, True, note="须在 2_字段 中存在（除容器控件）"),
        Col("显示标签", "label", 16, True),
        Col("控件类型", "field_type", 16, True,
            choices=["text", "select", "dictionary", "dropSelector", "dropSelectOrg", "linkdata",
                     "editor", "autoNumber", "attachment", "hidden", "number", "date"],
            note="dictionary=数据字典树控件；要级联请填「级联父字段」，会自动出成下拉+字典形态"),
        Col("取值来源", "value_source", 22, note="select/linkdata 填数据集或值来源 key"),
        Col("占位提示", "placeholder", 14),
        Col("必填", "required", 8, choices=["是", "否"]),
        Col("只读", "readonly", 8, choices=["是", "否"]),
        Col("占满整行", "full_row", 10, choices=["是", "否"], note="用于长文本/富文本"),
        Col("另起一行", "newline", 10, choices=["是", "否"]),
        Col("分区", "group", 16,
            note="表单布局V1.0 的分组标题；同名的归到一个卡片，留空=全部放一个卡片"),
        Col("级联父字段", "cascade_parent", 18,
            note="留空=不级联；填本表单另一个选项控件的编码，"
                 "向导出的控件写 cascadeEnabled/cascadeParentCode（字典须是层级字典）"),
        Col("扩展参数", "options", 30, note="JSON，覆盖控件默认参数"),
    ],
    S_LIST: [
        Col("列表key", "key", 24, True, note="小写+下划线"),
        Col("列表名称", "name", 20, True),
        Col("对象编码", "bo_code", 18, True),
        Col("视图类型", "views", 24, note="可多选，逗号分隔：grid,board,gantt,chart"),
        Col("显示字段", "columns", 40, note="字段编码，逗号分隔；留空=全部"),
        Col("排序字段", "sort_field", 20, note="字段编码，如 createTime"),
        Col("排序方向", "sort_dir", 10, choices=["asc", "desc"]),
        Col("树形展示", "tree", 10, choices=["是", "否"]),
        Col("父字段编码", "parent_field", 18, note="树形时的父级字段"),
        Col("标题字段", "title_field", 18, note="看板/甘特的主标题字段"),
    ],
    S_SCRIPT: [
        Col("表单key", "form_key", 30, True, note="对应 3_表单 的 key"),
        Col("脚本说明", "note", 26, note="仅备注，不写入包"),
        Col("JForm 脚本", "script", 90,
            note="整段 JS，例如 Object.assign(JForm, { onLoad(form){} })，写入表单 extendAttr.script"),
    ],
    S_HIDE: [
        Col("表单key", "form_key", 24, True, note="对应 3_表单 的 key"),
        Col("隐藏控件", "target", 20, True, note="条件成立时要隐藏的控件编码"),
        Col("依赖控件", "source", 20, True, note="判断条件依据的控件编码"),
        Col("判断条件", "operator", 12, choices=["等于", "不等于", "为空", "不为空"],
            note="条件成立时执行隐藏；想「等于X时才显示」就反过来写「不等于X」"),
        Col("条件值", "value", 16, note="等于/不等于时必填；为空类留空"),
        Col("条件值说明", "value_label", 16, note="仅备注，界面里显示成人话用"),
        Col("隐藏时清空", "clear_on_hide", 12, choices=["是", "否"],
            note="默认是，切回去后不会残留旧值"),
    ],
    S_DATASET: [
        Col("数据集key", "key", 24, True, note="表单 select 的取值来源"),
        Col("数据集名称", "name", 20, True),
        Col("主表名", "from_table", 22, True),
        Col("类型", "ds_type", 14, choices=["valueSource", "default", "filter"],
            note="值来源 / 列表数据集 / 筛选器（filter 走「筛选器」模块单独导入）"),
        Col("唯一字段", "unique_field", 14),
        Col("ID字段", "id_field", 14),
        Col("名称字段", "name_field", 16),
    ],
}

SETTING_ROWS = [
    ("租户ID", "-999", SETTING_HELP["租户ID"][1]),
    ("文件前缀", "001", SETTING_HELP["文件前缀"][1]),
    ("默认模块ID", "", SETTING_HELP["默认模块ID"][1]),
    ("默认分类ID", "", SETTING_HELP["默认分类ID"][1]),
    ("输出目录", "out", SETTING_HELP["输出目录"][1]),
]

HELP_TEXT = [
    ("PowerProject 导入包生成器 — 使用说明", "title"),
    ("", ""),
    ("1. 填写顺序", "h"),
    ("先填「1_业务对象」→ 再填「2_字段」→ 然后「3_表单」「4_表单控件」→ 最后「5_列表」「6_数据集」。", ""),
    ("需要级联下拉就在「4_表单控件」补「级联父字段」；需要按条件隐藏字段就填「8_隐藏规则」。", ""),
    ("字段依赖对象编码，表单控件依赖字段编码，列表列依赖字段编码，填错会在 check 阶段报出来。", ""),
    ("", ""),
    ("2. 命名规则（系统硬性要求）", "h"),
    ("表单key / 列表key：小写，单词间用下划线，例如 create_project。", ""),
    ("字段编码：小驼峰，例如 projectName；物理列名留空会自动生成 project_name_。", ""),
    ("物理表名：小写+下划线，建议 pp_ 前缀，例如 pp_project_demo。", ""),
    ("物理列名：下划线分隔 + 末尾下划线，例如 sys_data_（系统会自动补，一般不用填）。", ""),
    ("不能用数据库关键字：order / desc / key / type / level / status 等。", ""),
    ("    编码叫 order 不要紧（列名会自动变成 order_），但物理列名千万别只写 order。", ""),
    ("", ""),
    ("2.1 编码留空会自动生成", "h"),
    ("「对象编码」留空 → 按「对象名称」翻成小写下划线，如 采购申请 → purchase_apply。", ""),
    ("「字段编码」留空 → 按「字段名称」翻成小驼峰，如 申请金额 → applyAmount。", ""),
    ("翻译用的是内置业务词典（项目→project、金额→amount、状态→status…），", ""),
    ("词典里没有的词会退成拼音，所以不影响出包，但建议扫一眼改成语义更好的。", ""),
    ("想用自定义编码就直接填，填了就不会被覆盖。", ""),
    ("", ""),
    ("3. 出包与导入（按模块来）", "h"),
    ("系统里每个模块是单独导入的，一个 zip 只能属于一个模块，混装会导入失败。", ""),
    ("生成结果按模块分成独立的包，按这个顺序逐个上传：", ""),
    ("  ① 业务对象   管理 › 业务对象 › 导入     包名 {对象} -业务对象-{日期}.zip   目录 bo/", ""),
    ("  ② 数据集     管理 › 数据集   › 导入     包名 {对象} -数据集-{日期}.zip     目录 data_template/", ""),
    ("  ③ 筛选器     管理 › 筛选器   › 导入     包名 {对象} -筛选器-{日期}.zip     目录 data_template/", ""),
    ("  ④ 表单       管理 › 表单     › 导入     包名 {对象} -表单-{日期}.zip       目录 form/", ""),
    ("  ⑤ 列表       管理 › 列表     › 导入     包名 {对象} -列表-{日期}.zip       目录 data_template/", ""),
    ("业务对象必须最先导，表单和列表都按表名去找对象。6_数据集 里「类型」填 filter 的归到筛选器。", ""),
    ("只想要某个模块时用 build -m form（逗号分隔，可写 bo,dataset,form,list）。", ""),
    ("", ""),
    ("4. 命令", "h"),
    ("python -m pp6kit check  <填充后的xlsx>   只校验，不生成", ""),
    ("python -m pp6kit build  <填充后的xlsx>   按模块生成可导入的 zip 包", ""),
    ("python -m pp6kit build  <xlsx> -m form   只生成表单模块的包", ""),
    ("python -m pp6kit inspect <导出.zip或目录>  把已有导出包反解成 Excel（推荐作为起稿方式）", ""),
    ("", ""),
    ("5. 关于「扩展参数」列", "h"),
    ("JSON 对象，用于覆盖控件/字段的系统默认参数。不知道怎么写就留空，绝大多数场景不需要填。", ""),
    ("", ""),
    ("5.1 分区：控件在页面上怎么分块（布局 V1.0）", "h"),
    ("「4_表单控件」的「分区」列填一句话，同名的控件会放进同一个卡片 / 分组框，", ""),
    ("例如都填「基础信息」。每个分区自动带一个「表格布局V1.0」，里面两列排（一行最多 2 个控件）。", ""),
    ("留空 = 整张表单只有一个分区，标题为「基本信息」，同样具备两列对齐。", ""),
    ("详情类表单用卡片（带标题栏），新建 / 条件专用 / 导入表单用分组框（更轻）。", ""),
    ("标了「占满整行」的控件单独一行；一行里只剩一个控件时自动占满整行。", ""),
    ("分区不用一样多字段，也不要求连续填写，程序按首次出现的顺序排列分区。", ""),
    ("", ""),
    ("6. 选项字段（数据类型=选项）怎么填", "h"),
    ("「2_字段」里数据类型选「选项」，再在「选项值」列填候选值。", ""),
    ("推荐一个选项一行：在格子里按 Alt+Enter 换行，例如", ""),
    ("    普通", ""),
    ("    紧急", ""),
    ("    特急", ""),
    ("顺序就是界面显示顺序，第一项为默认。也支持逗号分隔（普通,紧急,特急），两种混用也行。", ""),
    ("「关联对象/字典」列：类型=选项 → 可留空，程序按字段编码自动生成字典 key；", ""),
    ("类型=对象 → 填对象编码（project）、中文名（项目）、或对象表名（pp_project）都认，", ""),
    ("出包前会按系统里真实的业务对象自动对照成编码；留空则按字段名推断（「关联项目」→ project）。", ""),
    ("生成的业务对象包里会带上这些选项，导入系统后直接就有候选值，不用再去后台配字典。", ""),
    ("", ""),
    ("7. 内置字段不用填", "h"),
    ("系统会自动给每个对象加这些内置字段，模板里不需要也不应该填（填了会被忽略）：", ""),
    ("    id 主键 / tenantId 租户 / ip / deleted 删除标记 / createBy 创建人 / createTime 创建时间", ""),
    ("    updateBy 更新人 / updateTime 更新时间 / version 版本 / baselineVersion 基线版本 / isDraft 是否草稿", ""),
    ("另外，对象勾了「流程驱动=是」时，还会自动补 4 个流程字段：", ""),
    ("    processNode 流程节点 / processHandler 流程处理人 / processDefId 流程定义 / processInstanceId 流程实例", ""),
    ("", ""),
    ("8. 级联下拉 与 条件隐藏", "h"),
    ("级联：在「2_字段」的「级联父字段」填本对象里另一个字段的编码即可，例如", ""),
    ("    品种分类 填 productCategory、品种类别 填 classificationId。父字段可以是「选项」也可以是「关联对象」。", ""),
    ("    会同时写进业务对象属性（cascadeEnabled/cascadeParentCode）和表单控件两层——", ""),
    ("    表单控件那层没单独填时自动继承，所以只填一次就够；「4_表单控件」再填一次可以覆盖。", ""),
    ("    父级选了什么，子级只列它下面的项。但具体「哪些子项属于哪个父值」存在系统的字典数据里，", ""),
    ("    导入后还要在字典管理里把子选项挂到父选项下，否则下拉不会真的过滤。", ""),
    ("条件隐藏：在「8_隐藏规则」里一行一条：隐藏控件 / 依赖控件 / 判断条件 / 条件值。", ""),
    ("    条件成立就把控件藏起来。想表达「等于 X 时才显示」，就反过来写「不等于 X 时隐藏」。", ""),
    ("    同一个控件要「满足任一条件就隐藏」，就写多行（OR）；判断条件只有 等于/不等于/为空/不为空。", ""),
    ("    条件值可以直接写中文选项名（如 是），出包时自动换成系统里的选项值；", ""),
    ("    引用线上已有的共享字典时，填它原来的选项码（如 QT、XP）。", ""),
    ("", ""),
    ("9. 未覆盖的能力", "h"),
    ("流程定义（workflowDefinition）、数据权限、按钮脚本、明细表内嵌子表单暂不在本工具范围内，", ""),
    ("这类内容建议在系统界面里配置，或先 inspect 一份已有导出包再手工微调 XML。", ""),
]
