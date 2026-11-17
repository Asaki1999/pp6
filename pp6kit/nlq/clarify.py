"""问句澄清：只在**真的缺关键条件**时追问一句，不打断正常查询。

为什么只做时间
--------------
「新增/趋势/同比」这类问题没有时间范围，SQL 必然要么全量、要么模型自己
猜一个月份——两种都容易被当成错答案。与其猜，不如让用户点一下。
对象、状态、人名这些条件缺失时，通常仍能给出有意义的清单，不必追问。
"""
from __future__ import annotations

import re

_EXPLICIT_RANGE = re.compile(
    r"今天|昨天|本周|上周|本月|这个月|上月|上个月|今年|去年|明年|"
    r"本季度|上季度|最近\s*\d+\s*(?:天|周|月|年)|近\s*\d+\s*(?:天|周|月|年)|"
    r"\d{4}\s*年|\d{1,2}\s*月|全部|所有")
_VAGUE_TIME = re.compile(r"最近|近期|近来|这段时间|这段时间|目前|当前")
_METRIC = re.compile(r"新增|变化|趋势|同比|环比|增长|下降|金额|数量|多少|统计")


def check(question: str) -> dict | None:
    q = (question or "").strip()
    if not q:
        return None
    if _EXPLICIT_RANGE.search(q):
        return None
    # 「最近的项目」是“最新”，不是时间范围；只有和指标一起出现才追问
    vague = bool(_VAGUE_TIME.search(q)) and bool(_METRIC.search(q))
    # 「新增了多少合同」没有时间范围，也必须问
    added = "新增" in q
    if not (vague or added):
        return None
    return {
        "question": "这个问题要看哪个时间范围？",
        "options": ["本月", "上个月", "今年", "全部"],
        "why": "「新增/趋势」类问题没有时间范围，直接查会把历史数据全算进去。",
        "field": "time",
    }
