"""M7 共享航班：跨批次采信、归并裁决、撤销补偿与对账。"""
import pytest

import app.store as store
from app.errors import DomainError
from app.modules.m7_codeshare import repo, service


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATABASE_PATH", tmp_path / "codeshare.sqlite3")
    repo.init()
    yield
    for suffix in ("", "-wal", "-shm"):
        path = tmp_path / f"codeshare.sqlite3{suffix}"
        if path.exists():
            path.unlink()


def leg(carrier, no, dep="2026-11-16T09:00", arr="2026-11-16T10:30", operating=None,
        date="2026-11-16", origin="PVG", destination="JFK"):
    """operating=None 表示未声明共享；否则为 (承运人, 实际航班号)。"""
    row = {
        "marketing_carrier": carrier,
        "marketing_flight_no": no,
        "flight_date": date,
        "origin": origin,
        "destination": destination,
        "departure_local": dep,
        "arrival_local": arr,
    }
    if operating:
        row["operating_carrier"], row["operating_flight_no"] = operating
    return row


def find_by_status(batch, status):
    return [group for group in batch["groups"] if group["status"] == status]


def read_all():
    with repo.reading() as conn:
        return service.readback(conn)


# import / declaration ------------------------------------------------------

def test_declared_codeshare_counts_one_actual_leg(db):
    batch = service.import_batch({"note": "A", "rows": [
        leg("MU", "MU5101", operating=("UA", "UA857")),
        leg("UA", "UA857", operating=("UA", "UA857")),
        leg("AA", "AA1234", operating=("UA", "UA857")),
    ]})
    assert batch["accepted"] == 3 and batch["quarantined"] == 0
    group = batch["groups"][0]
    assert group["status"] == "MERGED" and group["pending_decision"] is False
    assert group["marketing_count"] == 3 and len(group["members"]) == 3
    assert batch["metrics"]["marketing"] == 3
    assert batch["metrics"]["legs_owned"] == 1
    # 同批次内的 2 条营销记录被归并吸收；跨批次吸收数为 0
    assert batch["metrics"]["folded"] == 2
    assert batch["metrics"]["absorbing"] == 0
    assert read_all()["reconciliation"]["balanced"] is True


def test_identical_times_without_declaration_do_not_merge(db):
    batch = service.import_batch({"note": "same-time", "rows": [
        leg("MU", "MU5101"),
        leg("AA", "AA1234"),
        leg("UA", "UA857"),
    ]})
    assert batch["groups"] == []
    assert batch["metrics"]["legs_owned"] == 3
    for group in batch["groups"]:
        assert group["declaration_key"] is None
    reconciliation = read_all()["reconciliation"]
    assert reconciliation["actual_legs"] == 3 and reconciliation["balanced"]


def test_different_operating_flights_are_separate_when_times_match(db):
    batch = service.import_batch({"note": "diff-operating", "rows": [
        leg("MU", "MU5101", operating=("UA", "UA857")),
        leg("MU", "MU5200", operating=("DL", "DL200")),
    ]})
    assert len(batch["groups"]) == 2
    assert batch["metrics"]["legs_owned"] == 2


def test_same_marketing_key_across_batches_rejected(db):
    service.import_batch({"note": "A", "rows": [leg("MU", "MU5101")]})
    second = service.import_batch({"note": "B", "rows": [leg("MU", "MU5101")]})
    assert second["accepted"] == 0 and second["quarantined"] == 1
    assert second["evidence"][0]["reason_code"] == "DUPLICATE_KEY"
    assert second["evidence"][0]["line_no"] == 2


def test_cross_batch_conflict_quarantined_with_evidence(db):
    service.import_batch({"note": "A", "rows": [leg("MU", "MU5101")]})
    second = service.import_batch({"note": "B", "rows": [
        leg("MU", "MU5101", arr="2026-11-16T11:00"),
    ]})
    assert second["quarantined"] == 1
    assert second["evidence"][0]["reason_code"] == "VALUE_CONFLICT"
    assert second["evidence"][0]["raw"]["arrival_local"] == "2026-11-16T11:00"


