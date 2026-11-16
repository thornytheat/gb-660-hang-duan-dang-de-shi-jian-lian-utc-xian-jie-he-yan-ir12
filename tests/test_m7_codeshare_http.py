from fastapi.testclient import TestClient

import app.store as store
from app.main import app
from app.modules.m7_codeshare import repo


def row(carrier, no, operating, dep="2026-11-16T09:00", arr="2026-11-16T10:30"):
    return {
        "marketing_carrier": carrier, "marketing_flight_no": no,
        "flight_date": "2026-11-16", "origin": "PVG", "destination": "JFK",
        "departure_local": dep, "arrival_local": arr,
        "operating_carrier": operating[0], "operating_flight_no": operating[1],
    }


def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(store, "DATABASE_PATH", tmp_path / "http.sqlite3")
    repo.init()
    return TestClient(app)


def test_codeshare_page_renders(tmp_path, monkeypatch):
    http = client(tmp_path, monkeypatch)
    response = http.get("/codeshare")
    assert response.status_code == 200 and "panel" in response.text


def test_import_adjudicate_compensate_roundtrip(tmp_path, monkeypatch):
    http = client(tmp_path, monkeypatch)
    imported = http.post("/api/codeshare/import", json={"note": "A", "rows": [
        row("MU", "MU5101", ("UA", "UA857")),
        row("AA", "AA1234", ("UA", "UA857")),
    ]}).json()
    group_id = imported["groups"][0]["group_id"]
    assert imported["groups"][0]["status"] == "MERGED"

    compensated = http.post(f"/api/codeshare/groups/{group_id}/compensate",
                            json={"expected_version": 0, "op_id": "op-http"})
    assert compensated.status_code == 200
    body = compensated.json()
    assert body["group"]["status"] == "SEPARATE"
    assert body["reconciliation"]["balanced"] is True

    readback = http.get("/api/codeshare/groups").json()
    assert readback["groups_by_status"] == {"SEPARATE": 1}
    assert readback["completed_compensations"][0]["status"] == "COMPENSATED"


def test_conflict_error_carries_evidence(tmp_path, monkeypatch):
    http = client(tmp_path, monkeypatch)
    imported = http.post("/api/codeshare/import", json={"note": "C", "rows": [
        row("MU", "MU5101", ("UA", "UA857")),
        row("UA", "UA857", ("UA", "UA857"), dep="2026-11-16T09:15"),
    ]}).json()
    group_id = imported["groups"][0]["group_id"]
    response = http.post(f"/api/codeshare/groups/{group_id}/adjudicate",
                         json={"decision": "MERGE", "expected_version": 0})
    assert response.status_code == 409
    assert response.json()["code"] == "UNRESOLVED_CONFLICT"


def test_batch_readback_reports_quarantine(tmp_path, monkeypatch):
    http = client(tmp_path, monkeypatch)
    imported = http.post("/api/codeshare/import", json={"note": "D", "rows": [
        row("MU", "MU5101", ("UA", "UA857")),
        {**row("UA", "UA857", ("UA", "UA857")), "arrival_local": ""},
    ]}).json()
    view = http.get(f"/api/codeshare/batches/{imported['batch_id']}").json()
    assert view["accepted"] == 1 and view["quarantined"] == 1
    assert view["evidence"][0]["line_no"] == 3
    assert view["evidence"][0]["reason_code"] == "EMPTY_VALUE"


def test_unknown_group_returns_404(tmp_path, monkeypatch):
    http = client(tmp_path, monkeypatch)
    response = http.post("/api/codeshare/groups/missing/adjudicate",
                         json={"decision": "MERGE", "expected_version": 0})
    assert response.status_code == 404 and response.json()["code"] == "NOT_FOUND"
