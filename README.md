# PP6 Studio

**PowerProject V6 配置同步工作台** · 内部包名 `pp6kit` · 桌面应用 `PP6 Studio.app`

把 Excel 规格表变成 PowerProject V6 可直接导入的同步包（业务对象 / 表单 / 列表 / 数据集）；
也能反解现有导出包、在原有 zip 上改字段做最小补丁、用自然语言查数据库。

## 为什么不是"照抄模板"

系统导出的 zip 内部是**强耦合的 XML**：表单控件的 `fieldOptions` 是 30+ 键的 JSON，
列表视图的 `attrs` 是上万字符的 JSON，字段顺序、CDATA、`&quot;` 转义都有讲究。
手写字面量必错。本工具的做法是：

1. `capture` 从你线上的导出包里**提取骨架**（19 种控件、4 种视图、栅格布局）
2. `build` 时**只替换差异部分**（字段、标签、数据源、列），骨架原样保留
3. 系统升级后重跑 `capture`，工具自动跟上新格式

## AI 能力（各工作区共用一套配置）

左侧栏「设置」工作区里配一次大模型，**导入包生成**和**数据查询**都能用。

```
侧栏   1 导入包生成  ·  2 已有包改字段  ·  3 数据查询  ·  4 设置 ●
       ↑ 数字键直达                              圆点：绿=已配置，灰=没配
```

| 功能 | 用 AI 做什么 | 不用 AI 会怎样 |
|---|---|---|
| 导入包生成 | 一句话 → 业务对象 + 字段 | 手填 Excel，功能完整 |
| 数据查询 | 规则答不上时兜底出 SQL | 只用本地规则，覆盖率约 80% |

### 一句话生成业务对象 + 表单 + 列表

导入页输入框描述需求：

```
员工报销单：报销单号、报销人、所属部门、报销金额、费用类型、发生日期、事由说明、附件
```

**三步流水线**（每步一次模型调用，输出小、成功率高、出错好定位）：

| 步骤 | AI 决定什么 | 参考的 JeecgBoot 技能 |
|---|---|---|
| ① 业务对象 | 对象名、编码、字段清单、字段类型 | `codegen` |
| ② 表单布局 | 分几组、每组放哪些字段、哪些必填、哪些占整行 | `desform` |
| ③ 列表视图 | 显示哪些列、默认排序、用哪些视图（表格/看板/甘特） | `onlreport` + `onlchart` |

页面用三个页签分别预览，确认后点「套用并下载 Excel」，
得到一份**对象、表单、列表都填好的 Excel**，接着改就能出包。

实测输出（"员工报销单"）：

```
【报销信息】 报销单号* / 报销人* / 所属部门
【费用详情】 报销金额* / 费用类型* / 发生日期*
【事由说明】 事由说明 / 附件
列表：表格视图 · 报销单号/报销人/报销金额/费用类型/发生日期 · 按发生日期倒序
```

> 带 `*` 的是 AI 判断的必填项。

**为什么分三步而不是一次问完**：一次让模型产出"对象+表单+列表"会是几千 token，
容易截断、出错也难定位。分步后每步失败可以单独降级（表单失败就退回默认布局），
不会因为一块坏了整个生成作废。

实测类型选择：

| 你说的 | AI 给的 |
|---|---|
| 报销单号 / 合同编号 | `autoNumber` 自动编号 |
| 报销人 / 负责人 / 审批人 | `user` 人员 |
| 所属部门 | `department` 部门 |
| 金额 / 数量 / 单价 | `number` 数字 |
| 备注 / 说明 / 应对措施 | `clob` 多行文本 |
| 所属项目 / 关联客户 | `object` 关联对象 |
| 附件 | `attachment` 附件 |

**为什么不让 AI 直接写 XML**：一份 bo XML 有 35KB、几百个转义字符和 18 位 ID。
模型写这个必错；写「对象名 + 10 个字段」的 JSON 很稳。
所以 AI 只产出**结构化清单**，XML 仍由既有模板生成——和 JeecgBoot 的 skills 是同一个思路。

### 视图生成能力与限制

| 视图 | 生成能力 | 说明 |
|---|---|---|
| `grid` 表格 | 完整 | 列、排序、显示宽度都写进 XML |
| `board` 看板 | 完整 | 自动挑「选项」字段分列（状态/级别/紧急程度） |
| `gantt` 甘特 | 完整 | 自动挑开始/结束两个日期字段 |
| `chart` 图表 | **只有空视图** | 系统导出包里本来就只有 3 个键，图表样式要在系统界面里另配 |

看板分列有个坑：**只能用「选项」类型的字段**。人员、部门、关联对象都不行——
「按申请人分列」= 一个人一列，「按所属项目分列」= 一个项目一列，
数据一多就碎成几十上百列，看板彻底没法用。所以对象里没有 dic 字段时，
看板视图会直接跳过并给出提示，而不是硬塞一个不合适的字段。

### 免费平台

| 平台 | 免费政策 | 申请 |
|---|---|---|
| 智谱 GLM（推荐） | GLM-4.7-Flash 约 30M tokens/Key | https://open.bigmodel.cn |
| 硅基流动 | 新用户送 2000 万 tokens | https://cloud.siliconflow.cn |
| 火山豆包 | Lite 每日 200 万 token（需手动开通模型） | https://console.volcengine.com/ark |
| DeepSeek | 新用户少量，之后极便宜 | https://platform.deepseek.com |

Key 存在 `~/.pp6kit/nlq.json`，换机器可以直接拷。

## 第二个模式：自然语言查数据（NL2SQL）

应用顶部可切到「数据查询」：连你们自己的库，实施用中文提问，自动出 SQL 并执行。

**为什么准**：不靠猜表名。库里平台自带的元数据表就是语义层——

| 元数据表 | 提供什么 |
|---|---|
| `iform_bo_def` | 业务对象（编码 / 中文名） |
| `iform_bo_table` | 物理表（`pp_xxx` / 中文表名 / 行数） |
| `iform_bo_attr` | 属性中文名、`dataTypeNew`、关联对象 |
| `iform_bo_attr_column` | **中文属性 → 物理列名** |
| `iform_cat_type/dic` | 选项字典（`key` → 中文） |

再叠加两层：
- **外键自动识别**：`object` 字段 → 目标表，生成子查询（`state_id_ IN (SELECT id_ FROM pp_project_state WHERE name_='执行中')`）
- **真实取值注入**：把关联表的实际值（`执行中/已立项/暂停中`…）写进提示词，而不是让模型猜

所以**客户自己扩的字段也能自动跟上**——只要建过对象，元数据里就有记录。

### 数据库画像（先摸库，再生成）

```bash
bin/pp6 profile        # 全库扫一遍：真实行数 / 软删除写法 / 枚举取值 / 空表
bin/pp6 dbmap          # 结构图谱（按业务域分组）
```

