"""共享航班跨批次采信、归并裁决与撤销补偿。"""
import hashlib
import json
from datetime import datetime, timezone

from app.errors import DomainError
from app.reasons import REASONS
from . import repo
from .contracts import (
    conflict_evidence,
    declared_operating_key,
    group_id_for,
    normalize_record,
)
from .plan import apply_plan, build_plan

MARKETING_KEY_FIELDS = (
    "marketing_carrier", "marketing_flight_no", "flight_date", "origin", "destination",
)

COMPENSATION_STEPS = (
    (1, "SNAPSHOT", "记录归并状态、各批次计数与航链引用"),
    (2, "SPLIT", "撤销归并并拆回原航段"),
    (3, "RECOMPUTE", "重算受影响批次计数并核对航迹"),
    (4, "SETTLE", "提交补偿版本与完成状态"),
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def envelope(**fields) -> dict:
    return {"ok": True, "state": "COMPLETED", "pending_decision": False, **fields}


# import --------------------------------------------------------------------

def import_batch(payload: dict) -> dict:
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise DomainError("EMPTY_VALUE", "批次必须包含至少一行 rows")
    canonical = json.dumps(
        {"note": payload.get("note"), "rows": rows},
        ensure_ascii=False, sort_keys=True, default=str,
    ).encode("utf-8")
    batch_id = "B-" + hashlib.sha256(canonical).hexdigest()[:12]
    created_at = now()
    repo.init()

    with repo.transaction() as conn:
        if repo.get_batch(conn, batch_id) is not None:
            return {**_batch_view(conn, batch_id), "created": False}

        accepted, rejected = _classify(conn, rows, batch_id)
        for line_no, (code, source) in sorted(rejected.items()):
            repo.quarantine(conn, batch_id, line_no, code, source)

        touched = {}
        for record in accepted:
            record["state"] = "ACCEPTED"
            if record["declaration_key"]:
                record["group_id"] = group_id_for(record["declaration_key"])
                touched[record["group_id"]] = record["declaration_key"]
            repo.insert_record(conn, record)

        repo.insert_batch(conn, batch_id, hashlib.sha256(canonical).hexdigest(),
                          len(accepted), len(rejected), payload.get("note"), created_at)
        for group_id, declaration_key in sorted(touched.items()):
            _reevaluate_group(conn, group_id, declaration_key, created_at)
        for record in accepted:
            if record["declaration_key"] is None:
                _settle_own_leg(conn, record["record_id"], "import")
        repo.recompute_metrics(conn)
        return {**_batch_view(conn, batch_id), "created": True}


def _classify(conn, rows: list[dict], batch_id: str):
    accepted, rejected, seen = [], {}, {}
    for line_no, source in enumerate(rows, start=2):
        try:
            record = normalize_record(source, batch_id, line_no)
        except DomainError as exc:
            rejected[line_no] = (exc.code, source)
            continue

        marketing_key = tuple(record[field] for field in MARKETING_KEY_FIELDS)
        schedule = (record["departure_local"], record["arrival_local"],
                    record["departure_fold"], record["arrival_fold"])
        if marketing_key in seen:
            same = seen[marketing_key] == (schedule, record["operating_carrier"],
                                           record["operating_flight_no"])
            rejected[line_no] = ("DUPLICATE_KEY" if same else "VALUE_CONFLICT", source)
            continue
        prior = _existing_marketing(conn, marketing_key)
        if prior is not None:
            same = (prior["departure_local"], prior["arrival_local"],
                    prior["departure_fold"], prior["arrival_fold"]) == schedule and (
                prior["operating_carrier"], prior["operating_flight_no"]) == (
                record["operating_carrier"], record["operating_flight_no"])
            rejected[line_no] = ("DUPLICATE_KEY" if same else "VALUE_CONFLICT", source)
            continue
        seen[marketing_key] = (schedule, record["operating_carrier"], record["operating_flight_no"])
        record["declaration_key"] = declared_operating_key(record)
        accepted.append(record)
    return accepted, rejected


def _existing_marketing(conn, marketing_key):
    row = conn.execute(
        "SELECT * FROM cs_records WHERE state='ACCEPTED' AND marketing_carrier=?"
        " AND marketing_flight_no=? AND flight_date=? AND origin=? AND destination=?"
        " ORDER BY batch_id, line_no LIMIT 1",
        marketing_key,
    ).fetchone()
    return row


def _reevaluate_group(conn, group_id: str, declaration_key: str, created_at: str) -> None:
    """按承运方声明重新评估共享组；只有声明冲突才停在待裁决。"""
    members = repo.accepted_by_declaration(conn, declaration_key)
    if not members:
        return
    group = repo.get_group(conn, group_id)
    if group is None:
        repo.insert_group(conn, {
            "group_id": group_id,
            "declaration_key": declaration_key,
            "status": "PENDING",
            "version": 0,
            "members": [row["record_id"] for row in members],
        })
        group = repo.get_group(conn, group_id)

    member_ids = [row["record_id"] for row in members]
    previous_members = set(repo.group_members(group))
    repo.set_records_group(conn, member_ids, group_id)
    conflict = conflict_evidence(members)
    actor = "declaration"
    # 成员变化会推进版本，旧版本裁决与补偿一律失效，避免并发覆盖。
    version = group["version"] + (1 if set(member_ids) != previous_members else 0)

    if _mid_compensation(conn, group_id):
        # 补偿进行中不重排实际航段；记录先挂在组上，结算时统一拆回。
        repo.update_group(conn, group_id, decision=group["decision"], members=member_ids,
                          version=version)
        return

    if conflict is not None:
        plan = build_plan(conn, repo, group, members, "PENDING", version)
        apply_plan(conn, repo, group, plan, actor, created_at)
        repo.update_group(
            conn, group_id, status="PENDING", decision=None, merged_leg_id=None,
            representative_record_id=None, source_record_id=None, members=member_ids,
            version=version,
        )
        return

    if group["decision"] in ("SEPARATE", "COMPENSATE"):
        # 人工拆分或已撤销归并的组，不因新来源自动重新归并。
        plan = build_plan(conn, repo, group, members, "SEPARATE", version)
        apply_plan(conn, repo, group, plan, actor, created_at)
        repo.update_group(
            conn, group_id, status="SEPARATE", merged_leg_id=None,
            representative_record_id=None, source_record_id=None, members=member_ids,
            version=version,
        )
        return

    plan = build_plan(conn, repo, group, members, "MERGE", version)
    apply_plan(conn, repo, group, plan, actor, created_at)
    repo.update_group(
        conn, group_id, status="MERGED", decision="DECLARATION",
        merged_leg_id=plan["leg_ids"][0],
        representative_record_id=plan["representative_record_id"],
        source_record_id=None, members=member_ids, version=version,
    )


def _settle_own_leg(conn, record_id: str, actor: str) -> str:
    record = repo.get_record(conn, record_id)
    leg_id = repo.own_leg_id(record_id)
    repo.upsert_leg(conn, {
        "leg_id": leg_id, "group_id": None, "owner_record_id": record_id,
        "batch_id": record["batch_id"], "origin": record["origin"],
        "destination": record["destination"], "flight_date": record["flight_date"],
        "departure_local": record["departure_local"], "arrival_local": record["arrival_local"],
        "departure_fold": record["departure_fold"], "arrival_fold": record["arrival_fold"],
        "operating_carrier": record["operating_carrier"],
        "operating_flight_no": record["operating_flight_no"],
        "state": "ACTIVE", "created_by": actor, "created_at": now(),
    })
    repo.set_leg_active(conn, leg_id, True)
    repo.set_leg_members(conn, leg_id, [record_id])
    repo.refresh_chain_refs(conn, [record_id])
    return leg_id


# adjudication --------------------------------------------------------------

def adjudicate(group_id: str, payload: dict) -> dict:
    decision = str(payload.get("decision") or "").strip().upper()
    if decision not in ("MERGE", "SEPARATE"):
        raise DomainError("BAD_DECISION", "decision 只能是 MERGE 或 SEPARATE")
    expected = _expected_version(payload)
    source_record_id = payload.get("source_record_id")
    actor = _actor(payload)
    repo.init()

    with repo.transaction() as conn:
        group = _require_group(conn, group_id)
        _reject_unfinished_compensation(conn, group_id)
        if expected != group["version"]:
            raise _version_conflict(group)
        members = repo.accepted_by_group(conn, group_id)
        if not members:
            raise DomainError("NOT_FOUND", "共享组没有已采信的营销记录", 404)
        member_ids = {row["record_id"] for row in members}
        if source_record_id is not None and source_record_id not in member_ids:
            raise DomainError("BAD_SOURCE", "source_record_id 不属于该共享组")

        conflict = conflict_evidence(members)
        if decision == "MERGE":
            if conflict is not None and source_record_id is None:
                raise DomainError(
                    "UNRESOLVED_CONFLICT", "来源时刻冲突未裁决，不能直接归并", 409,
                    {"conflict": conflict, "version": group["version"]},
                )
            if group["status"] == "MERGED" and conflict is None:
                raise DomainError("NO_EFFECT", "共享组已处于归并状态，未生成新版本", 409)
        else:
            if group["status"] == "SEPARATE":
                raise DomainError("NO_EFFECT", "共享组已处于拆分状态，未生成新版本", 409)

        target = group["version"] + 1
        plan = build_plan(conn, repo, group, members, decision, target, source_record_id)
        applied = apply_plan(conn, repo, group, plan, actor, now())
        if decision == "MERGE":
            repo.update_group(
                conn, group_id, status="MERGED", decision="MERGE",
                merged_leg_id=plan["leg_ids"][0],
                representative_record_id=plan["representative_record_id"],
                source_record_id=source_record_id, version=target,
                decided_by=actor, decided_at=now(), members=sorted(member_ids),
            )
        else:
            repo.update_group(
                conn, group_id, status="SEPARATE", decision="SEPARATE",
                merged_leg_id=None, representative_record_id=None, source_record_id=None,
                version=target, decided_by=actor, decided_at=now(), members=sorted(member_ids),
            )
        repo.recompute_metrics(conn)
        view = _group_view(conn, repo.get_group(conn, group_id))
        return envelope(
            action=decision,
            group=view,
            version=target,
            merged_leg_id=view["merged_leg_id"],
            leg_count=view["leg_count"],
            marketing_count=view["marketing_count"],
            folded_marketing=view["marketing_count"] - view["leg_count"],
            applied=applied,
            conflict=conflict,
        )


# compensation --------------------------------------------------------------

def compensate(group_id: str, payload: dict) -> dict:
    expected = _expected_version(payload)
    op_id = str(payload.get("op_id") or "").strip()
    if not op_id:
        raise DomainError("OP_ID_REQUIRED", "补偿必须提供稳定的 op_id，以支持中断恢复与重试去重")
    actor = _actor(payload)
    interrupt_after = payload.get("interrupt_after_step")
    if interrupt_after is not None and (
        not isinstance(interrupt_after, int) or isinstance(interrupt_after, bool)
        or not 1 <= interrupt_after < len(COMPENSATION_STEPS)
    ):
        raise DomainError("BAD_FORMAT", "interrupt_after_step 必须是 1..3 的整数")
    repo.init()

    with repo.reading() as conn:
        existing = repo.get_operation(conn, op_id)
        if existing is not None:
            if existing["group_id"] != group_id:
                raise DomainError("OP_ID_CONFLICT", "op_id 已用于其他共享组", 409)
            if existing["expected_version"] != expected:
                raise DomainError("OP_ID_CONFLICT", "op_id 已登记不同 expected_version", 409)
            # 同一 op_id 重试：已完成则回放结果，中断则从断点续跑，均不重复扣增。
            if existing["status"] in ("COMPENSATED", "SETTLED"):
                return _compensation_result(conn, existing)
            return _run_compensation(op_id, interrupt_after)
        _reject_unfinished_compensation(conn, group_id)

    with repo.transaction() as conn:
        group = _require_group(conn, group_id)
        if expected != group["version"]:
            raise _version_conflict(group)
        if group["status"] != "MERGED":
            raise DomainError("NOT_COMPENSABLE", "只有已归并的共享组可以撤销归并", 409)
        repo.insert_operation(conn, {
            "op_id": op_id, "kind": "unmerge", "group_id": group_id,
            "status": "COMPENSATING", "expected_version": expected,
            "payload": {"actor": actor, "reason": payload.get("reason"),
                        "group_status": group["status"], "merge_version": expected},
            "created_at": now(),
        })
        # 组状态显式进入补偿中，回读能区分已归并与补偿中。
        repo.update_group(conn, group_id, status="COMPENSATING")
    return _run_compensation(op_id, interrupt_after)


def _run_compensation(op_id: str, interrupt_after: int | None = None) -> dict:
    resumed = False
    for step_no, name, _ in COMPENSATION_STEPS:
        with repo.reading() as conn:
            if repo.get_step(conn, op_id, step_no) is not None:
                resumed = True
                continue
            op = repo.get_operation(conn, op_id)
            group = _require_group(conn, op["group_id"])
            if resumed and group["version"] not in (
                op["expected_version"], op["expected_version"] + 1
            ):
                raise DomainError(
                    "VERSION_CONFLICT", "补偿中断期间版本被其他裁决改写，请回读后重试", 409,
                    {"current_version": group["version"], "op_id": op_id},
                )
            repo.mark_running(conn, op_id, now())
        _STEP_HANDLERS[step_no](op_id, name)
        if interrupt_after == step_no:
            stamp = now()
            with repo.transaction() as conn:
                repo.update_operation(conn, op_id, "INTERRUPTED", stamp)
            raise DomainError(
                "COMPENSATION_INTERRUPTED",
                f"补偿在步骤 {step_no} 后中断，可用同一 op_id 恢复",
                409,
                {"op_id": op_id, "completed_step": step_no,
                 "resume_from": step_no + 1},
            )
    with repo.reading() as conn:
        return _compensation_result(conn, repo.get_operation(conn, op_id))


def _op_context(conn, op_id: str):
    op = repo.get_operation(conn, op_id)
    if op is None:
        raise DomainError("NOT_FOUND", "补偿操作不存在", 404)
    payload = json.loads(op["payload_json"])
    members = repo.accepted_by_group(conn, op["group_id"])
    return op, payload, members


def _step_snapshot(op_id: str, name: str) -> None:
    stamp = now()
    with repo.transaction() as conn:
        op, payload, members = _op_context(conn, op_id)
        group = _require_group(conn, op["group_id"])
        legs = repo.group_legs(conn, op["group_id"])
        refs = repo.refs_for_legs(conn, [leg["leg_id"] for leg in legs])
        result = {
            "version": group["version"],
            "status": group["status"],
            "merged_leg_id": group["merged_leg_id"],
            "member_record_ids": sorted(row["record_id"] for row in members),
            "leg_ids": sorted(leg["leg_id"] for leg in legs),
            "chain_refs": refs,
            "batch_metrics": _metrics_rows(conn),
        }
        repo.complete_step(conn, op_id, 1, name, result, stamp)


def _step_split(op_id: str, name: str) -> None:
    stamp = now()
    with repo.transaction() as conn:
        op, payload, members = _op_context(conn, op_id)
        group = _require_group(conn, op["group_id"])
        target = op["expected_version"] + 1
        plan = build_plan(conn, repo, group, members, "SEPARATE", target)
        applied = apply_plan(conn, repo, group, plan, f"compensate:{op_id}", stamp)
        repo.update_group(conn, op["group_id"], status="COMPENSATING")
        result = {
            "target_version": target,
            "leg_ids": plan["leg_ids"],
            "retired_leg_ids": plan["retire_leg_ids"],
            "chains_repaired": sorted(plan["refs_by_chain"]),
            "suspended_refs": plan["suspended_refs"],
            "applied": applied,
        }
        repo.complete_step(conn, op_id, 2, name, result, stamp)


def _step_recompute(op_id: str, name: str) -> None:
    stamp = now()
    with repo.transaction() as conn:
        op, payload, members = _op_context(conn, op_id)
        snapshot = repo.get_step(conn, op_id, 1)
        before = json.loads(snapshot["result_json"])["batch_metrics"] if snapshot else []
        repo.recompute_metrics(conn)
        after = _metrics_rows(conn)
        deltas = _metric_deltas(before, after)
        reconciliation = _reconcile(conn)
        result = {
            "batch_metrics": after,
            "batch_deltas": deltas,
            "reconciliation": reconciliation,
        }
        repo.complete_step(conn, op_id, 3, name, result, stamp)


def _step_settle(op_id: str, name: str) -> None:
    stamp = now()
    with repo.transaction() as conn:
        op, payload, members = _op_context(conn, op_id)
        target = op["expected_version"] + 1
        # 补偿期间新到的来源也一并拆回，避免遗留未结算的营销记录。
        for record in repo.accepted_by_group(conn, op["group_id"]):
            repo.upsert_leg(conn, {
                "leg_id": repo.own_leg_id(record["record_id"]), "group_id": op["group_id"],
                "owner_record_id": record["record_id"], "batch_id": record["batch_id"],
                "origin": record["origin"], "destination": record["destination"],
                "flight_date": record["flight_date"],
                "departure_local": record["departure_local"],
                "arrival_local": record["arrival_local"],
                "departure_fold": record["departure_fold"],
                "arrival_fold": record["arrival_fold"],
                "operating_carrier": record["operating_carrier"],
                "operating_flight_no": record["operating_flight_no"],
                "state": "ACTIVE", "created_by": f"compensate:{op_id}", "created_at": stamp,
            })
            repo.set_leg_active(conn, repo.own_leg_id(record["record_id"]), True)
            repo.set_leg_members(conn, repo.own_leg_id(record["record_id"]),
                                 [record["record_id"]])
            repo.refresh_chain_refs(conn, [record["record_id"]])
        repo.update_group(
            conn, op["group_id"], status="SEPARATE", decision="COMPENSATE",
            merged_leg_id=None, representative_record_id=None, source_record_id=None,
            version=target, decided_by=payload["actor"], decided_at=stamp,
        )
        repo.complete_step(conn, op_id, 4, name,
                           {"version": target, "status": "SEPARATE"}, stamp)
        repo.update_operation(conn, op_id, "COMPENSATED", stamp)
        repo.recompute_metrics(conn)


_STEP_HANDLERS = {
    1: _step_snapshot,
    2: _step_split,
    3: _step_recompute,
    4: _step_settle,
}


# chains --------------------------------------------------------------------

def create_chain(payload: dict) -> dict:
    record_ids = payload.get("record_ids")
    if not isinstance(record_ids, list) or len(record_ids) < 2:
        raise DomainError("BAD_CHAIN", "航链至少包含两个营销记录")
    chain_id = str(payload.get("chain_id") or "").strip()
    if not chain_id:
        digest = hashlib.sha256("|".join(map(str, record_ids)).encode()).hexdigest()[:12]
        chain_id = f"C-{digest}"
    repo.init()
    with repo.transaction() as conn:
        for record_id in record_ids:
            record = repo.get_record(conn, record_id)
            if record is None or record["state"] != "ACCEPTED":
                raise DomainError("NOT_FOUND", f"营销记录不存在或未采信：{record_id}", 404)
        repo.insert_chain(conn, chain_id, [str(item) for item in record_ids], now())
        repo.refresh_chain_refs(conn, [str(item) for item in record_ids])
        return _chain_view(conn, chain_id)


# readback ------------------------------------------------------------------

def readback(conn) -> dict:
    groups = [_group_view(conn, row) for row in repo.list_groups(conn)]
    operations = [_operation_view(conn, row) for row in repo.list_operations(conn)]
    by_status = {}
    for view in groups:
        by_status.setdefault(view["status"], []).append(view)
    return {
        "ok": True,
        "state": "READBACK",
        "merged": by_status.get("MERGED", []),
        "pending_decision": by_status.get("PENDING", []),
        "separate": by_status.get("SEPARATE", []),
        "compensating": [
            view for view in operations if view["status"] in ("COMPENSATING", "INTERRUPTED")
        ],
        "compensating_groups": by_status.get("COMPENSATING", []),
        "completed_compensations": [
            view for view in operations if view["status"] in ("COMPENSATED", "SETTLED")
        ],
        "groups_by_status": {key: len(value) for key, value in sorted(by_status.items())},
        "chains": [_chain_view(conn, chain_id) for chain_id in repo.list_chain_ids(conn)],
        "quarantined_conflicts": _quarantined_conflicts(conn),
        "batches": [_batch_view(conn, row["batch_id"]) for row in repo.list_batches(conn)],
        "reconciliation": _reconcile(conn),
    }


def _quarantined_conflicts(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM cs_records WHERE state='QUARANTINED'"
        " AND reason_code IN ('VALUE_CONFLICT','BAD_DECLARATION','DUPLICATE_KEY')"
        " ORDER BY batch_id, line_no"
    ).fetchall()
    return [
        {
            "record_id": row["record_id"], "batch_id": row["batch_id"],
            "line_no": row["line_no"], "reason_code": row["reason_code"],
            "reason": REASONS.get(row["reason_code"], row["reason_code"]),
            "raw": _raw(row),
        }
        for row in rows
    ]


def _reconcile(conn) -> dict:
    marketing = conn.execute(
        "SELECT COUNT(*) FROM cs_records WHERE state='ACCEPTED'"
    ).fetchone()[0]
    active = repo.active_legs(conn)
    merged_legs = [leg for leg in active if str(leg["leg_id"]).startswith("leg:grp:")]
    folded = sum(len(repo.leg_members(conn, leg["leg_id"])) - 1 for leg in merged_legs)
    settled = repo.settled_map(conn)
    accepted_ids = [row[0] for row in conn.execute(
        "SELECT record_id FROM cs_records WHERE state='ACCEPTED'"
    )]
    unsettled = [record_id for record_id in accepted_ids if record_id not in settled]
    doubly = repo.double_settled_records(conn)
    pending_ids = {
        row[0] for row in conn.execute(
            "SELECT r.record_id FROM cs_records r JOIN cs_groups g ON g.group_id=r.group_id"
            " WHERE r.state='ACCEPTED' AND g.status='PENDING'"
        )
    }
    # 未结算里只有“待裁决”是预期状态；其余才是真正丢账。
    unaccounted = [record_id for record_id in unsettled if record_id not in pending_ids]
    quarantined = conn.execute(
        "SELECT COUNT(*) FROM cs_records WHERE state='QUARANTINED'"
    ).fetchone()[0]
    difference = marketing - len(active)
    # 恒等式：营销记录 = 实际航段 + 被吸收 + 未结算。
    expected_folded = marketing - len(active) - len(unsettled) - len(doubly)
    batches = [
        {
            "batch_id": row["batch_id"],
            "marketing": row["marketing"],
            "legs_owned": row["legs_owned"],
            "folded": row["folded"],
            "absorbed": row["absorbed"],
            "absorbing": row["absorbing"],
            "pending": row["pending"],
            "unsettled": row["unsettled"],
            # 每条营销记录恰好是一类：自持航段 / 同批次归并 / 跨批次吸收 / 未结算。
            "difference": (
                row["marketing"] - row["legs_owned"] - row["folded"]
                - row["absorbed"] - row["unsettled"]
            ),
            "balanced": (
                row["marketing"]
                == row["legs_owned"] + row["folded"] + row["absorbed"] + row["unsettled"]
                and row["unsettled"] == row["pending"]
            ),
        }
        for row in repo.list_metrics(conn)
    ]
    return {
        "marketing_records": marketing,
        "actual_legs": len(active),
        "merged_legs": len(merged_legs),
        "folded_marketing": folded,
        "pending_records": len(pending_ids),
        "unsettled_records": len(unsettled),
        "unaccounted_records": unaccounted,
        "double_settled_records": doubly,
        "quarantined_records": quarantined,
        "difference": difference,
        "expected_folded": expected_folded,
        # 守恒：营销记录 = 实际航段 + 被吸收 + 未结算；未结算必须全部是待裁决，且无重复计入。
        "balanced": not unaccounted and not doubly and folded == expected_folded
        and all(batch["balanced"] for batch in batches),
        "batches": batches,
    }


def _batch_view(conn, batch_id: str) -> dict:
    batch = repo.get_batch(conn, batch_id)
    if batch is None:
        raise DomainError("NOT_FOUND", "批次不存在", 404)
    rows = conn.execute(
        "SELECT * FROM cs_records WHERE batch_id=? ORDER BY line_no", (batch_id,)
    ).fetchall()
    accepted = [row for row in rows if row["state"] == "ACCEPTED"]
    quarantined = [row for row in rows if row["state"] == "QUARANTINED"]
    groups = sorted({row["group_id"] for row in accepted if row["group_id"]})
    metrics = conn.execute(
        "SELECT * FROM cs_batch_metrics WHERE batch_id=?", (batch_id,)
    ).fetchone()
    return {
        "ok": True,
        "batch_id": batch_id,
        "state": "COMPLETED",
        "note": batch["note"],
        "input": len(rows),
        "accepted": len(accepted),
        "quarantined": len(quarantined),
        "groups": [_group_summary(conn, repo.get_group(conn, group_id)) for group_id in groups],
        "evidence": [
            {"line_no": row["line_no"], "reason_code": row["reason_code"],
             "reason": REASONS.get(row["reason_code"], row["reason_code"]), "raw": _raw(row)}
            for row in quarantined
        ],
        "metrics": _metric_view(metrics),
    }


def _group_summary(conn, group) -> dict:
    members = repo.accepted_by_group(conn, group["group_id"])
    active = [leg for leg in repo.group_legs(conn, group["group_id"]) if leg["state"] == "ACTIVE"]
    return {
        "group_id": group["group_id"],
        "declaration_key": group["declaration_key"],
        "status": group["status"],
        "version": group["version"],
        "members": [row["record_id"] for row in members],
        "marketing_count": len(members),
        "leg_count": len(active),
        "conflict": conflict_evidence(members),
        "pending_decision": group["status"] == "PENDING",
    }


def _group_view(conn, group) -> dict:
    members = repo.accepted_by_group(conn, group["group_id"])
    conflict = conflict_evidence(members)
    legs = [leg for leg in repo.group_legs(conn, group["group_id"]) if leg["state"] == "ACTIVE"]
    merged = [leg for leg in legs if str(leg["leg_id"]).startswith("leg:grp:")]
    active_legs = merged if merged else legs
    refs = repo.refs_for_legs(conn, [leg["leg_id"] for leg in legs])
    return {
        "group_id": group["group_id"],
        "declaration_key": group["declaration_key"],
        "status": group["status"],
        "version": group["version"],
        "decision": group["decision"],
        "pending_decision": group["status"] == "PENDING",
        "members": [row["record_id"] for row in members],
        "member_sources": [
            {
                "record_id": row["record_id"],
                "batch_id": row["batch_id"],
                "line_no": row["line_no"],
                "marketing_carrier": row["marketing_carrier"],
                "marketing_flight_no": row["marketing_flight_no"],
                "operating_carrier": row["operating_carrier"],
                "operating_flight_no": row["operating_flight_no"],
                "departure_local": row["departure_local"],
                "arrival_local": row["arrival_local"],
                "departure_fold": row["departure_fold"],
                "arrival_fold": row["arrival_fold"],
                "raw": _raw(row),
            }
            for row in members
        ],
        "conflict": conflict,
        "merged_leg_id": merged[0]["leg_id"] if merged else None,
        "active_leg_ids": [leg["leg_id"] for leg in active_legs],
        "leg_count": len(active_legs),
        "marketing_count": len(members),
        "folded_marketing": max(len(members) - len(active_legs), 0),
        "chain_refs": refs,
        "representative_record_id": group["representative_record_id"],
        "source_record_id": group["source_record_id"],
        "decided_by": group["decided_by"],
        "decided_at": group["decided_at"],
        "note": group["note"],
    }


def _chain_view(conn, chain_id: str) -> dict:
    positions = repo.chain_positions(conn, chain_id)
    return {
        "ok": True,
        "chain_id": chain_id,
        "state": "COMPLETED",
        "record_ids": [row["record_id"] for row in positions],
        "refs": [
            {"position": row["position"], "leg_id": row["leg_id"], "state": row["state"],
             "reason_code": row["reason_code"]}
            for row in repo.chain_refs(conn, chain_id)
        ],
    }


def _operation_view(conn, op) -> dict:
    steps = [
        {"step_no": row["step_no"], "name": row["name"], "status": row["status"],
         "result": json.loads(row["result_json"]) if row["result_json"] else None}
        for row in repo.list_steps(conn, op["op_id"])
    ]
    return {
        "op_id": op["op_id"],
        "kind": op["kind"],
        "group_id": op["group_id"],
        "status": op["status"],
        "expected_version": op["expected_version"],
        "steps": steps,
        "completed_steps": [step["step_no"] for step in steps
                            if step["status"] == "COMPLETED"],
        "remaining_steps": len(COMPENSATION_STEPS) - len(steps),
        "payload": json.loads(op["payload_json"]),
        "created_at": op["created_at"],
        "updated_at": op["updated_at"],
    }


def _compensation_result(conn, op) -> dict:
    view = _operation_view(conn, op)
    group = repo.get_group(conn, op["group_id"])
    steps = {step["step_no"]: step["result"] for step in view["steps"]}
    return envelope(
        action="COMPENSATE",
        op=view,
        op_id=op["op_id"],
        compensation_status=op["status"],
        group=_group_view(conn, group) if group else None,
        version=group["version"] if group else None,
        batch_deltas=(steps.get(3) or {}).get("batch_deltas", []),
        reconciliation=(steps.get(3) or {}).get("reconciliation") or _reconcile(conn),
        reused=op["status"] in ("COMPENSATED", "SETTLED"),
    )


def _metric_view(row) -> dict:
    if row is None:
        return {"marketing": 0, "legs_owned": 0, "folded": 0, "absorbed": 0,
                "absorbing": 0, "pending": 0, "unsettled": 0}
    return {
        "marketing": row["marketing"],
        "legs_owned": row["legs_owned"],
        "folded": row["folded"],
        "absorbed": row["absorbed"],
        "absorbing": row["absorbing"],
        "pending": row["pending"],
        "unsettled": row["unsettled"],
    }


def _metrics_rows(conn) -> list[dict]:
    return [{"batch_id": row["batch_id"], **_metric_view(row)} for row in repo.list_metrics(conn)]


def _metric_deltas(before: list[dict], after: list[dict]) -> list[dict]:
    prior = {item["batch_id"]: item for item in before}
    fields = ("marketing", "legs_owned", "folded", "absorbed", "absorbing", "pending", "unsettled")
    deltas = []
    for item in after:
        old = prior.get(item["batch_id"], {})
        delta = {key: item[key] - old.get(key, 0) for key in fields}
        if any(delta.values()):
            deltas.append({"batch_id": item["batch_id"], **delta})
    return deltas


def _raw(row) -> dict:
    try:
        return json.loads(row["source_json"])
    except (TypeError, ValueError):
        return {"record_id": row["record_id"]}


# validation helpers --------------------------------------------------------

def _expected_version(payload: dict) -> int:
    value = payload.get("expected_version")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise DomainError("EXPECTED_VERSION_REQUIRED", "必须提供非负整数 expected_version", 409)
    return value


def _actor(payload: dict) -> str:
    actor = str(payload.get("actor") or payload.get("decided_by") or "operator").strip()
    return actor or "operator"


def _require_group(conn, group_id: str):
    group = repo.get_group(conn, group_id)
    if group is None:
        raise DomainError("NOT_FOUND", "共享组不存在", 404)
    return group


def _mid_compensation(conn, group_id: str) -> bool:
    return any(
        op["status"] in ("COMPENSATING", "INTERRUPTED")
        for op in repo.list_operations(conn, group_id)
    )


def _reject_unfinished_compensation(conn, group_id: str, exclude_op=None) -> None:
    for op in repo.list_operations(conn, group_id):
        if op["op_id"] == exclude_op:
            continue
        if op["status"] in ("COMPENSATING", "INTERRUPTED"):
            raise DomainError(
                "COMPENSATION_IN_PROGRESS", "该共享组有未完成的补偿，请用原 op_id 恢复",
                409, {"op_id": op["op_id"], "status": op["status"]},
            )


def _version_conflict(group) -> DomainError:
    return DomainError(
        "VERSION_CONFLICT", "版本已过期，请回读当前版本后重试", 409,
        {"current_version": group["version"], "status": group["status"]},
    )
