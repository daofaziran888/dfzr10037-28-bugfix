"""入库派生证据链回归验证：sensor-4 场景。

覆盖点：
1. 抽取登记省掉阈值/抽取规则时，派生证据字段保持未知（None），不再显示成文本 "None"；
2. 空值不等于可以接受的分数：置信阈值未评审时实体链接不领取候选，也不套用默认分；
3. 明确给出的数值 0 保留为 "0"，不被改成默认值；
4. 重复提交同次入库只生效一次，缺证环节不跟着另一侧一起确认；
5. 关系证据资格结果同步到建模待办，证据数据写入推导输入；
6. 历史残留记录（旧链路写出的 "None" 文本）不被误当成有效候选，重试入库时被校准而非复制。

运行：cd backend && .venv/bin/python tests/test_ingest_evidence.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import service_for  # noqa: E402
from app.store import store  # noqa: E402

extract = service_for("entity_extract")
assert extract is not None

MODELING_EVIDENCE = "sensor-catalog-belonging-rule"  # 足以建模的证据（抽取规则）
ZERO_SCORE = 0  # 明确零分（置信阈值）

checks = 0


def ok(label: str) -> None:
    global checks
    checks += 1
    print(f"  PASS {checks:02d} {label}")


def reset() -> None:
    for module in ("entity_extract", "relation_model", "entity_link"):
        store.rows(module).clear()


def register_sensor4(**overrides):
    values = {
        "实体编号": "sensor-4",
        "实体类型": "温度传感器",          # 有效关系类型
        "数据来源": "iot-device-catalog",  # 目录
        "抽取规则": None,                  # 建模证据待补
        "同义词典": None,
        "消岐策略": None,
        "置信阈值": None,                  # 链接置信度尚未评审
        "最后抽取": None,
    }
    values.update(overrides)
    entry, missing = extract.create_entry(values)
    assert not missing, f"登记失败：{missing}"
    return entry


def auto_relations(source: str = "sensor-4") -> list[dict]:
    return [r for r in store.rows("relation_model") if r.get("关系权重") == f"from_entity_{source}"]


def auto_links(source: str = "sensor-4") -> list[dict]:
    return [
        r for r in store.rows("entity_link")
        if r.get("源实体") == source and r.get("链接类型") == "自动发现"
    ]


def ingest(entry: dict) -> str:
    result, message = extract.run_action(int(entry["id"]), "确认入库")
    assert result is not None, message
    return message


def assert_no_none_text() -> None:
    for module, fields in (
        ("relation_model", ["主体类型", "客体类型", "推导规则"]),
        ("entity_link", ["目标实体", "置信度", "链接证据", "链接时间"]),
    ):
        for row in store.rows(module):
            for field in fields:
                assert row.get(field) != "None", f"{module}.{field} 出现文本 None：{row}"


print("场景 A：sensor-4 干净链路（省掉阈值与抽取规则 → 补证据 → 补零分）")
reset()
sensor4 = register_sensor4()

msg = ingest(sensor4)
rels, links = auto_relations(), auto_links()
assert len(rels) == 1, f"建模侧应有一条待复核关系，实际 {len(rels)}"
assert rels[0]["推导规则"] is None, "抽取规则缺失时推导输入应保持未知 None"
assert rels[0]["主体类型"] == "温度传感器" and rels[0]["客体类型"] == "iot-device-catalog"
assert rels[0]["pending"] is True and rels[0]["status"] == "草稿"
ok("首次入库：建模侧生成待复核关系，推导输入为未知 None（非文本 None），并同步到建模待办")
assert links == [], f"置信度未评审时链接侧不应领取候选，实际 {len(links)} 条"
assert "缺少分数依据" in msg
ok("首次入库：链接侧拿不到合法评分，不领取候选，也不套用默认分")

msg = ingest(sensor4)
assert len(auto_relations()) == 1 and auto_links() == []
assert "不重复生效" in msg
ok("重复提交同次入库只生效一次，派生记录不增不减")

sensor4["抽取规则"] = MODELING_EVIDENCE  # 补给一条足以建模的证据
msg = ingest(sensor4)
rels, links = auto_relations(), auto_links()
assert len(rels) == 1 and rels[0]["推导规则"] == MODELING_EVIDENCE
assert links == [], "链接侧缺证，不能跟着建模侧一起确认"
ok("补给建模证据后重试：推导输入写入真实证据，链接侧仍不跟随确认")

sensor4["置信阈值"] = ZERO_SCORE  # 补给一个明确零分
msg = ingest(sensor4)
rels, links = auto_relations(), auto_links()
assert len(links) == 1, f"补零分后应生成一条链接候选，实际 {len(links)}"
assert links[0]["置信度"] == "0", f"明确零分应保留为 '0'，实际 {links[0]['置信度']!r}"
assert links[0]["置信度"] not in ("0.6", "None")
assert links[0]["链接证据"] is None and links[0]["链接时间"] is None
assert len(rels) == 1 and rels[0]["推导规则"] == MODELING_EVIDENCE
ok("补给明确零分后重试：链接候选置信度为 '0'，未改成默认值，其余未知字段保持 None")

msg = ingest(sensor4)
assert len(auto_relations()) == 1 and len(auto_links()) == 1
assert msg.count("不重复生效") == 2
ok("两路证据都已生效后再次重复提交：两侧均不重复生效")

assert_no_none_text()
ok("回查可接手证据字段：全量派生记录无文本 None 残留")

print("场景 B：历史残留记录（旧链路写出的文本 None）不被误当成有效候选")
reset()
sensor4 = register_sensor4()
# 模拟旧链路在同一实体上留下的残留派生记录
store.rows("relation_model").append({
    "id": 900, "status": "草稿", "pending": True, "abnormal": False,
    "关系编号": "RELA-AUTO-0900", "关系名": "auto_derive_温度传感器",
    "主体类型": "温度传感器", "客体类型": "iot-device-catalog",
    "关系权重": "from_entity_sensor-4", "推导规则": "None",
    "反关系": "inv_900", "使用次数": "0",
})
store.rows("entity_link").append({
    "id": 900, "status": "待确认", "pending": True, "abnormal": False,
    "规则编号": "ENTI-LINK-0900", "源实体": "sensor-4", "目标实体": "温度传感器",
    "链接类型": "自动发现", "置信度": "None", "链接证据": "None",
    "人工确认": "待确认", "链接时间": "None",
})

msg = ingest(sensor4)
rels, links = auto_relations(), auto_links()
assert len(rels) == 1 and rels[0]["id"] == 900 and rels[0]["推导规则"] is None
assert len(links) == 1 and links[0]["id"] == 900 and links[0]["置信度"] is None
ok("重试入库兼容历史候选：残留记录被识别并归位为未知，不复制新行")

sensor4["抽取规则"] = MODELING_EVIDENCE
sensor4["置信阈值"] = ZERO_SCORE
msg = ingest(sensor4)
rels, links = auto_relations(), auto_links()
assert len(rels) == 1 and rels[0]["推导规则"] == MODELING_EVIDENCE
assert len(links) == 1 and links[0]["置信度"] == "0"
assert links[0]["链接证据"] is None
ok("残留记录未被误当成有效候选：真实证据与零分原位校准，派生记录仍各一条")

assert_no_none_text()
ok("回查：历史残留的文本 None 已全部归位，无有效候选误判")

print("场景 C：首次入库即带明确零分与建模证据")
reset()
sensor5 = register_sensor4()
sensor5["实体编号"] = "sensor-5"
sensor5["抽取规则"] = MODELING_EVIDENCE
sensor5["置信阈值"] = ZERO_SCORE
msg = ingest(sensor5)
rels = auto_relations("sensor-5")
links = auto_links("sensor-5")
assert len(rels) == 1 and rels[0]["推导规则"] == MODELING_EVIDENCE
assert len(links) == 1 and links[0]["置信度"] == "0"
ok("首次入库：零分直接生效为 '0'，建模证据直接写入推导输入")

assert all(r.get("置信度") != "0.6" for r in store.rows("entity_link"))
ok("全量链接候选中不存在被默认值顶替的分数")

print(f"\n全部 {checks} 项检查通过。")
