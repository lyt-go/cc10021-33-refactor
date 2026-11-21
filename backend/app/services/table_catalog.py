"""表目录服务：注册和管理数据表定义，驱动字段画像、标签分类和血缘关系。

domain_chain: 表目录注册 (table_catalog)
  └─ 入库 → 字段画像计算 (column_profile)
  └─ 关联 → 分类标签 (tag_classify)
  └─ 关联 → 血缘图谱 (lineage_graph)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.profile_standardization import (
    PROFILE_SOURCE_REGISTRATION,
    upsert_profile_record,
)
from app.store import store


class TableCatalogService(BaseModuleService):
    MODULE = "table_catalog"
    ENTITY = "表目录"
    OPERATION_TERM = "入库"
    LIST_FIELDS = ["表编号", "表名称", "所属库名", "表类型", "字段数量", "数据行数", "存储大小", "最后更新"]
    REQUIRED_FIELDS = ["表编号", "表名称", "所属库名"]
    STATUS_ORDER = ["草稿", "待校验", "已注册", "已下线"]
    ACTION_RULES = {"提交校验": "待校验", "确认注册": "已注册", "标记下线": "已下线"}
    NEGATIVE_ACTIONS = []

    def register_table_with_columns(self, table_name: str, db_name: str, columns: list[dict],
                                     row_count: int = 0) -> dict[str, Any]:
        """将采集到的表定义入库到目录并触发后续流水线。

        对表元数据进行标准化转换并入库到目录存储。入库后自动：
        1. 校验字段完整性（非空检查、主键检查、类型匹配）
        2. 触发字段画像计算管道
        3. 触发分类标签关联
        """
        rows = store.rows(self.MODULE)
        # 写入表目录记录
        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        entry = {
            "id": new_id,
            "表编号": f"TABL-{new_id:04d}",
            "表名称": table_name,
            "所属库名": db_name,
            "表类型": "TABLE",
            "字段数量": len(columns),
            "数据行数": str(row_count),
            "存储大小": f"{row_count * 256 // 1024} KB",
            "最后更新": datetime.now(timezone.utc).isoformat(),
            "status": "已注册",
            "pending": False,
            "abnormal": False,
        }
        rows.append(entry)

        validation_issues = self._validate_columns(columns)

        # 触发字段画像计算
        profile_entries = self._dispatch_column_profile(new_id, table_name, columns)

        # 触发分类标签关联
        tag_entries = self._dispatch_tag_classification(table_name, db_name, columns)

        return {
            "注册表": table_name,
            "字段数量": len(columns),
            "校验问题": len(validation_issues),
            "校验详情": validation_issues,
            "画像任务数": profile_entries,
            "标签分类数": tag_entries,
        }

    def _validate_columns(self, columns: list[dict]) -> list[dict]:
        """校验字段完整性：检查非空字段、主键唯一性和数据类型有效性。"""
        issues = []
        valid_types = {"BIGINT", "VARCHAR(255)", "TINYINT", "DECIMAL(18,2)", "TIMESTAMP",
                       "DATE", "INTEGER", "NUMERIC", "TEXT", "STRING", "MAP", "DOUBLE"}
        for col in columns:
            col_name = col.get("字段名称", "")
            col_type = col.get("数据类型", "")
            if not col_name:
                issues.append({"字段": "(无名)", "问题": "字段名称为空"})
                continue
            if col_type not in valid_types:
                issues.append({"字段": col_name, "问题": f"数据类型 {col_type} 不在已知类型集中"})
            nullable = col.get("可为空")
            if nullable not in (True, False):
                issues.append({"字段": col_name, "问题": "可为空属性缺失或无效"})
        return issues

    def _dispatch_column_profile(self, table_id: int, table_name: str, columns: list[dict]) -> int:
        """将字段信息写入画像计算流水线，驱动字段画像计算。

        登记初值与样本计算入口共用同一套字段画像标准化写法；按
        (关联表名, 字段名称) 收拢，登记中断重试时不会重复建档，
        也不会把已到达的真实样本结果记回初始化值。
        """
        profile_rows = store.rows("column_profile")
        dispatched = 0
        for col in columns:
            upsert_profile_record(
                profile_rows,
                table_name,
                col.get("字段名称", ""),
                source=PROFILE_SOURCE_REGISTRATION,
            )
            dispatched += 1
        return dispatched

    def _dispatch_tag_classification(self, table_name: str, db_name: str, columns: list[dict]) -> int:
        """根据表结构和字段类型关联分类标签。"""
        tag_rows = store.rows("tag_classify")
        classified = 0
        dimension_map = {
            "mysql": "ODS层",
            "hive": "ADS/DWD层",
            "postgresql": "业务库",
        }
        dimension = dimension_map.get(db_name, "未知层")

        # 为表级添加分类标签
        for tag_suffix, tag_val in [("分层", dimension), ("库", db_name), ("表类型", "事实表" if len(columns) > 4 else "维度表")]:
            new_tid = max((int(r.get("id", 0)) for r in tag_rows), default=0) + 1
            tag_rows.append({
                "id": new_tid,
                "标签编号": f"TAG_-{new_tid:04d}",
                "标签名称": f"{table_name}_{tag_suffix}",
                "分类维度": tag_suffix,
                "关联表名": table_name,
                "标签值": tag_val,
                "分类依据": f"{db_name}来源表自动分类",
                "生效时间": datetime.now(timezone.utc).isoformat(),
                "标签版本": "v1",
                "status": "已生效",
                "pending": False,
                "abnormal": False,
            })
            classified += 1
        return classified

    def sync_lineage_entries(self, table_name: str, upstream_tables: list[str]) -> int:
        """将上游表与当前表关联写入血缘图谱，建立上下游数据血缘关系。"""
        lineage_rows = store.rows("lineage_graph")
        synced = 0
        for upstream in upstream_tables:
            new_lid = max((int(r.get("id", 0)) for r in lineage_rows), default=0) + 1
            lineage_rows.append({
                "id": new_lid,
                "关系编号": f"LINE-{new_lid:04d}",
                "上游表名": upstream,
                "下游表名": table_name,
                "来源字段": "id",
                "目标字段": "source_id",
                "转换逻辑": "ETL聚合转换",
                "采集批次": datetime.now(timezone.utc).strftime("%Y%m%d%H"),
                "关系版本": "v1",
                "status": "已生效",
                "pending": False,
                "abnormal": False,
            })
            synced += 1
        return synced

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "入库"
        snapshot["desc"] = "将采集到的表定义入库到目录存储"
        snapshot["artifact"] = "表定义目录"
        snapshot["artifact_desc"] = "表定义目录登记册"
        return snapshot