def test_import_is_idempotent(db):
    payload = {"note": "A", "rows": [leg("MU", "MU5101", operating=("UA", "UA857"))]}
    first = service.import_batch(payload)
    second = service.import_batch(payload)
    assert first["created"] is True and second["created"] is False
    assert second["batch_id"] == first["batch_id"]
    with repo.reading() as conn:
        assert len(repo.list_batches(conn)) == 1


def test_missing_operating_pair_is_quarantined(db):
    row = leg("MU", "MU5101")
    row["operating_carrier"] = "UA"
    batch = service.import_batch({"note": "half-declaration", "rows": [row]})
    assert batch["accepted"] == 0
    assert batch["evidence"][0]["reason_code"] == "BAD_DECLARATION"


# declaration conflict ------------------------------------------------------

def test_conflicting_declaration_is_pending_not_merged(db):
    batch = service.import_batch({"note": "conflict", "rows": [
        leg("MU", "MU5101", operating=("UA", "UA857")),
        leg("UA", "UA857", dep="2026-11-16T09:05", arr="2026-11-16T10:40",
            operating=("UA", "UA857")),
    ]})
    group = batch["groups"][0]
    assert group["status"] == "PENDING" and group["pending_decision"] is True
    assert group["conflict"]["fields"] == ["departure_local", "arrival_local"]
    assert len(group["conflict"]["sources"]) == 2
    assert batch["metrics"]["legs_owned"] == 0
    assert batch["metrics"]["pending"] == 2
    assert read_all()["pending_decision"][0]["group_id"] == group["group_id"]


def test_merge_requires_source_selection_when_conflict(db):
    batch = service.import_batch({"note": "conflict", "rows": [
        leg("MU", "MU5101", operating=("UA", "UA857")),
        leg("UA", "UA857", dep="2026-11-16T09:05", arr="2026-11-16T10:40",
            operating=("UA", "UA857")),
    ]})
    group = batch["groups"][0]
    with pytest.raises(DomainError) as error:
        service.adjudicate(group["group_id"], {"decision": "MERGE", "expected_version": 0})
    assert error.value.code == "UNRESOLVED_CONFLICT"
    assert error.value.status == 409
    # 仍然停在待裁决，不产生实际航段
    with repo.reading() as conn:
        assert repo.get_group(conn, group["group_id"])["status"] == "PENDING"
        assert repo.active_legs(conn) == []


def test_merge_with_source_records_that_source(db):
    batch = service.import_batch({"note": "conflict", "rows": [
        leg("MU", "MU5101", operating=("UA", "UA857")),
        leg("UA", "UA857", dep="2026-11-16T09:05", arr="2026-11-16T10:40",
            operating=("UA", "UA857")),
    ]})
    group = batch["groups"][0]
    chosen = group["members"][1]
    result = service.adjudicate(group["group_id"], {
        "decision": "MERGE", "expected_version": 0, "source_record_id": chosen,
    })
    assert result["group"]["status"] == "MERGED"
    assert result["merged_leg_id"].startswith("leg:grp:")
    with repo.reading() as conn:
        merged = repo.get_leg(conn, result["merged_leg_id"])
        assert merged["departure_local"] == "2026-11-16T09:05"
        assert merged["owner_record_id"] == chosen


def test_source_record_must_belong_to_group(db):
    batch = service.import_batch({"note": "A", "rows": [leg("MU", "MU5101",
                                                            operating=("UA", "UA857"))]})
    group = batch["groups"][0]
    with pytest.raises(DomainError) as error:
        service.adjudicate(group["group_id"], {
            "decision": "MERGE", "expected_version": 0, "source_record_id": "nope:99",
        })
    assert error.value.code == "BAD_SOURCE"


def test_merge_keeps_marketing_evidence_after_fold(db):
    batch = service.import_batch({"note": "A", "rows": [
        leg("MU", "MU5101", operating=("UA", "UA857")),
        leg("AA", "AA1234", operating=("UA", "UA857")),
    ]})
    group = batch["groups"][0]
    read = read_all()["merged"][0]
    assert set(read["members"]) == set(group["members"])
    assert {item["marketing_carrier"] for item in read["member_sources"]} == {"MU", "AA"}
    assert len(read["active_leg_ids"]) == 1


# versioning ----------------------------------------------------------------