关系图谱见 [docs/关系图谱.md](docs/关系图谱.md)：中心表排名、两两实体最短路径、
**26 条有孤儿引用的关系**（`pp_project_budget_config` 44% 指向已删项目，
`pp_template_task.category_` 100% 失效）——所以生成 SQL 一律用 LEFT JOIN。

产物两份：`docs/数据库画像.md`（人看）+ `assets/dbprofile.json`（程序用）。

当前库实测结论（555 表 / 33.9 万行）：

| 指标 | 数值 |
|---|---|
| 业务表（pp_*） | 225 张，其中 **147 张有数据、78 张是空表** |
| 业务对象 | 192 个，**133 个有数据** |
| 软删除 `deleted_` 全为 NULL 的表 | **50 张** |
| 全为 'N' 的表 | 58 张 |

> **软删除是最大的坑**：用 `deleted_ <> 'Y'` 会把「全 NULL」的 50 张表整表过滤光
> （`pp_task` 601 行变 0 行）。现在统一写 `IFNULL(deleted_,'N') <> 'Y'`。

### 枚举值与中文翻译

库里的状态/优先级存的是**英文码**（`inProgress` / `high` / `under_tracking`），
字典表只覆盖一部分，直接拿中文去比永远是 0 行。工具的做法：

1. 连库时给每张表的枚举列采样真实取值（`COUNT(DISTINCT)` + `GROUP BY`）
2. 用 `iform_cat_dic` 的 key→中文 + 全局码表（449 条）反推「中文 → 码」
3. 用户说「进行中」→ 自动翻成 `in_progress`；说「高优先级」→ `high`

实测映射（`pp_issue`）：

```
状态     进行中=in_progress、已通过=pass、已拒绝=rejected、开始=start
优先级   高=high、中=medium、低=low
风险等级 高风险=High、中风险=Medium、低风险=Low
开发任务 已发布=released、未开始=notStarted、开发中=developing
```

### 跨对象查询：自动补多跳 join

两张表之间没有直接外键也能查——按关系图算最短路径（`pp6kit/nlq/graph.py`）：

```
客户 ←合同→ 项目        （2 跳，靠 pp_contract.project_ids_ 多值关联）
产品 ←项目→ 客户        （1 跳）
开发任务 ←项目→ 客户     （3 跳）
```

实测 197 条关系，全库是**一个连通块（111 张表）**，没有孤立表。
`pp_project` 是绝对中心（47 条连接），其次 `pp_classification`(19)、`pp_product`(15)。

问「每个客户有多少项目」会自动生成：

```sql
SELECT COALESCE(r_customer_id_.name_, '未关联') AS 客户, COUNT(*) AS 数量
FROM pp_project t
LEFT JOIN pp_contract j_contract ON FIND_IN_SET(t.id_, j_contract.project_ids_) > 0
LEFT JOIN pp_customer j_customer ON j_customer.id_ = j_contract.customer_id_
GROUP BY COALESCE(r_customer_id_.name_, '未关联')
```

> 27 个项目里有 13 个挂了合同、能查到客户，另外 20 个归到「未关联」——
> 这是数据现状，不是查错。

### 相对时间

「本月」「上周」「最近 7 天」「今年」都能识别，自动选时间列：

| 说法 | 生成的 WHERE |
|---|---|
| 本月 | `DATE_FORMAT(col,'%Y-%m') = DATE_FORMAT(CURDATE(),'%Y-%m')` |
| 上周 | `YEARWEEK(col,1) = YEARWEEK(CURDATE(),1) - 1` |
| 最近 7 天 | `DATE(col) >= CURDATE() - INTERVAL 7 DAY` |
| 今年 | `YEAR(col) = YEAR(CURDATE())` |

选列规则：问句点名的日期字段 > 语义关键词（到期→`due_time_`、开始→`start_`）> `create_time_`。

### 空结果会告诉你为什么

0 行不再只回一句「没有数据」：

```
「总预算」只有 6/32 行有值，范围 0 ~ 2,800,000；条件 10,000,000 没落在范围内。
「任务名称」里没有包含「电池」的记录。
「项目预算」在当前库里没有数据（该模块还没启用），换个对象试试。
pp_task（任务）601 行，去掉筛选条件能查到 601 行 —— 是筛选值没匹配上。
```

### 结果怎么变的（本地引擎的取列规则）

| 你的说法 | 生成的列 |
|---|---|
| 列出所有项目 | 整行（识别不到具体字段时不瞎猜） |
| 列出所有项目及其状态、级别、项目经理、项目经理所在部门 | 项目名称 + 状态 + 级别 + 项目经理 + 项目经理所属部门 |
| 查询项目编号和项目名称 | 正好这两列，按你说的顺序 |

- **自动带标题列**：只点名了几个字段时，会把「项目名称」补到最前（否则「列出」类问题只有状态一列，看不出是哪个项目）
- **多值外键**：一个项目挂多个经理时（`manager_ids_` 存 `id1,id2`），用 `GROUP_CONCAT` 聚合成「甲、乙」，不会把一行拆成两行
- **人员/部门两跳**：`项目经理所在部门` / `项目经理的所属部门` 都能识别，走 人员表 → 组织表
- **包含式条件**：`项目名称里带芯片` / `项目名称包含电池` → `LIKE '%芯片%'`

### 复制 SQL

SQL 卡片右上角有「复制 SQL」：

- 桌面版走系统剪贴板（macOS `pbcopy` / Windows `clip`），中文和多行都完整
- 网页版走浏览器 Clipboard API，失败时退回选中 + 复制，再失败会提示手动复制

### 不配大模型也能用

实现同事现场不一定有 API Key，所以内置了一个**本地规则解析器**（`pp6kit/nlq/local.py`）：

- 认对象名 + 字段名（`项目名称`、`项目经理`、`项目状态`…）
- 自动补 join：外键 → 目标表、人员 → `iform_party_employee`、部门 → `iform_party_org`
- 认「人员的所属部门」这种两跳关系（人员 → 组织）
- 认统计语句（`每个X有多少个`、`按X统计`）→ `COUNT` + `GROUP BY`
- 认「最近/最新」→ 按创建时间倒序
- 认条件（`状态是执行中`）→ 自动区分外键子查询 / 字典值 / 模糊匹配

**大模型不是必须的**，它是「问法很自由时才需要」的加分项。界面上有 Key 走大模型，没有就走本地。

### 结构图谱

```bash
bin/pp6 dbmap        # 生成 docs/数据库结构图谱.md
```

输出：表分类统计、按业务域分组的表清单、606 条对象关系（标注**实测有数据/整列为空**）、常用 join 路径。
换一个客户的库重跑一次，就得到他那套库的图谱。

### 使用

