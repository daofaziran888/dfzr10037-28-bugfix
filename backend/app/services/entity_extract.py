"""实体抽取领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 历史派生记录里被错误字符串化的空值占位文本；回查时按“未知”处理，不能当成有效业务值。
LEGACY_NONE_TEXT = "None"


def _evidence_text(value: Any) -> str | None:
    """抽取证据字段转换：空值保持 None（未知），显式数值 0 保留为 "0"，其余转字符串。

    空值不等于字符串 "None"，也不等于任何默认业务值；只有登记过的真实内容才进入派生记录。
    """
    if value is None:
        return None
    text = str(value)
    if not text.strip():
        return None
    return text


def _is_valid_evidence(value: Any) -> bool:
    """证据字段是否为有效业务值：None、空白和历史残留的 "None" 文本都不算。"""
    if value is None:
        return False
    text = str(value).strip()
    return bool(text) and text != LEGACY_NONE_TEXT


class EntityExtractService(BaseModuleService):
    MODULE = "entity_extract"
    ENTITY = "实体定义"
    OPERATION_TERM = "入库"
    LIST_FIELDS = ["实体编号", "实体类型", "数据来源", "抽取规则", "同义词典", "消岐策略", "置信阈值", "最后抽取"]
    REQUIRED_FIELDS = ["实体编号", "实体类型", "数据来源"]
    STATUS_ORDER = ["草稿", "抽取中", "已入库", "低置信"]
    ACTION_RULES = {"启动抽取": "抽取中", "确认入库": "已入库", "标记低置信": "低置信"}
    NEGATIVE_ACTIONS = []

    # ===== 跨模块数据流实现 =====
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        if entry.get("status") == "已入库":
            # 链路 L1: entity_extract → relation_model（实体入库后自动创建关系建模记录）
            rel_msg = self._push_to_relation_model(entry)
            message += "；" + rel_msg
            # 链路 L2: entity_extract → entity_link（实体入库后预创建链接候选）
            link_msg = self._push_to_entity_link(entry)
            message += "；" + link_msg
        return entry, message

    @staticmethod
    def _reconcile(existing: dict[str, Any], values: dict[str, Any]) -> list[str]:
        """兼容历史候选：只补全无效证据字段，已生效的业务值不被重复提交覆盖。"""
        changed: list[str] = []
        for field, new_value in values.items():
            old_value = existing.get(field)
            if _is_valid_evidence(old_value):
                continue
            existing[field] = new_value if _is_valid_evidence(new_value) else None
            if existing[field] != old_value:
                changed.append(field)
        return changed

    def _push_to_relation_model(self, entry: dict[str, Any]) -> str:
        relation_rows = store.rows("relation_model")
        marker = f"from_entity_{entry.get('实体编号', '')}"
        existing = next((r for r in relation_rows if r.get("关系权重") == marker), None)
        # 关系构建证据数据写入推导输入：抽取规则 → 推导规则，空值保持未知
        values = {
            "主体类型": _evidence_text(entry.get("实体类型")),
            "客体类型": _evidence_text(entry.get("数据来源")),
            "推导规则": _evidence_text(entry.get("抽取规则")),
        }
        if existing is not None:
            changed = self._reconcile(existing, values)
            if changed:
                return f"已校准关系建模记录 {existing.get('关系编号')} 的证据字段（{'、'.join(changed)}）"
            return f"关系建模记录 {existing.get('关系编号')} 已存在，本次入库不重复生效"
        next_id = max((int(r.get("id", 0)) for r in relation_rows), default=0) + 1
        rel_entry: dict[str, Any] = {
            "id": next_id,
            "status": "草稿",
            "pending": True,  # 关系证据资格结果同步到建模待办
            "abnormal": False,
            "关系编号": f"RELA-AUTO-{next_id:04d}",
            "关系名": f"auto_derive_{entry.get('实体类型') or 'unknown'}",
            "主体类型": values["主体类型"],
            "客体类型": values["客体类型"],
            "关系权重": marker,
            "推导规则": values["推导规则"],
            "反关系": f"inv_{next_id}",
            "使用次数": "0",
        }
        relation_rows.append(rel_entry)
        if values["推导规则"] is None:
            return f"已向关系建模推送自动关系 {rel_entry['关系编号']}（抽取规则缺失，推导输入待补）"
        return f"已向关系建模推送自动关系 {rel_entry['关系编号']}"

    def _push_to_entity_link(self, entry: dict[str, Any]) -> str:
        link_rows = store.rows("entity_link")
        source = _evidence_text(entry.get("实体编号"))
        existing = next(
            (
                r for r in link_rows
                if r.get("源实体") == source and r.get("链接类型") == "自动发现"
            ),
            None,
        )
        # 空值不等于可以接受的分数：置信阈值缺失时不允许用默认值顶替
        score = _evidence_text(entry.get("置信阈值"))
        values = {
            "目标实体": _evidence_text(entry.get("实体类型")),
            "置信度": score,
            "链接证据": _evidence_text(entry.get("消岐策略")),
            "链接时间": _evidence_text(entry.get("最后抽取")),
        }
        if existing is not None:
            changed = self._reconcile(existing, values)
            if changed:
                return f"已校准实体链接候选 {existing.get('规则编号')} 的证据字段（{'、'.join(changed)}）"
            return f"实体链接候选 {existing.get('规则编号')} 已存在，本次入库不重复生效"
        if score is None:
            # 缺证环节不能跟着另一侧一起确认：实体链接只领取带分数依据的候选
            return "置信阈值未评审，实体链接暂不领取候选（缺少分数依据）"
        next_id = max((int(r.get("id", 0)) for r in link_rows), default=0) + 1
        link_entry: dict[str, Any] = {
            "id": next_id,
            "status": "待确认",
            "pending": True,
            "abnormal": False,
            "规则编号": f"ENTI-LINK-{next_id:04d}",
            "源实体": source,
            "目标实体": values["目标实体"],
            "链接类型": "自动发现",
            "置信度": values["置信度"],
            "链接证据": values["链接证据"],
            "人工确认": "待确认",
            "链接时间": values["链接时间"],
        }
        link_rows.append(link_entry)
        return f"已向实体链接推送候选 {link_entry['规则编号']}"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["cross_module_consumers"] = ["relation_model", "entity_link"]
        return base