def test_stale_adjudication_rejected(db):
    batch = service.import_batch({"note": "A", "rows": [
        leg("MU", "MU5101", operating=("UA", "UA857")),
        leg("AA", "AA1234", operating=("UA", "UA857")),
    ]})
    group = batch["groups"][0]
    first = service.adjudicate(group["group_id"], {"decision": "SEPARATE", "expected_version": 0})
    assert first["version"] == 1
    with pytest.raises(DomainError) as error:
        service.adjudicate(group["group_id"], {"decision": "MERGE", "expected_version": 0})
    assert error.value.code == "VERSION_CONFLICT"
    assert error.value.details["current_version"] == 1


def test_expected_version_required(db):
    batch = service.import_batch({"note": "A", "rows": [leg("MU", "MU5101",
                                                            operating=("UA", "UA857"))]})
    with pytest.raises(DomainError) as error:
        service.adjudicate(batch["groups"][0]["group_id"], {"decision": "SEPARATE"})
    assert error.value.code == "EXPECTED_VERSION_REQUIRED"


def test_no_effect_rejected(db):
    batch = service.import_batch({"note": "A", "rows": [
        leg("MU", "MU5101", operating=("UA", "UA857")),
        leg("AA", "AA1234", operating=("UA", "UA857")),
    ]})
    group = batch["groups"][0]
    with pytest.raises(DomainError) as error:
        service.adjudicate(group["group_id"], {"decision": "MERGE", "expected_version": 0})
    assert error.value.code == "NO_EFFECT"


# chains --------------------------------------------------------------------

def _merged_with_chain():
    batch = service.import_batch({"note": "A", "rows": [
        leg("MU", "MU5101", operating=("UA", "UA857")),
        leg("AA", "AA1234", operating=("UA", "UA857")),
    ]})
    onward = service.import_batch({"note": "B", "rows": [
        leg("UA", "UA900", dep="2026-11-16T12:00", arr="2026-11-16T14:00", operating=None,
            origin="JFK", destination="LHR"),
    ]})
    with repo.reading() as conn:
        onward_record = conn.execute(
            "SELECT record_id FROM cs_records WHERE batch_id=?", (onward["batch_id"],)
        ).fetchone()["record_id"]
    group = batch["groups"][0]
    chain = service.create_chain({
        "record_ids": [group["members"][0], onward_record],
        "chain_id": "C-DEMO",
    })
    return group, chain, onward_record


def test_chain_references_actual_leg_not_marketing_records(db):
    group, chain, _ = _merged_with_chain()
    assert chain["refs"][0]["leg_id"] == f"leg:grp:{group['group_id']}:v0"
    assert chain["refs"][0]["state"] == "ACTIVE"
    assert chain["refs"][1]["leg_id"].startswith("leg:B-")


def test_compensation_splits_legs_and_repairs_chain(db):
    group, _, _ = _merged_with_chain()
    result = service.compensate(group["group_id"], {"expected_version": 0, "op_id": "op-1"})
    assert result["compensation_status"] == "COMPENSATED"
    assert result["group"]["status"] == "SEPARATE"
    assert result["group"]["merged_leg_id"] is None
    assert result["group"]["leg_count"] == 2
    refs = result["group"]["chain_refs"]
    assert refs and refs[0]["state"] == "ACTIVE"
    assert refs[0]["leg_id"].startswith("leg:B-")
    assert not refs[0]["leg_id"].startswith("leg:grp:")
    read = read_all()
    assert read["reconciliation"]["balanced"] is True
    assert read["reconciliation"]["merged_legs"] == 0


def test_compensation_restores_split_counts_per_batch(db):
    group, _, _ = _merged_with_chain()
    result = service.compensate(group["group_id"], {"expected_version": 0, "op_id": "op-1"})
    deltas = {item["batch_id"]: item for item in result["batch_deltas"]}
    # 归并时被吸收的那条营销记录在撤销后成为独立航段
    folded = [item for item in deltas.values() if item["folded"]]
    assert folded and folded[0]["folded"] == -1
    reconciliation = result["reconciliation"]
    assert all(batch["balanced"] for batch in reconciliation["batches"])
    assert reconciliation["expected_folded"] == 0