1. 左侧填连接串（host / port / user / 密码 / 库名）→ 点「连接并读取结构」
2. 直接点下方模板（项目清单、按状态统计…），或用一句话提问
3. SQL 可手改后重新执行；结果可导出 CSV
4. 觉得答得准就点「存为示例」，下次同类问题会作为 few-shot 参考

### 大模型（可选）

不填也能用内置模板。「生成 SQL」需要 OpenAI 兼容接口：

| 服务 | base_url | model |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| 本地 Ollama | `http://127.0.0.1:11434/v1` | `qwen2.5-coder` |

配置存在 `~/.pp6kit/nlq.json`（不进程序目录，方便随机器迁移）。

### 安全

生成结果一律过闸，用 **sqlglot 解析成 AST 再判断**（不是正则匹配）：

- 只放行 `SELECT` / `WITH ... SELECT`，语句数必须为 1
- 从 AST 取真实表名（自动排除 CTE 别名）比对白名单
- 拒绝 `information_schema` / `mysql` / `sys` 等系统库
- 拒绝 `INTO OUTFILE`、`SLEEP`、`BENCHMARK`、`LOAD_FILE` 等
- 自动补 `LIMIT 500`（已有更大的值也压下来）

11 个绕过尝试全部拦截（多语句、写操作、系统库、危险函数、导出文件）。

## 业务对象的编码和表名怎么来

对象编码会派生物理表名（`tblName`），表名是导入时真正建库用的名字，
所以这一层必须按系统的命名习惯走，不能随手拼。

`1_业务对象` 里**编码留空**就按对象名称自动生成，规则来自六份原生包：

| 对象名称 | 生成的编码 | 物理表名 | 线上真实对象 |
|---|---|---|---|
| 项目 | `project` | `pp_project` | ✅ 完全一致 |
| 项目立项 | `project_create` | `pp_project_create` | ✅ 完全一致 |
| 需求类型 | `requirement_type` | `pp_requirement_type` | 线上叫 `requirement_category` |
| 测试用例库大类 | `test_case_library_category` | `pp_test_case_library_category` | 线上叫 `test_case_base_category` |

做法是**英文词表最长匹配**（`pp6kit/naming.py` 的 `WORDS`），连续查不到的片段才退拼音。
和字段编码不一样：字段编码走拼音（`to_english`，`riskLevel` 这种驼峰），
对象编码走英文语义（`object_code`），两者刻意分开。

**物理表名留空也会自动补** `pp_ + 编码`，不用手填；编码本身已经带 `pp_` 的原生对象
（实测「设备」`code = tblName = pp_equipment`）直接沿用，不会拼成 `pp_pp_equipment`。

改 `WORDS` 里没有的词就继续加词条，加完重跑：

```bash
.venv/bin/python -c "from pp6kit.naming import object_code; print(object_code('质量管理'))"
```

## 导入前改包：加字段 / 改已有字段（增量补丁，不是重新生成）

顶部「已有包改字段」这一栏解决的是另一种需求：**手上已经有一份能导入的 zip，
在导入之前把它改对** —— 给业务对象补几个字段，或者改已有字段的类型、选项值。
原包可能是系统「推送工作区」导出的，也可能是本工具以前出的整包。

这件事必须在**导入之前**做：包一导进去，表就按当时的字段类型建好了，之后在线上
改类型只是改元数据，不会 `ALTER` 物理列（见下一节）。

### 为什么不用「反解现有导出包」

反解会把 zip 拆成 Excel、再由生成器**重新造一份包**。对原生推送包来说这是有损的：
对象角色、操作日志模板、字典展示样式、表单权限这些我们没建模的内容会丢。

增量补丁走的是另一条路 —— **只改被点名的 `bo/*.xml`**：

- 在 `bodefentity` 最后一个 `<attribute/>` 后插入新字段，字段行由
  `gen_bo._attr_line(BoField.resolved())` 生成，和从 Excel 新建出来的属性集合完全一致；
- 选项字段同时补 `dictionaryTypeList` / `dictionaryList`（含 `attrs` 展示样式）；
- 单行紧凑格式的原生 XML 仍保持单行，多行格式的生成器产物仍保持多行；
- 其余条目**逐字节复制**，条目顺序、时间戳、压缩方式、Unix 权限位一并保留；
- zip comment 沿用系统原生 `CONFIG_SYNC_NOTE:` 格式，里面是这次的改动报告。

`tools/check_patch.py` 会把上面每一条都断言一遍（当前 61/61），
包括「除插入外没有删除/替换」「新 sn 不撞已有 sn」「新包可完整解压」
「覆盖时物理列名不变」「改选项值时复用具名 key」。

### 怎么改已有字段（类型 / 选项值）

表里就一张字段表，**编码是新的就是加字段，编码和包里已有的一样就是改那个字段**：

1. 上传原包，勾上对象，在「③ 填字段」里写一行：`字段编码` 填已有字段的名字
   （对象卡片展开「查看已有字段」可以抄），`数据类型` / `选项值` 填新值；
2. 到「④ 生成新包」把**重名字段**从默认的「跳过」改成「按新定义覆盖」；
3. 点「预览改动」看报告（会写 `~ name 项目名称 按新定义覆盖 varchar→user`），再下载。

⚠ 默认模式是「跳过」：填了同名编码但没改模式，产物和原包**一模一样**。
以前这种包会被静默输出、报告里还写「已加上」，现在直接报错：
`没有产生任何改动：name（包里已有同名字段）…`。

覆盖的时候只改你动过的键，其余原样保留：

- `fieldName` 物理列名**永不改**（改名等于要求线上 `ALTER`）；
- `sn` 行序号、`type` 角色列、以及我们没建模的属性（`roleList` / `formula`…）保留；
- 物理类型没变（字符串↔用户↔部门↔选项…都是 `varchar`）时，`length` / `format`
  保留原值，不会因为选了「用户」就把长度 200 悄悄改成 64；
- 改了「选项」字段的选项值 → 重写这个字段的 `dictionaryList`，**同名选项复用原 key**
  （key 是库里已存的值，重新生成会让已有记录全部对不上）。

### 页面怎么用

1. 上传/拖入原 zip → 列出包里所有 `bo/*.xml`（重复推送的副本会标出来）；
2. 勾要处理的对象（可多选，同一批字段会加到每个选中的对象上）；
3. 填字段：表格逐行加、从 Excel 粘贴（Tab 分隔）、或「下载字段表模板 → 填好 → 导入」；
4. 「预览改动」先看报告，再「下载新 zip」。

四个开关：

| 开关 | 作用 |
|---|---|
| 重名字段 | `跳过`（默认，只加新字段，同名的不动）/ `按新定义覆盖`（改已有字段的类型、选项值就用这个） |
| 包内容 | `保留全部条目`（默认）/ `只留 bo/ 目录`（最小增量包，导入时不会碰表单、列表） |
| 同步改所有副本 | 包里同一对象被重复推送出多份 XML 时，一起打补丁 |
| 每个对象单独出包 | 一次下载多个 zip，每个只含一个对象的改动 |

