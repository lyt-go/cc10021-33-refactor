"""字段画像记录标准化：目录登记初值与样本计算指标共用同一套记录写法。

背景：表目录派发画像时填登记初值，字段画像用样本计算统计，两处曾各自
拼写基础字段（空值比例、唯一值数、最小值、最大值）。同一字段的两份画像
输入可能几乎同时送到（一份来自目录登记缺省值，一份来自真实样本），这里
统一记录写法，并按 (关联表名, 字段名称) 收拢：

- 登记初值：基础指标一律记为「待计算」占位，表示尚未计算，与样本算出的
  真实零值（如 "0.00%"、0）严格区分；
- 样本指标：计算结果原样保留（含真实零值），到达后就地替换同字段的登记
  初值，画像编号保持不变，便于基线核验回溯；
- 登记中断重试：已收拢为样本结果的记录不得被登记初值覆盖，也不会重复
  新建记录。
"""
from __future__ import annotations

import threading
from typing import Any

# 尚未计算的统一占位符；真实计算出的零值（如 "0.00%"、0）不得使用该占位
PENDING_METRIC = "待计算"

# 画像来源：登记初值（目录登记）/ 真实样本（样本计算）
PROFILE_SOURCE_REGISTRATION = "目录登记"
PROFILE_SOURCE_SAMPLE = "样本计算"

# 两个入口共用的基础指标字段
BASIC_METRIC_FIELDS = ("空值比例", "唯一值数", "最小值", "最大值")

_PROFILE_VERSION = "v1.0"

# 同一字段的两份画像输入可能几乎同时送达，收拢写入需要串行化
_UPSERT_LOCK = threading.Lock()


def registration_initial_metrics() -> dict[str, str]:
    """登记初值：全部基础指标记为「待计算」，表示尚未计算。"""
    return {field: PENDING_METRIC for field in BASIC_METRIC_FIELDS}


def standardize_profile_metrics(metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    """把任一入口的指标归一到基础字段上：缺省记「待计算」，实值原样保留。"""
    standardized = registration_initial_metrics()
    for field in BASIC_METRIC_FIELDS:
        value = (metrics or {}).get(field)
        if value is not None:
            standardized[field] = value
    return standardized


def is_pending_metric(value: Any) -> bool:
    """判断指标是否仍是「尚未计算」占位；真实零值（0、"0.00%"）不算。"""
    return value is None or str(value).strip() == PENDING_METRIC


def record_has_computed_metrics(record: dict[str, Any]) -> bool:
    """画像记录是否已带有样本计算出的指标。"""
    return any(not is_pending_metric(record.get(field)) for field in BASIC_METRIC_FIELDS)


def profile_source_of(record: dict[str, Any]) -> str:
    """读取记录的画像来源；没有来源字段的旧数据按指标是否已计算推断。"""
    source = record.get("画像来源")
    if source:
        return str(source)
    return PROFILE_SOURCE_SAMPLE if record_has_computed_metrics(record) else PROFILE_SOURCE_REGISTRATION


def build_profile_record(
    record_id: int,
    table_name: str,
    column_name: str,
    metrics: dict[str, Any] | None = None,
    source: str = PROFILE_SOURCE_REGISTRATION,
) -> dict[str, Any]:
    """按统一写法生成一条字段画像记录。"""
    computed = source == PROFILE_SOURCE_SAMPLE
    return {
        "id": record_id,
        "画像编号": f"COLU-{record_id:04d}",
        "关联表名": table_name,
        "字段名称": column_name,
        **standardize_profile_metrics(metrics),
        "画像版本": _PROFILE_VERSION,
        "画像来源": source,
        "status": "已完成" if computed else "计算中",
        "pending": not computed,
        "abnormal": False,
    }


def find_profile_record(
    rows: list[dict[str, Any]], table_name: str, column_name: str
) -> dict[str, Any] | None:
    """按 (关联表名, 字段名称) 定位收拢后的画像记录。"""
    for row in rows:
        if row.get("关联表名") == table_name and row.get("字段名称") == column_name:
            return row
    return None


def upsert_profile_record(
    rows: list[dict[str, Any]],
    table_name: str,
    column_name: str,
    metrics: dict[str, Any] | None = None,
    source: str = PROFILE_SOURCE_REGISTRATION,
) -> tuple[dict[str, Any], bool]:
    """按 (关联表名, 字段名称) 收拢画像记录，返回 (记录, 是否新建)。

    - 无记录：新建（登记初值或样本指标）；
    - 样本指标到达：就地更新已计算的基础指标与来源，保留画像编号，
      便于基线核验回溯到同一条记录；
    - 登记初值到达：既有记录（尤其已含样本结果的）保持不动，保证
      登记中断重试不会把真实样本结果记回初始化值。
    """
    with _UPSERT_LOCK:
        existing = find_profile_record(rows, table_name, column_name)
        if existing is None:
            new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            record = build_profile_record(new_id, table_name, column_name, metrics, source)
            rows.append(record)
            return record, True
        if source == PROFILE_SOURCE_SAMPLE:
            # 只覆盖本次样本实际算出的指标，未计算的字段保持原值
            for field in BASIC_METRIC_FIELDS:
                value = (metrics or {}).get(field)
                if value is not None:
                    existing[field] = value
            existing["画像来源"] = PROFILE_SOURCE_SAMPLE
            existing["status"] = "已完成"
            existing["pending"] = False
        return existing, False
