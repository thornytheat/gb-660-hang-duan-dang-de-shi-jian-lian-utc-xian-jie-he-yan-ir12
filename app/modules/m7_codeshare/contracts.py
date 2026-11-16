"""营销航班记录契约与共享关系识别。

共享关系只来自承运方声明（operating_carrier + operating_flight_no）。
时刻相同永远不足以证明共享关系，因此这里不提供任何按时刻猜测的分组逻辑。
"""
import json
import re
from datetime import date, datetime

from app.errors import DomainError

FLIGHT_NO = re.compile(r"^[A-Z0-9]{2,3}\d{1,4}$")
CARRIER = re.compile(r"^[A-Z0-9]{2,3}$")
AIRPORT = re.compile(r"^[A-Z]{3}$")

REQUIRED = (
    "marketing_flight_no",
    "marketing_carrier",
    "flight_date",
    "origin",
    "destination",
    "departure_local",
    "arrival_local",
)

# 只有这些字段描述“实际承运航段”的时刻。航班号/日期/机场由 group key 固定。
SCHEDULE_FIELDS = ("departure_local", "arrival_local", "departure_fold", "arrival_fold")
EVIDENCE_FIELDS = (
    "record_id", "batch_id", "line_no", "marketing_carrier", "marketing_flight_no",
    "operating_carrier", "operating_flight_no", *SCHEDULE_FIELDS,
)


def text(value) -> str:
    return "" if value is None else str(value).strip()


def _fold(value):
    token = text(value)
    if token == "":
        return None
    if token not in ("0", "1"):
        raise DomainError("BAD_FORMAT", "fold 只能是 0 或 1")
    return int(token)


def _local_time(value: str, field: str) -> str:
    if "T" not in value:
        raise DomainError("BAD_FORMAT", f"{field} 必须是带 T 的当地时刻")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise DomainError("BAD_FORMAT", f"{field} 无法解析") from exc
    return value


def normalize_record(source: dict, batch_id: str, line_no: int) -> dict:
    row = {key: text(source.get(key)) for key in REQUIRED}
    missing = [key for key in REQUIRED if not row[key]]
    if missing:
        raise DomainError("EMPTY_VALUE", f"必填值为空：{','.join(missing)}")

    row["marketing_flight_no"] = row["marketing_flight_no"].upper().replace(" ", "")
    row["marketing_carrier"] = row["marketing_carrier"].upper().replace(" ", "")
    row["origin"] = row["origin"].upper()
    row["destination"] = row["destination"].upper()

    if not FLIGHT_NO.fullmatch(row["marketing_flight_no"]):
        raise DomainError("BAD_FORMAT", "marketing_flight_no 格式不正确")
    if not CARRIER.fullmatch(row["marketing_carrier"]):
        raise DomainError("BAD_FORMAT", "marketing_carrier 格式不正确")
    if not AIRPORT.fullmatch(row["origin"]) or not AIRPORT.fullmatch(row["destination"]):
        raise DomainError("BAD_FORMAT", "机场三字码格式不正确")
    try:
        date.fromisoformat(row["flight_date"])
    except ValueError as exc:
        raise DomainError("BAD_FORMAT", "flight_date 必须是 ISO 日期") from exc

    row["departure_local"] = _local_time(row["departure_local"], "departure_local")
    row["arrival_local"] = _local_time(row["arrival_local"], "arrival_local")
    row["departure_fold"] = _fold(source.get("departure_fold"))
    row["arrival_fold"] = _fold(source.get("arrival_fold"))

    operating_carrier = text(source.get("operating_carrier")).upper().replace(" ", "")
    operating_flight_no = text(source.get("operating_flight_no")).upper().replace(" ", "")
    if bool(operating_carrier) != bool(operating_flight_no):
        raise DomainError("BAD_DECLARATION", "承运方声明必须同时给出承运人和实际航班号")
    if operating_carrier and not CARRIER.fullmatch(operating_carrier):
        raise DomainError("BAD_FORMAT", "operating_carrier 格式不正确")
    if operating_flight_no and not FLIGHT_NO.fullmatch(operating_flight_no):
        raise DomainError("BAD_FORMAT", "operating_flight_no 格式不正确")

    row["operating_carrier"] = operating_carrier or None
    row["operating_flight_no"] = operating_flight_no or None
    row["batch_id"] = batch_id
    row["line_no"] = int(line_no)
    row["record_id"] = record_id(batch_id, line_no)
    row["source_json"] = json.dumps(source, ensure_ascii=False, sort_keys=True, default=str)
    return row


def record_id(batch_id: str, line_no: int) -> str:
    return f"{batch_id}:{int(line_no)}"


def declared_operating_key(record: dict) -> str | None:
    """仅凭承运方声明得到实际承运航线键；未声明的记录不属于任何共享组。"""
    if not record.get("operating_carrier") or not record.get("operating_flight_no"):
        return None
    return "|".join((
        record["operating_carrier"], record["operating_flight_no"], record["flight_date"],
        record["origin"], record["destination"],
    ))


def group_id_for(operating_key: str) -> str:
    from hashlib import sha256
    return "g-" + sha256(operating_key.encode("utf-8")).hexdigest()[:12]


def get_field(record, field):
    try:
        return record[field]
    except (KeyError, IndexError):
        return None


def schedule_facts(record: dict) -> dict:
    return {field: get_field(record, field) for field in SCHEDULE_FIELDS}


def choose_representative(records: list, source_record_id: str | None = None) -> str:
    """裁决指定来源优先；否则优先实际承运人自己的营销记录，再按批次与行号稳定选择。"""
    if source_record_id is not None and any(
        get_field(record, "record_id") == source_record_id for record in records
    ):
        return source_record_id

    def key(record):
        own = 0 if (
            get_field(record, "marketing_carrier") == get_field(record, "operating_carrier")
            and get_field(record, "marketing_flight_no") == get_field(record, "operating_flight_no")
        ) else 1
        return (own, get_field(record, "batch_id"), int(get_field(record, "line_no")))

    return sorted(records, key=key)[0]["record_id"]


def conflict_evidence(records: list) -> dict | None:
    """同一声明键下时刻声明不一致即为来源冲突；不做任何“少数服从多数”猜测。"""
    facts = [schedule_facts(record) for record in records]
    fields = [field for field in SCHEDULE_FIELDS
              if len({str(item[field]) for item in facts}) > 1]
    if not fields:
        return None
    return {
        "fields": fields,
        "sources": [
            {field: record[field] for field in EVIDENCE_FIELDS}
            for record in sorted(records, key=lambda item: (item["batch_id"], item["line_no"]))
        ],
    }
