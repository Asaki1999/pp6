"""项目字段快照夹具：给「需要项目字段池」的用例注入一份固定定义。

2026-11-27 移除了 `assets/project_fields.json`（随包快照）之后，空 HOME / CI
上没有库就只剩 `unavailable`，WEB24 / VR16 / WEB39 这类用例会失去对齐目标。
它们要验证的是「拿到字段后怎么对齐」，所以显式给一份最小字段集，不依赖
本机 `~/.pp6kit`，也不去联网。

字段口径对齐线上 `pp_project`：`code` 是逻辑编码，`field_name` 是物理列。
字段集照线上 `pp_project` 冻结版本原样取 51 个，够 `MIN_SNAPSHOT_FIELDS`（30）让 status=ok。
"""
from __future__ import annotations

import contextlib

from pp6kit import projlink

# (code, name, type_new, field_name, object_type)
_FIELDS: list[tuple[str, str, str, str, str]] = [
    ('name', '项目名称', 'varchar', 'name_', ''),
    ('shortName', '项目简称', 'varchar', 'short_name_', ''),
    ('classificationId', '项目类型', 'object', 'classification_id_', 'classification'),
    ('ppParentId', '父项目', 'object', 'pp_parent_id_', 'project'),
    ('systemCalendarId', '系统日历', 'varchar', 'system_calendar_id_', ''),
    ('stateId', '项目状态', 'object', 'state_id_', 'project_state'),
    ('gradeId', '项目级别', 'object', 'grade_id_', 'project_grade'),
    ('templateId', '应用的项目模版', 'object', 'template_id_', 'template'),
    ('pinyin', '拼音', 'varchar', 'pinyin_', ''),
    ('code', '项目编号', 'varchar', 'code_', ''),
    ('phase', '项目阶段名称', 'varchar', 'phase_', ''),
    ('phaseIds', '项目阶段', 'object', 'phase_ids_', 'project_phase'),
    ('duration', '项目工期(单位:分钟)', 'number', 'duration_', ''),
    ('standardHours', '项目标准工时', 'number', 'standard_hours_', ''),
    ('start', '计划开始时间', 'datetime', 'start_', ''),
    ('finish', '计划完成时间', 'datetime', 'finish_', ''),
    ('actualStart', '实际开始时间', 'datetime', 'actual_start_', ''),
    ('actualFinish', '实际结束时间', 'datetime', 'actual_finish_', ''),
    ('baselineDuration', '基线工期', 'number', 'baseline_duration_', ''),
    ('baselineStart', '基线开始时间', 'datetime', 'baseline_start_', ''),
    ('baselineFinish', '基线结束时间', 'datetime', 'baseline_finish_', ''),
    ('progress', '实际进度', 'number', 'progress_', ''),
    ('plannedProgress', '计划进度', 'number', 'planned_progress_', ''),
    ('baselineProgress', '基线进度', 'number', 'baseline_progress_', ''),
    ('tag', '标签', 'varchar', 'tag_', ''),
    ('light', '亮灯', 'number', 'light_', ''),
    ('lightDescription', '亮灯描述', 'varchar', 'light_description_', ''),
    ('orgId', '隶属部门', 'department', 'org_id_', ''),
    ('managerIds', '项目经理', 'user', 'manager_ids_', ''),
    ('notes', '备注', 'clob', 'notes_', ''),
    ('totalBudget', '总预算', 'number', 'total_budget_', ''),
    ('totalCost', '总成本', 'number', 'total_cost_', ''),
    ('budgetState', '预算状态', 'number', 'budget_state_', ''),
    ('zhuanYe', '专业', 'varchar', 'zhuan_ye_', ''),
    ('durationDays', '项目工期(单位:天)', 'number', 'duration_days_', ''),
    ('baselineDurationDays', '基线天工期', 'number', 'baseline_duration_days_', ''),
    ('huiBaoLeiXing', '汇报类型', 'dic', 'hui_bao_lei_xing_', 'report_type'),
    ('id', '主键', 'varchar', 'id_', ''),
    ('tenantId', '租户ID', 'varchar', 'tenant_id_', ''),
    ('ip', 'IP地址', 'varchar', 'ip_', ''),
    ('deleted', '数据删除状态', 'varchar', 'deleted_', ''),
    ('createBy', '项目创建人', 'user', 'create_by_', ''),
    ('createTime', '创建时间', 'datetime', 'create_time_', ''),
    ('updateTime', '更新时间', 'datetime', 'update_time_', ''),
    ('version', '版本', 'number', 'version_', ''),
    ('updateBy', '更新人', 'user', 'update_by_', ''),
    ('baselineVersion', '基线版本', 'number', 'baseline_version_', ''),
    ('currencyId', '币种ID', 'varchar', 'currency_id_', ''),
    ('baseTotalBudget', '本位币总预算', 'number', 'base_total_budget_', ''),
    ('baseTotalCost', '本位币总成本', 'number', 'base_total_cost_', ''),
    ('isDraft', '是否草稿', 'varchar', 'is_draft_', ''),
]

_PROJECT = {"code": "project", "name": "项目", "tbl": "pp_project",
            "tableLabel": "项目", "attributeCount": len(_FIELDS)}


def snapshot() -> projlink.Snapshot:
    """一份「连库拿到」的项目字段快照（字段数 ≥ MIN_SNAPSHOT_FIELDS）。"""
    fields = [
        projlink.ProjectField(
            code=code, name=name, type_new=type_new, field_name=field_name,
            object_type=object_type, is_null=True, sn=i + 1,
        )
        for i, (code, name, type_new, field_name, object_type) in enumerate(_FIELDS)
    ]
    return projlink.Snapshot(
        source="target_db", generated_at="2026-11-27T00:00:00+08:00",
        project=dict(_PROJECT), fields=fields,
        system_codes={"id", "createTime", "updateTime"},
        declared_total=len(fields), min_fields=projlink.MIN_SNAPSHOT_FIELDS,
    )


@contextlib.contextmanager
def project_fields(snap: projlink.Snapshot | None = None):
    """把 load_snapshot 固定成给定快照，退出时还原。

    覆盖 `projlink.load_snapshot` 本身：builder / links / webui 都从这里取，
    patch 一个点就够，不用逐处 mock。
    """
    fixed = snap or snapshot()
    old = projlink.load_snapshot

    def fake(*args, **kwargs):
        return fixed

    # 编码白名单（project_field_codes）也是模块级缓存：不清的话，前一条
    # 用例在无库环境下算出的空集合会被这条用例继承（PL21 单跑绿、全量红）。
    projlink.clear_cache()
    projlink.load_snapshot = fake
    try:
        yield fixed
    finally:
        projlink.load_snapshot = old
        projlink.clear_cache()