产物名会写明这次做了什么：只加字段叫 `xx-加字段-日期.zip`，改已有字段叫
`xx-改字段-日期.zip`，两样都有就是 `xx-加字段+改字段-日期.zip`。

另外还有「下载带新字段的规格 Excel」：把原包反解成完整规格表、把新字段并进
`2_字段`（勾选「同步补表单控件」时也会加到 `4_表单控件`），适合接着调整表单/列表，
再走主流程重新出一份完整包。

> 新字段只进**业务对象**。想让它出现在表单/列表上，要么用上面的规格 Excel 重新出包，
> 要么在系统表单设计器里手动加控件 —— 增量包本身不会动表单 XML。

### 字段表模板列顺序

`字段编码 / 字段名称 / 数据类型 / 选项值 / 关联对象或字典 / 长度 / 精度 / 默认值 /
必填 / 建索引 / 物理列名 / 字段说明`。数据类型可写中文（`字符串`、`选项`、`对象`…）
也可写代码（`varchar`、`dic`…）。字段编码必须英文小驼峰且不能与包里已有字段重名，
物理列名留空自动生成；撞了已有物理列名会直接报错而不是悄悄改列。

## 导入之后：线上还能改什么

包导进系统后，能不能再在线改字段类型，**取决于字段类型，不取决于包是谁生成的**。

| 字段类型 | 线上「数据类型」下拉 | 为什么 |
|---|---|---|
| 字符串 / 用户 / 部门 / 选项 / 对象 / 用户组 / 自动编号 | ✅ 可改（这 7 项之间任意换） | 物理列都是 `varchar`，改类型不动表结构 |
| 数字 / 日期时间 / 大文本 / 日期 | ❌ 置灰 | 改它要 `ALTER TABLE`，平台不在线上做 |

平台前端写死了这份白名单（`equalAttrsMap`），**原生对象一样受限**——拿系统自带的
「项目立项信息」试，`number` 字段同样置灰。所以要改数字↔文本，只能改规格 Excel 重新出包。

改类型的具体走法：`数据 › 业务对象 › 点卡片进详情 › 对象属性`，
**在字段行上右键**开右侧抽屉 → 改「数据类型」→ 保存。
（那张表是 canvas 画的，DOM 里查不到行，自动化脚本别按文本找。）

改动只改语义类型：`varchar` → 「用户」之后物理列仍是 `varchar`，历史数据不受影响。

> 注意：控件类型是**表单层独立配置**的，改了 BO 字段类型不等于表单里的控件也跟着变。
> 两者要不要同步、怎么同步，以表单设计器里看到的为准。

## 界面布局（桌面工具范式）

界面按「固定侧栏 + 上下文栏 + 独立滚动工作区 + 底部状态栏」组织：**窗口本身不滚动，
只有中间工作区滚动**，顶栏标题、状态栏与 AI 状态灯跟着工作区切换。

| 区域 | 作用 |
|---|---|
| 左侧深色导航 | 4 个工作区：导入包生成 / 已有包改字段 / 数据查询 / 设置；下方标注导入路径 |
| 顶部上下文栏 | 面包屑 + 当前工作区标题 + 一句话说明；右侧 AI 状态胶囊与「反解现有导出包」 |
| 中间工作区 | 唯一滚动容器，内容最大 1360px；卡片、表格、分段页签密度统一 |
| 底部状态栏 | 最近一次操作提示、当前工作区、本地时间与运行状态 |

快捷键：`1`–`4` 直达工作区，`⌘/Ctrl + K` 回到「导入包生成」（输入框聚焦时不抢按键）。
窗口窄于 1120px 时侧栏收成图标条，窄于 720px 时隐藏侧栏；查询 / 设置的双栏布局在
1000px 以下自动改单栏。

样式全部在 `pp6kit/static/style.css`：顶部 `:root` 是设计 token（色板 / 圆角 / 阴影 / 骨架尺寸），
末尾「统一覆盖层」把历史组件里散落的硬编码色值收敛到 token。改界面只动 html/js/css，
用 `tools/sync_static.py` 同步进已打包的 App 即可，不用重新打包。

## 桌面应用（发给同事用这个）

`dist/PP6 Studio.app` 是自带 Python 运行时的独立程序，对方无需安装任何东西：

1. 把整个 `.app` 压缩后发给同事（微信 / 内网盘都行）
2. 对方解压后拖进「应用程序」文件夹，双击打开
3. 首次打开若提示「无法验证开发者」：右键点图标 → 打开 → 再点「打开」

命令行解除隔离（可选）：

```bash
xattr -dr com.apple.quarantine "/Applications/PP6 Studio.app"
```

界面：拖入（或点击选择）Excel → 立即看到对象/字段/表单布局 → 校验结果 → 一键生成 zip。

> **文件进出走系统原生对话框**，不依赖浏览器下载语义。
> WKWebView / WebView2 里 `<a download>` 是无效的、隐藏的 file input 也唤不起选择框，
> 所以桌面版的「选择文件 / 保存」都调 `pp6kit/desktop_api.py` 的原生接口；
> 网页模式（`bin/pp6 ui`）仍走浏览器下载，两条路径互不影响。

### 重新打包

```bash
./build/build_macos.sh          # macOS → dist/PP6 Studio.app
build\build_windows.bat          # Windows → dist\PP6ImportBuilder\PP6ImportBuilder.exe
```

Windows 版必须在 Windows 机器上打（PyInstaller 不能交叉编译）。
如果没有 Windows 电脑：把项目推到 GitHub，手动跑一次 `.github/workflows/build-desktop.yml`，
在 Actions 的 Artifacts 里下载 `.exe`。

> 分发 Windows 版要发**整个文件夹**，不能只发 exe（它依赖同级 `_internal`）。

### ⚠️ 改完界面看不到变化？