def test_compensation_retry_does_not_double_count(db):
    group, _, _ = _merged_with_chain()
    first = service.compensate(group["group_id"], {"expected_version": 0, "op_id": "op-1"})
    second = service.compensate(group["group_id"], {"expected_version": 0, "op_id": "op-1"})
    assert second["reused"] is True
    assert second["compensation_status"] == "COMPENSATED"
    assert second["reconciliation"] == first["reconciliation"]
    with repo.reading() as conn:
        assert len(repo.list_operations(conn, group["group_id"])) == 1
        assert repo.get_group(conn, group["group_id"])["version"] == 1


def test_compensation_requires_op_id(db):
    group, _, _ = _merged_with_chain()
    with pytest.raises(DomainError) as error:
        service.compensate(group["group_id"], {"expected_version": 0})
    assert error.value.code == "OP_ID_REQUIRED"


def test_compensation_stale_version_rejected(db):
    group, _, _ = _merged_with_chain()
    with pytest.raises(DomainError) as error:
        service.compensate(group["group_id"], {"expected_version": 7, "op_id": "op-x"})
    assert error.value.code == "VERSION_CONFLICT"


def test_compensation_not_possible_when_already_separate(db):
    group, _, _ = _merged_with_chain()
    service.compensate(group["group_id"], {"expected_version": 0, "op_id": "op-1"})
    with pytest.raises(DomainError) as error:
        service.compensate(group["group_id"], {"expected_version": 1, "op_id": "op-2"})
    assert error.value.code == "NOT_COMPENSABLE"


def test_compensation_interrupt_then_resume(db):
    group, _, _ = _merged_with_chain()
    with pytest.raises(DomainError) as interrupted:
        service.compensate(group["group_id"], {
            "expected_version": 0, "op_id": "op-int", "interrupt_after_step": 2,
        })
    assert interrupted.value.code == "COMPENSATION_INTERRUPTED"
    assert interrupted.value.details["resume_from"] == 3

    read = read_all()
    assert read["compensating"][0]["op_id"] == "op-int"
    assert read["compensating"][0]["status"] == "INTERRUPTED"

    resumed = service.compensate(group["group_id"], {"expected_version": 0, "op_id": "op-int"})
    assert resumed["compensation_status"] == "COMPENSATED"
    assert [step["step_no"] for step in resumed["op"]["steps"]] == [1, 2, 3, 4]
    assert resumed["group"]["status"] == "SEPARATE"
    assert read_all()["reconciliation"]["balanced"] is True


def test_compensation_interrupt_blocks_new_adjudication(db):
    group, _, _ = _merged_with_chain()
    with pytest.raises(DomainError):
        service.compensate(group["group_id"], {
            "expected_version": 0, "op_id": "op-int", "interrupt_after_step": 1,
        })
    with pytest.raises(DomainError) as error:
        service.adjudicate(group["group_id"], {"decision": "SEPARATE", "expected_version": 0})
    assert error.value.code == "COMPENSATION_IN_PROGRESS"


def test_compensation_interrupt_visible_as_compensating_group(db):
    group, _, _ = _merged_with_chain()
    with pytest.raises(DomainError):
        service.compensate(group["group_id"], {
            "expected_version": 0, "op_id": "op-int", "interrupt_after_step": 1,
        })
    read = read_all()
    assert read["compensating"][0]["status"] == "INTERRUPTED"
    assert read["compensating_groups"][0]["group_id"] == group["group_id"]
    assert read["compensating_groups"][0]["status"] == "COMPENSATING"


def test_source_arriving_mid_compensation_settles_on_resume(db):
    batch = service.import_batch({"note": "A", "rows": [
        leg("MU", "MU5101", operating=("UA", "UA857")),
        leg("UA", "UA857", operating=("UA", "UA857")),
    ]})
    group_id = batch["groups"][0]["group_id"]
    with pytest.raises(DomainError):
        service.compensate(group_id, {
            "expected_version": 0, "op_id": "op-mid", "interrupt_after_step": 1,
        })
    late = service.import_batch({"note": "B", "rows": [
        leg("AA", "AA1234", operating=("UA", "UA857")),
    ]})
    # 补偿中的组不因新来源恢复归并
    assert late["groups"][0]["status"] == "COMPENSATING"
    resumed = service.compensate(group_id, {"expected_version": 0, "op_id": "op-mid"})
    assert resumed["compensation_status"] == "COMPENSATED"
    assert resumed["group"]["status"] == "SEPARATE"
    assert resumed["group"]["leg_count"] == 3
    assert read_all()["reconciliation"]["balanced"] is True


