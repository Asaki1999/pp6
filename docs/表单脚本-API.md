# 表单脚本（JForm）· 真实 API 清单

**数据来源**：2026-11-14 从 8112 真实库 `powerproject6_release` 的
`iform_form_def.extend_attr_.script` 拉取。381 张表单里 **377 张带 script 字段**，
抽样 85 张有实际脚本的表单做统计。**只读查询，未做任何写入。**

> 为什么用数据库而不是文档：`samples_pdf/java表单脚本方案.pdf`（59 页）是原始依据，
> 但它不在仓库里（`samples_pdf/` 是空目录）。真实库里跑着的脚本才是平台**实际接受**的写法。

## 一、落盘方式（唯一可行写法）

```js
Object.assign(JForm, {
  onLoad: function (form) { ... }
});
```

整段 JS 序列化后存进表单的 `formDef.extendAttr.script`。**85 个样本里没有一例**用
`JForm.onLoad = ...` 或其它挂载方式——平台只执行 `Object.assign(JForm, {...})`。

工具侧：`7_表单脚本` 表（列为「表单key / 脚本说明 / JForm 脚本」）原样透传到
`extendAttr.script`，见 `gen_form.py` 的 `ext["script"] = script`。

## 二、生命周期钩子（真实使用频次）

| 钩子 | 次数 | 用途 |
|---|---:|---|
| `onLoad(form)` | 80 | 加载后初始化：设默认值、拉数据、绑事件 |
| `beforeSubmit(form)` | 45 | 提交前校验、补数据 |
| `beforeLoad(form)` | 33 | 加载前干预 |
| `saveBefore(form)` | 20 | 保存前处理 |
| `afterSubmit(form)` | 16 | 提交后跳转/关窗/刷新 |
| `onLoadActions(form, action, button, type)` | 3 | 按钮显隐（`hidden`）/禁用（`disabled`） |
| `onValidate` | 2 | 自定义校验 |

函数签名统一是 `function (form) {...}`，个别用 `async function (form)`。

## 三、`form.*` 能做什么（按真实调用频次）

| 方法 | 次数 | 说明 |
|---|---:|---|
| `setData(obj)` / `getData()` | 392 / 177 | 批量读写表单数据 |
| `getParent(key)` | 70 | 拿父表单实例（明细/子表单联动） |
| `getRefsField(code)` / `getRefs(code)` | 52 / 51 | 取控件实例 / 取引用控件 |
| `closeDialog()` | 30 | 关闭弹窗 |
| `setHidden(code, bool)` | 28 | 隐藏控件 |
| `setFormRights(code, 权限)` | 24 | **h 隐藏 / e 编辑 / r 只读 / b 必填 / d 禁用** |
| `setReadOnly` / `setDisabled` / `setRequired` / `setEdit` | 14 / 13 / 10 / 4 | 单控件权限 |
| `validate()` / `validateField(code)` | 7 / 9 | 触发校验 |
| `formSubmitVerify` / `formErrorToast` | 7 / 7 | 提交校验 / 错误提示 |
| `elTree` / `saveUnBindBoField` | 4 / 3 | 树控件 / 未绑字段保存 |

## 四、`this` 上的顶层能力（脚本作用域内）

| 能力 | 次数 | 说明 |
|---|---:|---|
| `this.$request(config)` | 113 | HTTP 请求（`{url, method, params, data}`） |
| `makeRequest` | 27 | 请求封装 |
| `interval` | 21 | 定时器 |
| `getListData` / `refreshList` | 6 / 4 | 列表数据 |
| `submit` / `updateFormAndClose` | 4 / 2 | 提交 / 保存并关闭 |
| `addRelation` / `fieldChange` | 3 / 3 | 关联数据 / 触发字段联动 |
| `formatDateStandard` / `getRootDirId` / `currentClassification` / `requestMessage` | 2 / 2 / 2 / 2 | 工具方法 |

## 五、一段真实脚本（最短的一支，237 字）

```js
Object.assign(JForm,{
  //加载事件
  onLoad:function(form){
    form.$on("close", (data) => {
      form.getParent('test_case_template_details')
        .getRefsField("test_case_template_step_ungroup_details")
        .initCascadeDynamicData()
    });
  }
});
```

可见还有一层：`form.$on(事件名, cb)` 事件订阅、`getRefsField(...).initCascadeDynamicData()`
这类控件级方法。

## 六、工具现在做到哪一步

| 能力 | 状态 |
|---|---|
| 脚本落盘（透传到 `extendAttr.script`） | ✅ 已支持 |
| 条件隐藏（`8_控件隐藏` → `formRules` 声明式） | ✅ 已支持，不走脚本 |
| **脚本静态体检**（2026-11-14 新增） | ✅ 三类静默故障拦截 |

新增的三类检查（`validate._validate_scripts`）：

1. **没挂 JForm** → `error`：整段脚本不会执行，导入后表单行为完全不对
2. **钩子名拼错** → `warn`：如 `onload` / `onLoads`，平台既不报错也不执行
3. **花括号不配平** → `error`：脚本语法错误会让整张表单打不开

回归 `VAL23` 钉住。这三类都属于「导入不报错、运行也不报错，只是不生效」，
比语法错误更难排查，所以在出包前拦。

## 七、还没掌握的部分（写脚本前要确认）

1. **`$request` 的接口约定**——`this.$request` 走的是平台哪个网关、权限怎么带，样本里只有调用没有说明；
2. **`form.$on` 的事件名全集**——只见到 `"close"`；
3. **控件级方法**（`initCascadeDynamicData` 这类）的完整清单——只在片段里出现过几个；
4. **`onLoadActions` 的参数语义**（`action` / `button` / `type` 的取值）。

这四项要靠原版 `java表单脚本方案.pdf`（59 页）或平台前端源码才能补齐。
拿到 PDF 的话建议放进 `samples_pdf/`，我读完后把上面三节扩成完整 API 表。