PyInstaller 把 `pp6kit/static` **打进了 dist/**，改源码不会自动生效。
如果你改的是 `index.html` / `app.js` / `query.js` / `style.css`，
不用重新打包，直接同步（几秒）：

```bash
.venv/bin/python tools/sync_static.py          # 同步到已打包的 App
.venv/bin/python tools/sync_static.py --check  # 只检查是否一致
```

然后**完全退出 App（Cmd+Q）再打开**——只关窗口 WKWebView 可能还在用缓存。

只有改了 `.py` 逻辑，才必须重新打包。

## 命令行安装

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## 用法

```bash
./bin/pp6 ui                    # 打开可视化界面（本地起服务 + 浏览器）
.venv/bin/python -m pp6kit.app  # 桌面窗口模式

# 1. 学习骨架（已有 assets/snippets.json 可跳过；系统升级后重跑）
.venv/bin/python -m pp6kit capture PP6

# 2. 生成填写模板（内置一套"采购申请"示例，改字即用）
.venv/bin/python -m pp6kit template out/模板.xlsx

# 3. 校验（不生成）
.venv/bin/python -m pp6kit check out/模板.xlsx

# 4. 出包（按模块，每个模块一个独立的包）
.venv/bin/python -m pp6kit build out/模板.xlsx -o out/build

# 只要某个模块的包
.venv/bin/python -m pp6kit build out/模板.xlsx -m form        # 只出表单
.venv/bin/python -m pp6kit build out/模板.xlsx -m bo,dataset  # 出对象 + 数据集

# 起稿捷径：拿一份线上跑通的导出包反解成 Excel，改字段再出包
.venv/bin/python -m pp6kit inspect PP6 -o out/基于现有配置.xlsx
```

### 自检命令

```bash
.venv/bin/python -m pp6kit probe              # 183 条中文问答语料回归（当前 183/183）
.venv/bin/python -m pp6kit audit              # 规则引擎盲区审计
.venv/bin/python -m pp6kit audit --sweep      # 全表全列扫描（查幽灵列）
.venv/bin/python tools/selftest.py            # 端到端自检（当前 3/5 基线，失败两项是原生包同名对象告警）
.venv/bin/python tools/check_patch.py         # 增量补丁保真度（当前 57/57）
.venv/bin/python tools/sync_static.py --check # 打包产物里的前端是否最新
```

## HTTP 接口

文件类接口都是裸字节 POST（不走 multipart），文件直接当 body；
配置类接口用 JSON body（下面标了 `JSON`）。

| 接口 | 入参 | 返回 |
|---|---|---|
| `POST /api/parse` | xlsx 字节 | 结构 + 模块清单 + 校验结果 |
| `POST /api/build` | xlsx 字节，头 `X-Modules` | 按模块分包的 zip |
| `POST /api/import/<模块>` | xlsx 字节 | 单个模块的 zip |
| `POST /api/inspect` | zip 字节 | 反解出的 xlsx |
| `POST /api/ai/bo` | `{description, known}` | 业务对象 + 字段清单 |
| `POST /api/ai/bo/apply` | `{bo}` | 填好的完整 Excel |
| `POST /api/nlq/ask` | `{question, engine}` | SQL + 结果（engine: hybrid/local/llm） |
| `POST /api/nlq/test-llm` | 可空 | 连通性测试（空则用已存配置） |
| `GET /api/patch/fields-template` | — | 新增字段表模板 xlsx |
| `POST /api/patch/inspect` | zip 字节 | 包内对象清单 + `token`（原包按内容哈希缓存，后续不必重传） |
| `POST /api/patch/fields` | xlsx 字节 | 从增量字段表读出的字段清单 |
| `POST /api/patch/dry-run` | `JSON` `{token, targets, fields, mode, scope, allCopies}` | 改动报告（与真实出包同源） |
| `POST /api/patch/build` | 同上 | 打好补丁的新 zip |
| `POST /api/patch/workbook` | 同上 + `syncForm` | 带新字段的完整规格 Excel |

> `engine` 参数名不是 `mode`，传错了会静默走默认值。

## 回归测试（每次迁功能都要跑）

`tests/` 下有一套零依赖的回归用例，覆盖从 Excel 解析到 HTTP 接口的整条链路，
当前 **257 条**，全量跑完约 8 秒。

```bash
./.venv/bin/python tools/regress.py                    # 全量
./.venv/bin/python tools/regress.py --list             # 只列清单（带编号）
./.venv/bin/python tools/regress.py -m patch           # 只跑改包
./.venv/bin/python tools/regress.py --only WEB         # 按编号前缀
./.venv/bin/python tools/regress.py --level P0         # 只跑核心链路
./.venv/bin/python tools/regress.py -x                 # 首个失败即停
./.venv/bin/python tools/regress.py --report docs/测试用例清单.md   # 重写清单
```

不需要装 pytest，用的是自带 runner（`tools/regress.py`），退出码 0/1 可直接接 CI。
用例清单见 [`docs/测试用例清单.md`](docs/测试用例清单.md)，**由注册表生成，不要手改**。

### 覆盖范围

| 模块 | 条数 | 守什么 |
|---|---|---|
| `naming` | 20 | 对象编码/物理表名与系统真实命名一致，不拼 `pp_pp_` |
| `reader` | 23 | Excel 容错：编码留空自动生成、坏行报错、选项换行解析 |
| `validate` | 22 | 该拦的拦住（表名大写、明细表缺来源、导入表单带系统字段），不该拦的不拦 |
| `bo` | 21 | 字段属性、字典、系统字段、父子关系、日期字段不被平台隐藏 |
| `form` | 18 | 布局 V1.0 契约：parentId / columns↔rows / 两份 field_options / sn 连续 |
| `builder` | 15 | zip 顶层目录、条目编号、模块过滤、校验有错不出包 |
| `patch` | 20 | 改包：加字段、覆盖改类型、改选项值真的写进字典 |
| `webui` | 21 | 所有 HTTP 接口的正常与错误分支、路径穿越、30MB 上限 |
| `security` | 12 | 坏 zip、`]]>`、`<script>`、emoji、200 字段宽表 |
| `nlq` | 85 | 自然语言转 SQL：安全闸（锁定读/写操作/跨库/导出）、答案质量判定、表关系 join、选表、否定与数值条件 |

### 迁功能时的固定动作

1. 先跑全量，确认基线是绿的（有红的先判断是不是既有问题）；
2. 改代码；
3. 再跑全量 —— 失败说明真改坏了，不要绕过；
4. 新行为补成用例（见下），`--report` 更新清单；
5. 涉及 `.py` / `assets/` / `static/` 时打包：`./build/build_macos.sh`。

新增接口至少要两条：一条正常返回、一条坏输入；新增字段类型要在 `bo` 和 `form`
各补一条，断言 XML 里的**真实属性**，不要只断言中间变量。

### 用例怎么写

```python
@case("PAT21", "patch", "改字段类型后表单控件跟着变", "P1",
      ["form/ 里的 fieldType 不再是 text"])
def _():
    ...   # 直接 assert，失败信息就是给人看的
```

- 编号 `模块前缀 + 两位序号`，**发布的编号不复用**，删了留空号；
- 标题写用户能感知的行为，不写「测试某函数」；
- `tags` 写这条用例真正守住的东西，回归失败时一眼看出影响面；
- 只能人工确认的（连库、大模型）用 `skip("原因")`，并在验收单里写手工步骤。

## 开发约定

几个反复踩到的，写下来省事：

1. **AI 只产出结构化清单，不写 XML** — 生成器负责模板，模型负责语义。
2. **改前端先跑 `sync_static.py --check`** — 否则你以为改了，实际 App 里还是旧的。
3. **`getElementById` 返回 null 不报错** — 删元素后要全局搜旧 id，否则状态静默不更新。
   （踩过：`paintAiState()` 引用了已删除的 `aiCfgLabel`，灯一直不亮）
4. **调用模型必须传 `max_tokens`** — 不传会一直写到自然结束，生成类任务能拖到 100 秒。
5. **降级链要区分「模型没开通」和「Key 无效」** — 前者该跳过，后者该报错，混在一起就两头不讨好。

## 版本控制与 GitHub

代码托管在 **github.com/Asaki1999/pp6**（私有仓库），推送走 SSH 免密。

### 日常操作（改完代码后）

```bash
git add -A
git commit -m "改了什么，一句话"
git push
```

约定：**改完代码先 commit 再打包**（`dist/` 产物本身不入库，源码丢了 dist 救不了）。

### 新电脑上克隆

```bash
git clone git@github.com:Asaki1999/pp6.git
cd pp6
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-desktop.txt
```

克隆下来就能开发，但注意 **`PP6/` 不在仓库里**（真实系统导出包 + 客户表单，94M 且含业务
数据）——`tools/selftest.py` 依赖它，需要从线上系统重新导出几个包放进 `PP6/业务对象/`。

### SSH 密钥（推送权限的唯一凭证）

本机密钥是 `~/.ssh/id_ed25519`（注释 `pp6-mac`），对应 GitHub 上 Title 为 `pp6-mac`
的那把钥匙。**换了电脑 / 密钥丢了**：

```bash
ssh-keygen -t ed25519 -C "pp6-mac" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub        # 复制输出
```

打开 https://github.com/settings/ssh/new → Title 随意 → 粘贴 → Add SSH key，
然后 `ssh -T git@github.com` 见到 `Hi Asaki1999!` 即通。

### 什么在仓库里、什么不在

| 在仓库 | 不在仓库（.gitignore）|
|---|---|
| `pp6kit/` 全部源码 + `assets/` 快照资源 | `PP6/`（客户业务数据，94M）|
| `tools/`、`docs/`、`build/`（脚本与 spec）| `dist/`、`build/work/`（可重建产物）|
| `out/规范2.xlsx`（测试样本）| `out/` 其余输出、`.venv/`、`backup/` |

## 从线上取一份真正的样本包（发布工作区）

生成器的格式是照着线上产物一条条对的，不是猜的。要对新版本/新环境，就去后台
**自己导一个包下来**——`系统管理 › 发布管理 › 发布工作区` 就是系统自己的导出入口，
它产出的 zip 和「导入同步包」吃的 zip 是同一套格式。

> 不要去找「导出配置」之类的菜单，也没有「业务对象 › 导出」这种单点入口。
> 系统的设计是：任何配置被**改动**就会落进「变更区」，从变更区挑进「发布工作区」，
> 再从工作区发布成 ZIP。所以「造一个包」= 先让配置产生一次变更。

### 操作步骤（2026-11-12 实测，全流程约 2 分钟）

1. 登录后台，左下 `系统管理` → 顶部页签 `发布管理`。
2. 点 `变更区`。这里列的是全系统最近改过的配置：`配置类型`（业务对象/表单/数据模板）、
   `Code`、`配置名称`、`最近动作`（新增/修改/删除）、`最近修改人/时间`。
3. 勾选要导出的行。**优先挑「修改」或「新增」的，别挑「删除」**——删除动作的包只带
   id 和删除标记，拿不到字段和布局。三类各挑一条最值：
   - `业务对象` → 看 `bo/*.xml` 的字段表
   - `表单` → 看 `form/*.xml` 的容器与布局
   - `数据模板` → 看 `data_template/*.xml` 的列配置
4. 勾完后右上出现三个按钮，点 **`加入发布工作区`**（不要点「发布 ZIP」，
   那样会直接在变更区发布，工作区里看不到、也无法挑选）。
5. 切到 `发布工作区` 页签，确认那几行在列表里（带 `推送时间`），点右上 **`发布 ZIP`**。
6. 弹出 `发布说明` 输入框（**上限 500 字**），填一句能认出这次导的是什么，例如
   `PP6 Studio 样本采集 20261112-1230 对象1+表单1+列表2`。
   **这段文字就是 zip 的 comment**，包内写的是 `CONFIG_SYNC_NOTE:<发布说明>`。
7. 点 `发布`。浏览器开始下载，工作区随即清空。

**变更区里没有想要的那条怎么办**：去 `数据` / `页面` 里打开那个配置，
不改任何东西直接点保存，它就会作为「修改」落进变更区。

### 下载后先看一眼

Chrome 有时把文件存成 `未确认 377157.crdownload`（下载未改名），**它其实已经是完整的 zip**，
直接解包即可：

```bash
unzip -l "$HOME/Downloads/未确认 377157.crdownload"    # 先看清单
unzip -o "$HOME/Downloads/未确认 377157.crdownload" -d /tmp/native
```

一个真实样本长这样（就是本仓库 `docs/原生包样本-20261112/` 里那两个）：

```
bo/001-pp_review_ext_check_item.xml
form/002-pmscs.xml
form/003-xjmbcsyltest.xml
data_template/004-zl.xml
```

三个可直接对照的事实：

- **序号跨目录连续**（1→4，不是每个目录各自从 1 开始），`comment` 是 `CONFIG_SYNC_NOTE:…`
- **一个包可以混装三类配置**，不需要分包
- `bo/*.xml` 的系统字段顺序是
  `id → tenantId → ip → createBy → createTime → updateBy → updateTime → deleted → version`，
  `deleted` 是 `varchar(1)`

### 拿到包之后干什么

不要手工改包。把它当**样本**读，把差异落回生成器，再让以后所有包都符合：

```bash
# 反解成 Excel 看得懂的形式
.venv/bin/python -m pp6kit inspect /tmp/native/native-121230.zip -o /tmp/样本.xlsx
# 改完生成器，用同一份 Excel 重出包，比对是否已具备样本特征
.venv/bin/python -m pp6kit build /tmp/样本.xlsx -o /tmp/out
.venv/bin/python tools/check_form_layout.py /tmp/out
```

## 按模块导入

系统里每个模块（业务对象 / 表单 / 列表 / 数据集 / 筛选器）**都有自己的导入菜单**，
只导某一块时按模块传更省事。但**不必分包**——`系统管理 › 发布管理 › 导入同步包`
可以一次吃完整个 zip，服务端按 `bo/ form/ data_template/` 目录名识别类型
（线上自己的发布包就是一个 zip 里混装三类）。

生成结果按模块各出一份，放在 `out/build/按模块/`：

| 顺序 | 包名 | 系统里点哪儿 | 包内目录 |
|---|---|---|---|
| ① | `001-业务对象.zip` | 管理 › 业务对象 › 导入 | `bo/` |
| ② | `001-数据集.zip` | 管理 › 数据集 › 导入 | `data_template/` |
| ③ | `001-筛选器.zip` | 管理 › 筛选器 › 导入 | `data_template/` |
| ④ | `001-表单.zip` | 管理 › 表单 › 导入 | `form/` |
| ⑤ | `001-列表.zip` | 管理 › 列表 › 导入 | `data_template/` |

- **业务对象必须最先导**，表单和列表都按表名去找对象。
- 列表 / 数据集 / 筛选器在系统里是**三个菜单**，底层都是 `data_template/`。
  `6_数据集` 的「类型」列填 `filter` 就归到筛选器，填 `valueSource` / `default` 归到数据集。
- 界面里每行右侧有「导入 / 导出」两个按钮：**导入**直接给该模块的单包，**导出**只发选的模块。
  导入成功后该行打上「已导入」标记，后续模块的「建议先导入业务对象」提示会自动消失。
- 流程定义（`workflowDefinition`）是裸 XML、由流程设计器导出，生成器不产生流程包。

单条 XML 也还保留在 `out/build/bo|form|data_template/` 下，需要单独传某个文件时用。

```bash
# 用命令行也能只导一个模块
.venv/bin/python -m pp6kit build out/模板.xlsx -m dataset
```

## Excel 结构

| 表 | 作用 | 关键约束 |
|---|---|---|
| `0_项目设置` | 租户、模块ID、分类ID、文件前缀 | 模块ID/分类ID 空着会警告 |
| `1_业务对象` | 对象编码、名称、物理表名 | 表名小写下划线，建议 `pp_` 前缀 |
| `2_字段` | 字段编码、类型、长度、精度 | 物理列名留空自动生成 `xxx_` |
| `3_表单` | 表单key、绑定对象、布局 | 表单key 小写下划线 |
| `4_表单控件` | 控件类型、标签、取值来源、**分区** | 字段编码必须存在于 `2_字段`（`dataTemplate` 例外，填列表 key）；分区同名的归到一个卡片 |
| `5_列表` | 视图类型、显示列、排序 | 列填字段编码 |
| `6_数据集` | 下拉框取值来源 | 主表名 |
| `7_表单脚本` | JForm 脚本（JS） | 表单key 必须存在；整段写入 `extendAttr.script` |

每张表的第 2 行是灰色填写说明（以 `#` 开头），读取时自动跳过，不用删。

## 自动处理的事

- **系统字段补齐**：`id / tenantId / ip / createBy / createTime / updateBy / updateTime / deleted / version`
  缺失时自动补，不会重复补；顺序照线上发布包排（`updateBy` 在 `updateTime` 前），`deleted` 是 `varchar(1)`
- **物理列名推导**：`projectName` → `project_name_`
- **表单布局（布局 V1.0）**：按「分区」把控件分成若干组，每组一个容器（详情表单用 `card`、其余用 `fieldset`）+ 一个 `grid_layout`（表格布局V1.0）；
  容器里同时写 `columns`（扁平单元格引用）和 `rows`（每格内嵌完整控件 JSON），控件 `parentId` 指向布局控件；
  一行最多 2 个控件（`span=12`），富文本/附件/标记"占满整行"的单独一行（`span=24`）
- **明细表控件（`dataTemplate`）**：控件类型填 `dataTemplate`、取值来源填 `5_列表` 的列表 key，
  会把该列表嵌进表单。它**不经过** `grid_layout`，单独占一个容器、`parentId` 直接指容器
  （跟线上一致：项目汇报表里 4 个明细表都是这个形态）
- **表单权限**：按控件自动生成 `formRights` + `permission` JSON
- **列表列**：字段编码自动映射为物理列名 + `dataType` + 对齐方式
- **骨架消毒**：剔除骨架里的自定义 JS（`script`）和校验规则，避免把别的表单的业务逻辑带过来

## Excel 模板的字段填写规则

### 选项字段（数据类型=选项）

「2_字段」里数据类型选「选项」，在**「选项值」列**填候选值：

```
普通,紧急,特急          ← 顺序即界面顺序，第一项为默认
```

「关联对象/字典」列留空即可，程序按字段编码自动生成字典 key。
生成时会把选项内嵌进 bo XML：

```xml
<dictionaryTypeList>
  <dictionaryType typeKey="urgency" name="紧急程度" struType="1" sn="1" typeScope="GLOBAL"/>
</dictionaryTypeList>
<dictionaryList>
  <dictionary typeKey="urgency" key="普通" name="普通" sn="1" disabled="false"/>
  <dictionary typeKey="urgency" key="紧急" name="紧急" sn="2" disabled="false"/>
</dictionaryList>
```

导入系统后字段直接就有候选值，**不用再去后台配字典**。

### 内置字段（11 个）

模板里**不出现**，生成 zip 时自动补齐：

```
id / tenantId / ip / deleted / createBy / createTime
updateBy / updateTime / version / baselineVersion / isDraft
```

> 这 11 个是通过统计判定的：出现在 75% 以上业务对象里。
> 系统和 XML 层面**没有内置/自定义的区分标志**（`type_`、`status_`、`type` 属性全是 `base`）。

勾了「流程驱动=是」时额外补 4 个：`processNode / processHandler / processDefId / processInstanceId`。

### 0_项目设置 为什么保留

`moduleId` / `typeId` 会写进 bo XML 的 `moduleId` / `typeId` 属性，
**必须是系统里真实存在的 18 位 ID**。留空的话对象导入后没有归属，在列表里找不到。
其余三项（租户ID / 文件前缀 / 输出目录）都有默认值，不用改。

## 不支持

- 流程定义（`workflowDefinition.xml`）— 流程引擎格式独立，建议界面配置
- 按钮脚本、数据权限矩阵
- 明细表**内部**的子表再嵌一层（`dataTemplate` 本身已支持，见「自动处理的事」）
- 表单设计器的复杂布局（tabs 标签页 / 分栏嵌套）—— 分区（card / fieldset）已支持，
  再深一层（标签页里嵌表格布局）仍需在设计器里手工调

## 踩过的坑（改之前先看这个）

都是实际遇到、排查过的问题，改代码时容易重新踩。

### 打包与生效

| 现象 | 原因 | 怎么办 |
|---|---|---|
| 改了界面，App 里看不到 | `pp6kit/static` 被打进了 `dist/`，改源码不生效 | `tools/sync_static.py` 同步 + **Cmd+Q 完全退出**再开 |
| 只关窗口重开还是旧的 | WKWebView 用了缓存 | 必须完全退出进程 |
| 改了 `.py` 不生效 | 静态脚本不管用 | 重新跑 `build/build_macos.sh` |

### AI 接入

| 现象 | 原因 | 处理 |
|---|---|---|
| 提示「Key 无效」但其实没问题 | 账号**某个模型没开通**（智谱返回 401/1220），旧代码当成 Key 坏掉，中断了整条降级链 | `NoAccess` 异常单独处理，跳过该模型继续试下一个 |
| 每次提问都先卡几秒 | 反复去试一个已知没开通的模型 | `_NO_ACCESS` 进程内缓存，第二次直接跳过 |
| 保存 Key 后点「测试连接」说没填 | 保存成功会清空输入框（「留空则不修改」），测试却读输入框 | 输入框空时回落到已保存配置 |
| 生成对象要等 40~100 秒 | 没传 `max_tokens`，模型一直写到自然结束 | `llm.chat/chat_any` 支持 max_tokens；生成类任务改走快模型链 |
| AI 返回的 JSON 解析失败 | 输出被 max_tokens 截断在半截 | `_repair_truncated()` 砍掉残缺元素、补齐括号 |

各模型实测（同一 Key）：

```
glm-4.7-flash   429 限流（高峰期频繁）
glm-4.6-flash   401 未开通（多数账号）
glm-4.5-flash   OK 1.5s
glm-4-flash     OK 0.9s   ← 生成类任务用这个
glm-4-plus      OK 1.9s
```

### 语义层与 NL2SQL

| 现象 | 原因 | 处理 |
|---|---|---|
| 查出来的状态值全是 UUID | `user_id_` 没推断出关联人员表 | 按列名 + 中文标签补 `iform_party_employee`；`org_id_` 优先按部门处理 |
| `source_id_` 关联错了表 | `pp_i18n_resource` 主键是 `id` 不是 `id_` | `infer_relations` 校验目标表真有 `id_` 列 |
| 语义层出现幽灵列 | 元数据里有、物理表没有 | `build_schema` 校验物理列存在 |
| 「去除掉」生成的 SQL 正好反了 | 否定词穷举漏了「去除掉」 | 改用构词规则 `NEG_VERBS × NEG_COMPLEMENTS` |
| 「每个项目**有**多少个」被当条件 | 模糊匹配把「有」也认了 | 只认「不能有 / 没有 / 不带有」 |
| 查 0 行但确实有数据 | AI 写歪了条件（SQL 不报错） | 「有哪些/列出…」类问法 0 行时让模型自查（带数字条件的不猜） |

### AI 生成对象 / 表单 / 列表

| 现象 | 原因 | 处理 |
|---|---|---|
| 看板按「所属项目」分列，碎成几十列 | 关联对象被当成可分列字段 | 分列只认 `dic`（选项）类型，没有就不出看板 |
| 看板按「汇报类型」而不是「项目状态」分列 | 只按类型打分，没看语义重要性 | 先看名字（state/status > stage > type/level），再看类型 |
| 甘特图所有条都是零长度 | 开始/结束填了同一个日期字段 | 按 `start/begin` 与 `end/due/finish` 语义挑两个不同的列 |
| AI 编造不存在的字段（如 `status`） | 模型按常理想当然 | 逐个核对字段清单，对不上的剔除并提示，不生成幽灵控件 |
| AI 漏排某个字段 | 分组时漏了 | 自动补到最后一组——漏了等于数据录不进去 |
| 表单分组只有第一组有名字 | 组名只标在每组第一个字段上，前端需继承 | 渲染时按 `newline` 断组，`desc` 为组名 |
| **选项字段导入后没有候选值** | `dictionaryTypeList`/`dictionaryList` 写成了空标签 | 把「选项值」列内嵌进 bo XML |
| **内置字段判定不出来** | `type_`/`status_`/XML `type` 全是 `base`，无区分标志 | 靠统计：出现在 75%+ 对象里的才是内置 |

### 控件映射（对照线上系统 68 种控件校正）

| 字段类型 | 真实控件名 | 之前错用 | 后果 |
|---|---|---|---|
| datetime | `datePicker` | `date` | 日期字段生成了错误控件 |
| dic | `dictionary` | `select` | 字典字段用了值来源下拉 |
| clob | `textarea` | `text` | 大文本变成了单行输入 |
| number | `number` | number（缺骨架） | 数字字段退化成 text，丢精度 |

> 这是 2026-10 对照线上系统 `iform_form_field` 表（68 种控件）发现的。
> 之前骨架是从旧导出包 capture 的，恰好没覆盖这几个最常用的控件。

### 其他

- `date` 没有 `isoformat(sep=...)`，只有 `datetime` 有 —— 工时表这类纯日期列会崩
- `AVG(*)` 是语法错误，聚合空表要判 `COUNT`
- `filled = -1` 不能当「空列」处理，只有 `== 0` 才是
- 模块包的 XML 要写**原文**，不能把单条 zip 塞进去（会 zip 套 zip，系统解不出来）

## 桌面包自检

窗口环境下验证「JS 能跑 + 原生桥接已注入 + 生成的文件真的落盘」：

```bash
PP6_SELFTEST=1 PP6_SELFTEST_SAVE=/tmp/e2e.zip \
  "dist/PP6 Studio.app/Contents/MacOS/PP6ImportBuilder"
```

期望输出里全为 True：

```
SELFTEST js_alive=True bridge=True pick_excel=function save_file=function \
         desktop_flag=True demo_loaded=True stat_first=1 saved=True saved_is_zip=True
```

## 自检

```bash
.venv/bin/python tools/selftest.py PP6
```

会跑：骨架重建 → 反解 → 校验 → 出包 → XML 合法性 → ID 唯一性 → 与原包结构比对 → 新对象出包。
全绿说明生成器与当前系统格式仍兼容。

## 目录

```
pp6kit/
  app.py       桌面应用入口（原生窗口 + WebView 兜底）
  webui.py     本地服务与 HTTP 接口
  static/      界面（HTML/CSS/JS，打包时内嵌）
  resources.py 打包后的资源定位
  spec.py      Excel 表结构的唯一真相（生成模板和读取共用）
  reader.py    Excel -> 模型
  model.py     中间模型 + 类型映射
  gen_bo.py    业务对象 XML
  gen_form.py  表单 XML（布局引擎 + 19 种控件骨架）
  gen_tpl.py   列表 / 数据集 XML
  ai_bo.py     一句话 -> 业务对象（AI 只出 JSON，XML 仍走模板）
  modules.py   导入模块定义（系统里每个模块单独导入）
  builder.py   编排 + 打包
  inspect.py   导出包 -> Excel
  validate.py  填错拦截
  template.py  模板生成
assets/
  snippets.json  从线上导出包学到的骨架（勿手改，用 capture 重建）
build/
  pp6.spec             PyInstaller 配置
  build_macos.sh       macOS 一键打包 + 自检
  build_windows.bat    Windows 一键打包
tools/
  capture_snippets.py  骨架提取
  compare.py           结构兼容性比对
  sync_static.py       把前端改动同步进已打包的 App
  selftest.py          端到端自检
```