def test_remerge_after_compensation_reuses_group(db):
    group, _, _ = _merged_with_chain()
    service.compensate(group["group_id"], {"expected_version": 0, "op_id": "op-1"})
    merged = service.adjudicate(group["group_id"], {"decision": "MERGE", "expected_version": 1})
    assert merged["group"]["status"] == "MERGED"
    assert merged["version"] == 2
    assert merged["group"]["leg_count"] == 1
    read = read_all()
    assert read["reconciliation"]["balanced"] is True
    assert read["reconciliation"]["folded_marketing"] == 1


def test_new_conflicting_source_sets_group_pending(db):
    service.import_batch({"note": "A", "rows": [
        leg("MU", "MU5101", operating=("UA", "UA857")),
        leg("UA", "UA857", operating=("UA", "UA857")),
    ]})
    third = service.import_batch({"note": "C", "rows": [
        leg("AA", "AA9999", dep="2026-11-16T09:30", arr="2026-11-16T11:00",
            operating=("UA", "UA857")),
    ]})
    group = third["groups"][0]
    assert group["status"] == "PENDING"
    assert group["conflict"] is not None
    assert third["metrics"]["legs_owned"] == 0
    assert len(group["members"]) == 3
    # 待裁决期间不占用实际航段，也不丢任何营销来源
    read = read_all()
    assert read["pending_decision"][0]["marketing_count"] == 3


def test_member_change_advances_version_and_invalidates_old_decision(db):
    batch = service.import_batch({"note": "A", "rows": [
        leg("MU", "MU5101", operating=("UA", "UA857")),
    ]})
    group_id = batch["groups"][0]["group_id"]
    assert batch["groups"][0]["version"] == 0
    service.import_batch({"note": "B", "rows": [
        leg("AA", "AA1234", operating=("UA", "UA857")),
    ]})
    with repo.reading() as conn:
        assert repo.get_group(conn, group_id)["version"] == 1
    with pytest.raises(DomainError) as error:
        service.adjudicate(group_id, {"decision": "SEPARATE", "expected_version": 0})
    assert error.value.code == "VERSION_CONFLICT"


def test_manual_separate_stays_separate_on_new_source(db):
    first = service.import_batch({"note": "A", "rows": [
        leg("MU", "MU5101", operating=("UA", "UA857")),
        leg("UA", "UA857", operating=("UA", "UA857")),
    ]})
    group_id = first["groups"][0]["group_id"]
    service.adjudicate(group_id, {"decision": "SEPARATE", "expected_version": 0})
    second = service.import_batch({"note": "B", "rows": [
        leg("AA", "AA1234", operating=("UA", "UA857")),
    ]})
    group = second["groups"][0]
    assert group["status"] == "SEPARATE"
    assert group["leg_count"] == 3
    assert read_all()["reconciliation"]["balanced"] is True


def test_compensated_group_stays_separate_on_new_source(db):
    group, _, _ = _merged_with_chain()
    service.compensate(group["group_id"], {"expected_version": 0, "op_id": "op-1"})
    added = service.import_batch({"note": "C", "rows": [
        leg("AA", "AA7777", operating=("UA", "UA857")),
    ]})
    assert added["groups"][0]["status"] == "SEPARATE"
    assert added["groups"][0]["leg_count"] == 3
    reconciliation = read_all()["reconciliation"]
    # 2 条拆回航段 + 1 条后续航段 + 新来源自己的航段
    assert reconciliation["actual_legs"] == 4 and reconciliation["balanced"] is True


def test_new_matching_source_after_merge_stays_merged(db):
    first = service.import_batch({"note": "A", "rows": [
        leg("MU", "MU5101", operating=("UA", "UA857")),
        leg("UA", "UA857", operating=("UA", "UA857")),
    ]})
    group = first["groups"][0]
    assert group["status"] == "MERGED"
    second = service.import_batch({"note": "B", "rows": [
        leg("AA", "AA1234", operating=("UA", "UA857")),
    ]})
    group = second["groups"][0]
    assert group["status"] == "MERGED"
    assert group["marketing_count"] == 3 and group["leg_count"] == 1


