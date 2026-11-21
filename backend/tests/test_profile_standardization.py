"""字段画像标准化重构测试：两个入口统一写法、双输入收拢、基线回溯。

覆盖场景：
- 目录登记初值与样本计算指标共用同一套记录写法，两个入口输入输出不变；
- 同一字段两份画像输入几乎同时送到时按 (关联表名, 字段名称) 收拢；
- 登记初值（待计算）与真实零值（0.00%）可区分；
- 登记中断重试不会把既有真实样本结果记回初始化值；
- 画像统计结果同步基线校对清单，可回溯到实际采用的画像记录；
- 暂停目录登记最后一列、提交对应样本、登记恢复后的两类画像含义。
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.column_profile import ColumnProfileService
from app.services.profile_standardization import (
    BASIC_METRIC_FIELDS,
    PENDING_METRIC,
    PROFILE_SOURCE_REGISTRATION,
    PROFILE_SOURCE_SAMPLE,
    find_profile_record,
    is_pending_metric,
    profile_source_of,
    record_has_computed_metrics,
    registration_initial_metrics,
    standardize_profile_metrics,
    upsert_profile_record,
)
from app.services.table_catalog import TableCatalogService
from app.store import store


def _columns(*names: str) -> list[dict]:
    type_map = {
        "id": ("BIGINT", False, True),
        "name": ("VARCHAR(255)", True, False),
        "amount": ("DECIMAL(18,2)", True, False),
        "created_at": ("TIMESTAMP", False, False),
    }
    return [
        {"字段名称": n, "数据类型": type_map[n][0], "可为空": type_map[n][1], "是否主键": type_map[n][2]}
        for n in names
    ]


def _profiles_of(table: str) -> dict[str, dict]:
    return {
        r["字段名称"]: r
        for r in store.rows("column_profile")
        if r.get("关联表名") == table
    }


def _latest_baseline(table: str) -> dict:
    rows = [r for r in store.rows("dq_baseline") if r.get("目标表名") == table]
    assert rows, f"{table} 应已同步基线校对清单"
    return rows[-1]


class TestUnifiedStandardization:
    """两个入口共用同一套标准化写法，且输入输出保持不变。"""

    def test_registration_entry_io_and_record_shape(self):
        svc = TableCatalogService()
        result = svc.register_table_with_columns(
            "std_reg_table", "mysql", _columns("id", "name"), 1024)
        # 入口输出不变
        assert result["注册表"] == "std_reg_table"
        assert result["字段数量"] == 2
        assert result["画像任务数"] == 2
        assert "校验问题" in result and "标签分类数" in result
        # 登记初值：统一写法，全部基础指标为「待计算」占位
        for record in _profiles_of("std_reg_table").values():
            assert record["画像来源"] == PROFILE_SOURCE_REGISTRATION
            assert record["画像版本"] == "v1.0"
            assert record["status"] == "计算中"
            assert record["pending"] is True
            for field in BASIC_METRIC_FIELDS:
                assert record[field] == PENDING_METRIC

    def test_sample_entry_io_and_record_shape(self):
        svc = ColumnProfileService()
        profiles = svc.compute_column_statistics(
            "std_sample_table", _columns("id", "amount"))
        # 入口输出不变
        assert len(profiles) == 2
        for p in profiles:
            for key in ("字段名称", "数据类型", "空值比例", "填充率", "唯一值数",
                        "基数比例", "样本行数", "计算时间", "最小值", "最大值", "均值",
                        "质量评分"):
                assert key in p
        # 样本指标落档为字段画像记录
        records = _profiles_of("std_sample_table")
        assert set(records) == {"id", "amount"}
        for record in records.values():
            assert record["画像来源"] == PROFILE_SOURCE_SAMPLE
            assert record["status"] == "已完成"
            assert record["pending"] is False
            assert record_has_computed_metrics(record)

    def test_standardize_metrics_keeps_real_zero(self):
        standardized = standardize_profile_metrics({"空值比例": "0.00%", "唯一值数": 0})
        assert standardized["空值比例"] == "0.00%"
        assert standardized["唯一值数"] == 0
        assert standardized["最小值"] == PENDING_METRIC
        assert not is_pending_metric("0.00%")
        assert not is_pending_metric(0)
        assert is_pending_metric(PENDING_METRIC)
        assert registration_initial_metrics() == {f: PENDING_METRIC for f in BASIC_METRIC_FIELDS}


class TestProfileConvergence:
    """同一字段的两份画像输入（登记缺省值/真实样本）按字段收拢。"""

    def test_converges_registration_then_sample(self):
        table = "converge_reg_first"
        TableCatalogService().register_table_with_columns(
            table, "mysql", _columns("id", "name"), 100)
        ColumnProfileService().compute_column_statistics(table, _columns("id", "name")[:1])
        records = _profiles_of(table)
        assert len(records) == 2  # 每字段恰好一条，不重复建档
        assert records["id"]["画像来源"] == PROFILE_SOURCE_SAMPLE
        assert records["id"]["status"] == "已完成"
        assert records["name"]["画像来源"] == PROFILE_SOURCE_REGISTRATION
        assert records["name"]["空值比例"] == PENDING_METRIC

    def test_converges_sample_then_registration(self):
        table = "converge_sample_first"
        ColumnProfileService().compute_column_statistics(table, _columns("id"))
        TableCatalogService().register_table_with_columns(
            table, "mysql", _columns("id", "name"), 100)
        records = _profiles_of(table)
        assert len(records) == 2
        # 样本先到的记录不被登记初值覆盖
        assert records["id"]["画像来源"] == PROFILE_SOURCE_SAMPLE
        assert records["id"]["空值比例"] != PENDING_METRIC
        assert records["name"]["画像来源"] == PROFILE_SOURCE_REGISTRATION

    def test_registration_retry_keeps_sample_results(self):
        """登记中断后重试：既有真实样本结果不能被记成初始化值。"""
        table = "converge_retry"
        catalog = TableCatalogService()
        catalog.register_table_with_columns(table, "mysql", _columns("id", "name"), 100)
        ColumnProfileService().compute_column_statistics(table, _columns("id", "name"))
        before = {k: dict(v) for k, v in _profiles_of(table).items()}
        # 中断后重试：全量列再次登记
        catalog.register_table_with_columns(table, "mysql", _columns("id", "name"), 100)
        after = _profiles_of(table)
        assert len(after) == 2
        for name, record in after.items():
            assert record["id"] == before[name]["id"]  # 画像编号不变
            assert record["画像来源"] == PROFILE_SOURCE_SAMPLE
            for field in BASIC_METRIC_FIELDS:
                assert record[field] == before[name][field]

    def test_real_zero_survives_registration_retry(self):
        """真实零值与尚未计算区分：零值样本记录重试后仍是零值。"""
        rows = store.rows("column_profile")
        record, created = upsert_profile_record(
            rows, "converge_zero", "zero_col",
            metrics={"空值比例": "0.00%", "唯一值数": 0, "最小值": "0.00", "最大值": "0.00"},
            source=PROFILE_SOURCE_SAMPLE,
        )
        assert created and record_has_computed_metrics(record)
        # 登记重试（缺省值后到）
        again, created = upsert_profile_record(
            rows, "converge_zero", "zero_col", source=PROFILE_SOURCE_REGISTRATION)
        assert not created
        assert again["空值比例"] == "0.00%" and not is_pending_metric(again["空值比例"])
        assert again["唯一值数"] == 0
        assert profile_source_of(again) == PROFILE_SOURCE_SAMPLE


class TestBaselineTraceability:
    """画像统计结果同步基线校对清单，核验可回溯到实际采用的画像记录。"""

    def test_baseline_syncs_with_adopted_refs(self):
        table = "baseline_trace"
        svc = ColumnProfileService()
        profiles = svc.compute_column_statistics(table, _columns("id", "name", "amount"))
        agg = svc.aggregate_table_profile(table, profiles)
        # 原口径不变
        assert agg["画像字段数"] == 3
        assert agg["基线已建立"] is True
        assert "平均空值率" in agg and "平均质量评分" in agg
        baseline = _latest_baseline(table)
        assert baseline["基线指标"] == "字段空值率"
        assert baseline["基线上界"].endswith("%") and baseline["基线下界"].endswith("%")
        # 回溯：来源画像指向收拢后的实际画像记录
        assert len(baseline["来源画像"]) == 3
        assert baseline["画像来源"] == PROFILE_SOURCE_SAMPLE
        records = _profiles_of(table)
        by_code = {r["画像编号"]: r for r in records.values()}
        assert set(baseline["来源画像"]) == set(by_code)
        for ref in baseline["来源画像"]:
            assert by_code[ref]["关联表名"] == table
            assert profile_source_of(by_code[ref]) == PROFILE_SOURCE_SAMPLE

    def test_baseline_reads_converged_record_after_sample_arrives(self):
        """建基线恰逢双输入之间：收拢后读到的是样本计算记录。"""
        table = "baseline_race"
        TableCatalogService().register_table_with_columns(table, "mysql", _columns("id"), 100)
        svc = ColumnProfileService()
        profiles = svc.compute_column_statistics(table, _columns("id"))
        svc.aggregate_table_profile(table, profiles)
        baseline = _latest_baseline(table)
        assert baseline["画像来源"] == PROFILE_SOURCE_SAMPLE
        record = find_profile_record(store.rows("column_profile"), table, "id")
        assert baseline["来源画像"] == [record["画像编号"]]
        assert record["画像来源"] == PROFILE_SOURCE_SAMPLE


class TestPauseLastColumnScenario:
    """暂停目录登记最后一列并提交对应样本，登记恢复后检查两类画像含义。"""

    def test_pause_last_column_then_resume(self):
        table = "pause_last_col"
        catalog = TableCatalogService()
        profiler = ColumnProfileService()
        cols = _columns("id", "name", "amount", "created_at")

        # 1. 暂停目录登记的最后一列（登记中断在最后一列之前）
        catalog.register_table_with_columns(table, "mysql", cols[:-1], 2048)
        paused = _profiles_of(table)
        assert set(paused) == {"id", "name", "amount"}

        # 2. 提交最后一列对应的真实样本
        sampled = profiler.compute_column_statistics(table, [cols[-1]])
        assert sampled[0]["字段名称"] == "created_at"

        # 3. 建基线任务在此间取数：收拢后读到的是样本计算记录
        profiler.aggregate_table_profile(table, sampled)
        baseline = _latest_baseline(table)
        assert baseline["画像来源"] == PROFILE_SOURCE_SAMPLE
        sample_record = find_profile_record(store.rows("column_profile"), table, "created_at")
        assert baseline["来源画像"] == [sample_record["画像编号"]]
        assert sample_record["画像来源"] == PROFILE_SOURCE_SAMPLE

        # 4. 登记恢复（中断重试，全量列重新登记）
        catalog.register_table_with_columns(table, "mysql", cols, 2048)
        records = _profiles_of(table)

        # 5. 收拢检查：每字段恰好一条画像记录
        assert set(records) == {"id", "name", "amount", "created_at"}

        # 6. 两类画像含义：样本列保持真实指标，登记列保持初值语义
        resumed = records["created_at"]
        assert resumed["id"] == sample_record["id"]  # 画像编号未被重试改写
        assert resumed["画像来源"] == PROFILE_SOURCE_SAMPLE
        assert resumed["status"] == "已完成"
        for field in BASIC_METRIC_FIELDS:
            assert not is_pending_metric(resumed[field])
        for name in ("id", "name", "amount"):
            assert records[name]["画像来源"] == PROFILE_SOURCE_REGISTRATION
            assert records[name]["status"] == "计算中"
            for field in BASIC_METRIC_FIELDS:
                assert records[name][field] == PENDING_METRIC
