"""纯函数：把共享组的目标状态推导为可重放的实际航段与航链引用计划。

裁决、补偿、恢复都消费同一份计划；计划不含任何累加量，计数由活跃航段
与成员关系重新推导，因此重放不会重复扣增。
"""
from .contracts import choose_representative


def own_leg(record_id: str) -> str:
    return f"leg:{record_id}"


def merged_leg(group_id: str, version: int) -> str:
    return f"leg:grp:{group_id}:v{version}"


def build_plan(conn, repo, group, members, decision: str, target_version: int,
               source_record_id: str | None = None) -> dict:
    """decision: MERGE 单条实际航段；SEPARATE 每个营销来源一条；PENDING 暂不占用实际航段。"""
    if decision not in ("MERGE", "SEPARATE", "PENDING"):
        raise ValueError("BAD_DECISION")
    group_id = group["group_id"]

    if decision == "MERGE":
        legs = [{
            "leg_id": merged_leg(group_id, target_version),
            "record_ids": sorted(row["record_id"] for row in members),
            "representative_record_id": choose_representative(members, source_record_id),
        }]
        status = "MERGED"
    elif decision == "SEPARATE":
        legs = [{
            "leg_id": own_leg(row["record_id"]),
            "record_ids": [row["record_id"]],
            "representative_record_id": row["record_id"],
        } for row in members]
        status = "SEPARATE"
    else:
        legs = []
        status = "PENDING"

    by_record = {record_id: leg["leg_id"] for leg in legs for record_id in leg["record_ids"]}
    plan_leg_ids = {leg["leg_id"] for leg in legs}
    group_leg_ids = {leg["leg_id"] for leg in repo.group_legs(conn, group_id)}
    retiring = group_leg_ids - plan_leg_ids
    settled = {
        record_id: leg_id for record_id, leg_id in repo.settled_map(conn).items()
        if leg_id not in retiring
    }
    settled.update(by_record)

    refs_by_chain, suspended = {}, []
    for chain_id in repo.chains_touching_records(conn, [row["record_id"] for row in members]):
        before = {row["position"]: row for row in repo.chain_refs(conn, chain_id)}
        items = []
        for position in repo.chain_positions(conn, chain_id):
            leg_id = settled.get(position["record_id"])
            old = before.get(position["position"])
            revoked = (old is None or not old["leg_id"] or old["leg_id"] in retiring
                       or str(old["leg_id"]).startswith("leg:grp:")) and leg_id is None
            reason = None
            if leg_id is None:
                reason = "MERGE_REVOKED" if revoked else "UNSETTLED_RECORD"
            item = {
                "position": position["position"],
                "leg_id": leg_id if leg_id is not None else (old["leg_id"] if old else None),
                "state": "ACTIVE" if leg_id is not None else "SUSPENDED",
                "reason_code": reason,
            }
            items.append(item)
            if item["state"] == "SUSPENDED":
                suspended.append({"chain_id": chain_id, "position": item["position"],
                                  "leg_id": item["leg_id"], "reason_code": reason})
        refs_by_chain[chain_id] = items

    return {
        "group_id": group_id,
        "status": status,
        "decision": decision,
        "target_version": target_version,
        "legs": legs,
        "leg_ids": sorted(plan_leg_ids),
        "retire_leg_ids": sorted(group_leg_ids - plan_leg_ids),
        "refs_by_chain": refs_by_chain,
        "suspended_refs": suspended,
        "settled_record_ids": sorted(by_record),
        "representative_record_id": (
            legs[0]["representative_record_id"] if decision == "MERGE" else None
        ),
    }


def apply_plan(conn, repo, group, plan: dict, actor: str, created_at: str) -> dict:
    """幂等执行；重复调用不改变最终状态，也不产生额外计数。"""
    group_id = group["group_id"]
    applied = {"legs": [], "chains": [], "retired": []}

    for leg in plan["legs"]:
        first = repo.get_record(conn, leg["representative_record_id"])
        repo.upsert_leg(conn, {
            "leg_id": leg["leg_id"],
            "group_id": group_id,
            "owner_record_id": leg["representative_record_id"],
            "batch_id": first["batch_id"],
            "origin": first["origin"],
            "destination": first["destination"],
            "flight_date": first["flight_date"],
            "departure_local": first["departure_local"],
            "arrival_local": first["arrival_local"],
            "departure_fold": first["departure_fold"],
            "arrival_fold": first["arrival_fold"],
            "operating_carrier": first["operating_carrier"],
            "operating_flight_no": first["operating_flight_no"],
            "state": "ACTIVE",
            "created_by": actor,
            "created_at": created_at,
        })
        repo.set_leg_active(conn, leg["leg_id"], True)
        repo.set_leg_members(conn, leg["leg_id"], leg["record_ids"])
        applied["legs"].append(leg["leg_id"])

    for leg_id in plan["retire_leg_ids"]:
        if repo.set_leg_active(conn, leg_id, False):
            applied["retired"].append(leg_id)

    for chain_id, items in plan["refs_by_chain"].items():
        repo.replace_chain_refs(conn, chain_id, items)
        applied["chains"].append(chain_id)

    return applied