# readback ------------------------------------------------------------------

def test_readback_states_and_reconciliation(db):
    conflicting = service.import_batch({"note": "conflict", "rows": [
        leg("MU", "MU5101", operating=("UA", "UA857")),
        leg("UA", "UA857", dep="2026-11-16T09:05", arr="2026-11-16T10:40",
            operating=("UA", "UA857")),
    ]})
    group = conflicting["groups"][0]
    service.adjudicate(group["group_id"], {
        "decision": "MERGE", "expected_version": 0, "source_record_id": group["members"][1],
    })
    read = read_all()
    assert read["groups_by_status"] == {"MERGED": 1}
    assert read["merged"][0]["version"] == 1
    assert read["pending_decision"] == []
    reconciliation = read["reconciliation"]
    assert reconciliation["marketing_records"] == 2
    assert reconciliation["actual_legs"] == 1
    assert reconciliation["folded_marketing"] == 1
    assert reconciliation["balanced"] is True


def test_batch_view_reconciles_marketing_vs_legs(db):
    batch = service.import_batch({"note": "A", "rows": [
        leg("MU", "MU5101", operating=("UA", "UA857")),
        leg("UA", "UA857", operating=("UA", "UA857")),
    ]})
    metrics = batch["metrics"]
    # 同一批次内部的归并：marketing = legs_owned + folded
    assert metrics["marketing"] == metrics["legs_owned"] + metrics["folded"]
    assert metrics["folded"] == 1
    view = service._batch_view(repo.connect(), batch["batch_id"])
    assert view["input"] == view["accepted"] + view["quarantined"]


def test_cross_batch_fold_keeps_each_batch_balanced(db):
    operating = service.import_batch({"note": "own", "rows": [
        leg("UA", "UA857", operating=("UA", "UA857")),
    ]})
    marketing = service.import_batch({"note": "sale", "rows": [
        leg("MU", "MU5101", operating=("UA", "UA857")),
        leg("AA", "AA1234", operating=("UA", "UA857")),
    ]})
    # 后到批次的两条营销记录被先到批次的实际航段吸收
    assert marketing["metrics"]["absorbed"] == 2
    with repo.reading() as conn:
        operating_now = service._batch_view(conn, operating["batch_id"])["metrics"]
    assert operating_now["absorbing"] == 2
    reconciliation = read_all()["reconciliation"]
    assert reconciliation["marketing_records"] == 3
    assert reconciliation["actual_legs"] == 1
    assert reconciliation["balanced"] is True
    assert all(batch["balanced"] for batch in reconciliation["batches"])

    # 撤销后两个批次都各自持有自己的航段
    compensated = service.compensate(marketing["groups"][0]["group_id"],
                                     {"expected_version": marketing["groups"][0]["version"],
                                      "op_id": "op-cross"})
    after = compensated["reconciliation"]
    assert after["actual_legs"] == 3 and after["balanced"] is True
    assert all(batch["balanced"] for batch in after["batches"])


def test_pending_conflict_still_reconciles(db):
    service.import_batch({"note": "conflict", "rows": [
        leg("MU", "MU5101", operating=("UA", "UA857")),
        leg("UA", "UA857", dep="2026-11-16T09:05", arr="2026-11-16T10:40",
            operating=("UA", "UA857")),
    ]})
    reconciliation = read_all()["reconciliation"]
    assert reconciliation["pending_records"] == 2
    assert reconciliation["unsettled_records"] == 2
    assert reconciliation["unaccounted_records"] == []
    assert reconciliation["balanced"] is True
    assert reconciliation["batches"][0]["balanced"] is True


def test_quarantine_keeps_line_numbers_and_raw_values(db):
    batch = service.import_batch({"note": "dirty", "rows": [
        leg("MU", "MU5101"),
        {**leg("UA", "UA857"), "departure_local": ""},
        leg("MU", "MU5101"),
    ]})
    assert batch["accepted"] == 1 and batch["quarantined"] == 2
    reasons = {item["line_no"]: item["reason_code"] for item in batch["evidence"]}
    assert reasons == {3: "EMPTY_VALUE", 4: "DUPLICATE_KEY"}
    assert batch["evidence"][0]["raw"]["departure_local"] == ""
