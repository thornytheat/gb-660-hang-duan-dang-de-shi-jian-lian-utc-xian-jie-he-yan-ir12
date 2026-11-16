"""共享航班批次的持久化层。

连接每次打开；裁决与补偿使用 BEGIN IMMEDIATE 串行化版本检查。
时间戳只用于审计，业务判定依赖记录内容与显式版本。
"""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import app.store as store

SCHEMA = """
CREATE TABLE IF NOT EXISTS cs_batches (
    batch_id TEXT PRIMARY KEY,
    input_sha256 TEXT NOT NULL,
    accepted INTEGER NOT NULL,
    quarantined INTEGER NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cs_records (
    record_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    marketing_carrier TEXT,
    marketing_flight_no TEXT,
    flight_date TEXT,
    origin TEXT,
    destination TEXT,
    departure_local TEXT,
    arrival_local TEXT,
    departure_fold INTEGER,
    arrival_fold INTEGER,
    operating_carrier TEXT,
    operating_flight_no TEXT,
    declaration_key TEXT,
    group_id TEXT,
    state TEXT NOT NULL,
    reason_code TEXT,
    source_json TEXT NOT NULL,
    UNIQUE(batch_id, line_no)
);
CREATE TABLE IF NOT EXISTS cs_groups (
    group_id TEXT PRIMARY KEY,
    declaration_key TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL,
    representative_record_id TEXT,
    merged_leg_id TEXT,
    decision TEXT,
    source_record_id TEXT,
    members_json TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS cs_legs (
    leg_id TEXT PRIMARY KEY,
    group_id TEXT,
    owner_record_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    flight_date TEXT NOT NULL,
    departure_local TEXT NOT NULL,
    arrival_local TEXT NOT NULL,
    departure_fold INTEGER,
    arrival_fold INTEGER,
    operating_carrier TEXT,
    operating_flight_no TEXT,
    state TEXT NOT NULL,
    created_by TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cs_leg_members (
    leg_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    PRIMARY KEY (leg_id, record_id)
);
CREATE TABLE IF NOT EXISTS cs_chains (
    chain_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cs_chain_positions (
    chain_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    record_id TEXT NOT NULL,
    PRIMARY KEY (chain_id, position)
);
CREATE TABLE IF NOT EXISTS cs_chain_refs (
    chain_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    leg_id TEXT,
    state TEXT NOT NULL,
    reason_code TEXT,
    PRIMARY KEY (chain_id, position)
);
CREATE TABLE IF NOT EXISTS cs_operations (
    op_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    group_id TEXT NOT NULL,
    status TEXT NOT NULL,
    expected_version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cs_op_steps (
    op_id TEXT NOT NULL,
    step_no INTEGER NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (op_id, step_no)
);
CREATE TABLE IF NOT EXISTS cs_batch_metrics (
    batch_id TEXT PRIMARY KEY,
    marketing INTEGER NOT NULL,
    legs_owned INTEGER NOT NULL,
    folded INTEGER NOT NULL,
    absorbed INTEGER NOT NULL,
    absorbing INTEGER NOT NULL,
    pending INTEGER NOT NULL,
    unsettled INTEGER NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    path = Path(store.DATABASE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")
    finally:
        conn.close()


@contextmanager
def transaction():
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


@contextmanager
def reading():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


# batches -------------------------------------------------------------------

def get_batch(conn, batch_id: str):
    return conn.execute("SELECT * FROM cs_batches WHERE batch_id=?", (batch_id,)).fetchone()


def insert_batch(conn, batch_id, sha, accepted, quarantined, note, created_at):
    conn.execute(
        "INSERT OR IGNORE INTO cs_batches(batch_id,input_sha256,accepted,quarantined,note,created_at)"
        " VALUES(?,?,?,?,?,?)",
        (batch_id, sha, accepted, quarantined, note, created_at),
    )


def list_batches(conn):
    return conn.execute("SELECT * FROM cs_batches ORDER BY created_at, batch_id").fetchall()


# records -------------------------------------------------------------------

RECORD_FIELDS = (
    "record_id", "batch_id", "line_no", "marketing_carrier", "marketing_flight_no",
    "flight_date", "origin", "destination", "departure_local", "arrival_local",
    "departure_fold", "arrival_fold", "operating_carrier", "operating_flight_no",
    "declaration_key", "group_id", "state", "reason_code", "source_json",
)


def insert_record(conn, row: dict):
    conn.execute(
        "INSERT OR REPLACE INTO cs_records(" + ",".join(RECORD_FIELDS) + ") VALUES("
        + ",".join("?" for _ in RECORD_FIELDS) + ")",
        tuple(row.get(field) for field in RECORD_FIELDS),
    )


def get_record(conn, record_id):
    return conn.execute("SELECT * FROM cs_records WHERE record_id=?", (record_id,)).fetchone()


def accepted_by_declaration(conn, declaration_key: str):
    return conn.execute(
        "SELECT * FROM cs_records WHERE state='ACCEPTED' AND declaration_key=?"
        " ORDER BY batch_id, line_no",
        (declaration_key,),
    ).fetchall()


def accepted_by_group(conn, group_id: str):
    return conn.execute(
        "SELECT * FROM cs_records WHERE state='ACCEPTED' AND group_id=?"
        " ORDER BY batch_id, line_no",
        (group_id,),
    ).fetchall()


def set_records_group(conn, record_ids, group_id):
    for record_id in record_ids:
        conn.execute("UPDATE cs_records SET group_id=? WHERE record_id=?", (group_id, record_id))


def quarantine(conn, batch_id, line_no, reason_code, source):
    conn.execute(
        "INSERT OR REPLACE INTO cs_records(record_id,batch_id,line_no,state,reason_code,source_json)"
        " VALUES(?,?,?,'QUARANTINED',?,?)",
        (f"{batch_id}:{line_no}", batch_id, line_no, reason_code,
         json.dumps(source, ensure_ascii=False, sort_keys=True, default=str)),
    )


# groups --------------------------------------------------------------------

def get_group(conn, group_id: str):
    return conn.execute("SELECT * FROM cs_groups WHERE group_id=?", (group_id,)).fetchone()


def list_groups(conn):
    return conn.execute("SELECT * FROM cs_groups ORDER BY group_id").fetchall()


def insert_group(conn, group: dict):
    conn.execute(
        "INSERT OR IGNORE INTO cs_groups(group_id,declaration_key,status,version,"
        "representative_record_id,merged_leg_id,decision,source_record_id,members_json,"
        "decided_by,decided_at,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            group["group_id"], group["declaration_key"], group["status"], group["version"],
            group.get("representative_record_id"), group.get("merged_leg_id"),
            group.get("decision"), group.get("source_record_id"),
            json.dumps(sorted(group.get("members", [])), ensure_ascii=False),
            group.get("decided_by"), group.get("decided_at"), group.get("note"),
        ),
    )


GROUP_COLUMNS = (
    "status", "version", "representative_record_id", "merged_leg_id", "decision",
    "source_record_id", "decided_by", "decided_at", "note",
)


def update_group(conn, group_id: str, **fields):
    members = fields.pop("members", None)
    assignments, params = [], []
    for key in GROUP_COLUMNS:
        if key in fields:
            assignments.append(f"{key}=?")
            params.append(fields[key])
    if members is not None:
        assignments.append("members_json=?")
        params.append(json.dumps(sorted(members), ensure_ascii=False))
    if not assignments:
        return
    params.append(group_id)
    conn.execute(f"UPDATE cs_groups SET {','.join(assignments)} WHERE group_id=?", params)


def group_members(group_row) -> list[str]:
    return json.loads(group_row["members_json"] or "[]")


# legs ----------------------------------------------------------------------

LEG_COLUMNS = (
    "leg_id", "group_id", "owner_record_id", "batch_id", "origin", "destination",
    "flight_date", "departure_local", "arrival_local", "departure_fold", "arrival_fold",
    "operating_carrier", "operating_flight_no", "state", "created_by", "created_at",
)


def upsert_leg(conn, leg: dict) -> str:
    conn.execute(
        "INSERT OR IGNORE INTO cs_legs(" + ",".join(LEG_COLUMNS) + ") VALUES("
        + ",".join("?" for _ in LEG_COLUMNS) + ")",
        tuple(leg.get(field) for field in LEG_COLUMNS),
    )
    return leg["leg_id"]


def own_leg_id(record_id: str) -> str:
    return f"leg:{record_id}"


def merged_leg_id(group_id: str, version: int) -> str:
    return f"leg:grp:{group_id}:v{version}"


def get_leg(conn, leg_id: str):
    return conn.execute("SELECT * FROM cs_legs WHERE leg_id=?", (leg_id,)).fetchone()


def list_legs(conn):
    return conn.execute("SELECT * FROM cs_legs ORDER BY created_at, leg_id").fetchall()


def active_legs(conn):
    return conn.execute("SELECT * FROM cs_legs WHERE state='ACTIVE' ORDER BY leg_id").fetchall()


def set_leg_members(conn, leg_id: str, record_ids) -> None:
    conn.execute("DELETE FROM cs_leg_members WHERE leg_id=?", (leg_id,))
    for record_id in sorted(record_ids):
        conn.execute("INSERT INTO cs_leg_members(leg_id,record_id) VALUES(?,?)", (leg_id, record_id))


def leg_members(conn, leg_id: str) -> list[str]:
    return [row[0] for row in conn.execute(
        "SELECT record_id FROM cs_leg_members WHERE leg_id=? ORDER BY record_id", (leg_id,)
    )]


def set_leg_active(conn, leg_id: str, active: bool) -> bool:
    row = get_leg(conn, leg_id)
    if row is None:
        return False
    target = "ACTIVE" if active else "RETIRED"
    if row["state"] == target:
        return False
    conn.execute("UPDATE cs_legs SET state=? WHERE leg_id=?", (target, leg_id))
    return True


def group_legs(conn, group_id: str):
    return conn.execute(
        "SELECT * FROM cs_legs WHERE group_id=? ORDER BY leg_id", (group_id,)
    ).fetchall()


def active_legs_for_group(conn, group_id: str):
    return [row[0] for row in conn.execute(
        "SELECT leg_id FROM cs_legs WHERE group_id=? AND state='ACTIVE' ORDER BY leg_id",
        (group_id,),
    )]


def settled_map(conn) -> dict[str, str]:
    """每个已采信记录当前归属的实际航段（只取活跃航段）。"""
    rows = conn.execute(
        "SELECT m.record_id,m.leg_id FROM cs_leg_members m"
        " JOIN cs_legs l ON l.leg_id=m.leg_id WHERE l.state='ACTIVE'"
    ).fetchall()
    return {row["record_id"]: row["leg_id"] for row in rows}


def double_settled_records(conn) -> list[str]:
    """同一记录被多个活跃航段同时引用即为重复计入，必须为零。"""
    rows = conn.execute(
        "SELECT m.record_id,COUNT(*) AS hits FROM cs_leg_members m"
        " JOIN cs_legs l ON l.leg_id=m.leg_id WHERE l.state='ACTIVE'"
        " GROUP BY m.record_id HAVING hits > 1 ORDER BY m.record_id"
    ).fetchall()
    return [row[0] for row in rows]


# chains --------------------------------------------------------------------

def list_chain_ids(conn) -> list[str]:
    return [row[0] for row in conn.execute("SELECT chain_id FROM cs_chains ORDER BY chain_id")]


def get_chain(conn, chain_id: str):
    return conn.execute("SELECT * FROM cs_chains WHERE chain_id=?", (chain_id,)).fetchone()


def insert_chain(conn, chain_id: str, positions: list[str], created_at: str) -> None:
    conn.execute("INSERT OR IGNORE INTO cs_chains(chain_id,created_at) VALUES(?,?)",
                 (chain_id, created_at))
    replace_chain_positions(conn, chain_id, positions)


def replace_chain_positions(conn, chain_id: str, positions: list[str]) -> None:
    conn.execute("DELETE FROM cs_chain_positions WHERE chain_id=?", (chain_id,))
    for position, record_id in enumerate(positions, start=1):
        conn.execute(
            "INSERT INTO cs_chain_positions(chain_id,position,record_id) VALUES(?,?,?)",
            (chain_id, position, record_id),
        )


def chain_positions(conn, chain_id: str):
    return conn.execute(
        "SELECT * FROM cs_chain_positions WHERE chain_id=? ORDER BY position", (chain_id,)
    ).fetchall()


def chain_refs(conn, chain_id: str):
    return conn.execute(
        "SELECT * FROM cs_chain_refs WHERE chain_id=? ORDER BY position", (chain_id,)
    ).fetchall()


def replace_chain_refs(conn, chain_id: str, refs: list[dict]) -> None:
    conn.execute("DELETE FROM cs_chain_refs WHERE chain_id=?", (chain_id,))
    for ref in refs:
        conn.execute(
            "INSERT INTO cs_chain_refs(chain_id,position,leg_id,state,reason_code)"
            " VALUES(?,?,?,?,?)",
            (chain_id, ref["position"], ref.get("leg_id"), ref["state"], ref.get("reason_code")),
        )


def chains_touching_records(conn, record_ids) -> list[str]:
    if not record_ids:
        return []
    marks = ",".join("?" for _ in record_ids)
    rows = conn.execute(
        f"SELECT DISTINCT chain_id FROM cs_chain_positions WHERE record_id IN ({marks})"
        " ORDER BY chain_id",
        tuple(sorted(record_ids)),
    ).fetchall()
    return [row[0] for row in rows]


def refs_for_legs(conn, leg_ids) -> list[dict]:
    if not leg_ids:
        return []
    marks = ",".join("?" for _ in leg_ids)
    rows = conn.execute(
        f"SELECT chain_id,position,leg_id,state,reason_code FROM cs_chain_refs"
        f" WHERE leg_id IN ({marks}) ORDER BY chain_id, position",
        tuple(sorted(leg_ids)),
    ).fetchall()
    return [
        {"chain_id": row["chain_id"], "position": row["position"], "leg_id": row["leg_id"],
         "state": row["state"], "reason_code": row["reason_code"]}
        for row in rows
    ]


def refresh_chain_refs(conn, record_ids) -> list[str]:
    """按当前实际航段归属刷新包含这些营销记录的航链引用。"""
    settled = settled_map(conn)
    refreshed = []
    for chain_id in chains_touching_records(conn, record_ids):
        items = []
        for position in chain_positions(conn, chain_id):
            leg_id = settled.get(position["record_id"])
            items.append({
                "position": position["position"],
                "leg_id": leg_id,
                "state": "ACTIVE" if leg_id else "SUSPENDED",
                "reason_code": None if leg_id else "UNSETTLED_RECORD",
            })
        replace_chain_refs(conn, chain_id, items)
        refreshed.append(chain_id)
    return refreshed


# operations ----------------------------------------------------------------

def get_operation(conn, op_id: str):
    return conn.execute("SELECT * FROM cs_operations WHERE op_id=?", (op_id,)).fetchone()


def insert_operation(conn, op: dict) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO cs_operations(op_id,kind,group_id,status,expected_version,"
        "payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (op["op_id"], op["kind"], op["group_id"], op["status"], op["expected_version"],
         json.dumps(op["payload"], ensure_ascii=False), op["created_at"], op["created_at"]),
    )


def update_operation(conn, op_id: str, status: str, updated_at: str) -> None:
    conn.execute("UPDATE cs_operations SET status=?,updated_at=? WHERE op_id=?",
                 (status, updated_at, op_id))


def mark_running(conn, op_id: str, updated_at: str) -> None:
    conn.execute(
        "UPDATE cs_operations SET status='COMPENSATING',updated_at=? WHERE op_id=? AND status IN"
        " ('COMPENSATING','INTERRUPTED')",
        (updated_at, op_id),
    )


def list_operations(conn, group_id: str | None = None):
    if group_id is None:
        return conn.execute("SELECT * FROM cs_operations ORDER BY created_at, op_id").fetchall()
    return conn.execute(
        "SELECT * FROM cs_operations WHERE group_id=? ORDER BY created_at, op_id", (group_id,)
    ).fetchall()


def get_step(conn, op_id: str, step_no: int):
    return conn.execute(
        "SELECT * FROM cs_op_steps WHERE op_id=? AND step_no=?", (op_id, step_no)
    ).fetchone()


def complete_step(conn, op_id: str, step_no: int, name: str, result: dict, updated_at: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO cs_op_steps(op_id,step_no,name,status,result_json,updated_at)"
        " VALUES(?,?,?,'COMPLETED',?,?)",
        (op_id, step_no, name, json.dumps(result, ensure_ascii=False, sort_keys=True), updated_at),
    )


def list_steps(conn, op_id: str):
    return conn.execute(
        "SELECT * FROM cs_op_steps WHERE op_id=? ORDER BY step_no", (op_id,)
    ).fetchall()


# metrics -------------------------------------------------------------------

def recompute_metrics(conn) -> None:
    """按批次重算计数。

    - marketing：本批次已采信的营销记录数。
    - legs_owned：本批次记录作为持有人的活跃实际航段数。
    - folded：本批次记录被吸收进其他实际航段的条数。
    - absorbed：被其他批次持有人的实际航段吸收的本批次记录数。
    - absorbing：本批次持有的实际航段吸收了其他批次的记录数。
    守恒：marketing = legs_owned + folded，且 legs_owned + absorbed = 本批次来源的实际航段数。
    """
    keys = ("marketing", "legs_owned", "folded", "absorbed", "absorbing", "pending", "unsettled")
    data = {
        row[0]: {key: 0 for key in keys}
        for row in conn.execute("SELECT batch_id FROM cs_batches")
    }

    def bucket(batch_id):
        return data.setdefault(batch_id, {key: 0 for key in keys})

    accepted = conn.execute(
        "SELECT record_id,batch_id,group_id FROM cs_records WHERE state='ACCEPTED'"
    ).fetchall()
    for row in accepted:
        bucket(row["batch_id"])["marketing"] += 1

    settled = set()
    for leg in conn.execute(
        "SELECT leg_id,owner_record_id,batch_id FROM cs_legs WHERE state='ACTIVE'"
    ):
        bucket(leg["batch_id"])["legs_owned"] += 1
        for member in leg_members(conn, leg["leg_id"]):
            settled.add(member)
            record = get_record(conn, member)
            if record is None:
                continue
            if record["batch_id"] != leg["batch_id"]:
                bucket(record["batch_id"])["absorbed"] += 1
                bucket(leg["batch_id"])["absorbing"] += 1
            elif member != leg["owner_record_id"]:
                bucket(record["batch_id"])["folded"] += 1

    pending_groups = {
        row[0] for row in conn.execute("SELECT group_id FROM cs_groups WHERE status='PENDING'")
    }
    for row in accepted:
        if row["group_id"] in pending_groups:
            bucket(row["batch_id"])["pending"] += 1
        if row["record_id"] not in settled:
            bucket(row["batch_id"])["unsettled"] += 1

    conn.execute("DELETE FROM cs_batch_metrics")
    for batch_id, values in sorted(data.items()):
        conn.execute(
            "INSERT INTO cs_batch_metrics(batch_id,marketing,legs_owned,folded,absorbed,"
            "absorbing,pending,unsettled) VALUES(?,?,?,?,?,?,?,?)",
            (batch_id, values["marketing"], values["legs_owned"], values["folded"],
             values["absorbed"], values["absorbing"], values["pending"], values["unsettled"]),
        )


def list_metrics(conn):
    return conn.execute("SELECT * FROM cs_batch_metrics ORDER BY batch_id").fetchall()
