"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

All code lives in this one file, in this order:
  1. setup & indexes   2. time / rule helpers   3. request models   4. serializers
  5. pipeline builders (shared by the endpoints and /admin/explain)   6. endpoints
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False: real environment variables take priority
log = logging.getLogger("attendance")

# tz_aware=True: PyMongo hands back aware UTC datetimes instead of naive ones.
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True,serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
GRACE_SECONDS = 10 * 60
MIN_OVERTIME = 30
HALF_DAY_BELOW = Decimal("4.50")
MAX_SHIFT_SECONDS = 24 * 3600
MAX_TREND_DAYS = 92

MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"
DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
HHMM_RE = r"^([01]\d|2[0-3]):[0-5]\d$"

Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]


def ensure_indexes() -> None:
    """Idempotent. Each index and the query it serves is explained in DECISIONS.md."""
    emp, logs = db.employees, db.attendance_logs
    emp.create_index([("emp_code", 1)], unique=True, name="uq_emp_code")
    emp.create_index([("department", 1), ("emp_code", 1)], name="dept_emp_code")
    emp.create_index([("joined_on", 1), ("department", 1)], name="joined_dept")
    logs.create_index([("emp_code", 1), ("date", -1)], unique=True, name="uq_emp_date")
    logs.create_index([("date", -1), ("emp_code", 1)], name="date_emp")
    logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name="status_date_emp")
    logs.create_index([("emp_code", 1), ("punch_in", -1)], name="emp_punch_in")


_indexes_ready = False


def ensure_indexes_once() -> None:
    """Creates the indexes the first time MongoDB answers; a no-op afterwards."""
    global _indexes_ready
    if not _indexes_ready:
        ensure_indexes()  # idempotent; raises PyMongoError while MongoDB is unreachable
        _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes_once()
    except PyMongoError as exc:  # keep the process up; /health retries until Mongo answers
        log.warning("index creation failed, will retry from /health: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time and rule helpers (R1 - R5)
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def from_ms(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return (EPOCH + timedelta(milliseconds=ms)).replace(microsecond=0)


def to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:  # defensive: treat naive values as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def attendance_date(ts: datetime, shift_start: str, shift_end: str) -> date:
    """R1: IST calendar date of the punch-in; overnight shifts belong to the day they started."""
    ist = ts.astimezone(IST)
    d = ist.date()
    if is_overnight(shift_start, shift_end) and ist.time() < parse_hhmm(shift_end):
        d -= timedelta(days=1)
    return d


def shift_start_dt(d: date, shift_start: str) -> datetime:
    return datetime.combine(d, parse_hhmm(shift_start), tzinfo=IST)


def shift_end_dt(d: date, shift_start: str, shift_end: str) -> datetime:
    end = datetime.combine(d, parse_hhmm(shift_end), tzinfo=IST)
    return end + timedelta(days=1) if is_overnight(shift_start, shift_end) else end


def compute_late_minutes(punch_in: datetime, d: date, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then whole minutes since shift start."""
    seconds = (punch_in - shift_start_dt(d, shift_start)).total_seconds()
    return int(seconds // 60) if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, d: date, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes past shift end, only if at least 30."""
    seconds = (punch_out - shift_end_dt(d, shift_start, shift_end)).total_seconds()
    minutes = int(seconds // 60)
    return minutes if minutes >= MIN_OVERTIME else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals, half-up (Decimal, so no float or banker's rounding)."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), ROUND_HALF_UP))


def derive(emp: dict, d: date, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """All four stored derived fields for a presence record (R2-R5)."""
    out = {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    if punch_in is None:
        return out
    out["late_minutes"] = compute_late_minutes(punch_in, d, emp["shift_start"])
    if punch_out is not None:
        wh = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = wh
        out["overtime_minutes"] = compute_overtime(punch_out, d, emp["shift_start"], emp["shift_end"])
        out["half_day"] = Decimal(str(wh)) < HALF_DAY_BELOW
    return out


def half_up(value: Decimal, places: int) -> float:
    return float(value.quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP))


def month_bounds(month: str) -> tuple[str, str]:
    y, m = int(month[:4]), int(month[5:])
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"


def count_weekdays(a: date, b: date) -> int:
    n, d = 0, a
    while d <= b:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


def parse_date_or_422(value: str, name: str) -> date:
    if not re.match(DATE_RE, value or ""):
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{name} is not a valid date")


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_RE)
    shift_end: str = Field(default="18:30", pattern=HHMM_RE)
    joined_on: str = Field(pattern=DATE_RE)

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, v: str) -> str:
        date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Serializers (BSON -> API)
# --------------------------------------------------------------------------- #
def emp_out(d: dict) -> dict:
    created = d.get("created_at")
    return {
        "emp_code": d["emp_code"], "name": d["name"], "email": d["email"], "department": d["department"],
        "shift_start": d["shift_start"], "shift_end": d["shift_end"], "joined_on": d["joined_on"],
        "created_at": to_ms(created) if created else None,
    }


def _history_value(key: str, v: Any) -> Any:
    return to_ms(v) if key in ("punch_in", "punch_out") and isinstance(v, datetime) else v


def history_out(entry: dict) -> dict:
    return {
        "at": to_ms(entry["at"]), "by": entry.get("by"), "reason": entry.get("reason"),
        "changes": {k: {"from": _history_value(k, c.get("from")), "to": _history_value(k, c.get("to"))}
                    for k, c in (entry.get("changes") or {}).items()},
    }


def record_out(d: dict) -> dict:
    """Tolerates legacy documents: missing history / half_day / late / overtime mean empty / false / 0."""
    return {
        "emp_code": d["emp_code"], "date": d["date"], "status": d["status"],
        "punch_in": to_ms(d["punch_in"]) if d.get("punch_in") else None,
        "punch_out": to_ms(d["punch_out"]) if d.get("punch_out") else None,
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes") or 0,
        "overtime_minutes": d.get("overtime_minutes") or 0,
        "half_day": bool(d.get("half_day", False)),
        "history": [history_out(h) for h in (d.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# Query / pipeline builders. The endpoints and /admin/explain both call these,
# so the explained query is exactly the one that runs.
# --------------------------------------------------------------------------- #
def attendance_find_spec(emp_code, date_from, date_to, status, page, page_size) -> dict:
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return {"filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size}


def check_attendance_filters(date_from, date_to) -> None:
    f = parse_date_or_422(date_from, "date_from") if date_from else None
    t = parse_date_or_422(date_to, "date_to") if date_to else None
    if f and t and f > t:
        raise HTTPException(422, "date_from must not be after date_to")


_IS_PRESENT = {"$in": ["$status", PRESENCE]}
_DOW = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}  # 1=Sun .. 7=Sat
_IS_WEEKDAY = {"$and": [{"$gte": [_DOW, 2]}, {"$lte": [_DOW, 6]}]}
_IS_HALF = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
_HAS_HOURS = {"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}
# Presence on a weekday, in half-day units (full day = 2, half day = 1) so every sum stays an exact integer.
_WEEKDAY_UNITS = {"$cond": [{"$and": [_IS_PRESENT, _IS_WEEKDAY]}, {"$cond": [_IS_HALF, 1, 2]}, 0]}
_ANY_DAY_UNITS = {"$cond": [_IS_PRESENT, {"$cond": [_IS_HALF, 1, 2]}, 0]}
_LATE = {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}


def _per_key_group(key: Any) -> dict:
    return {"$group": {
        "_id": key,
        "units": {"$sum": _WEEKDAY_UNITS},
        "leave": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [_LATE, 1, 0]}},
        "late_total": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        "ot_total": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "wh_cents": {"$sum": {"$cond": [_HAS_HOURS, {"$toLong": {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}}, 0]}},
        "wh_n": {"$sum": {"$cond": [_HAS_HOURS, 1, 0]}},
    }}


def monthly_pipeline(emp_code: str, month: str) -> list:
    start, end = month_bounds(month)
    return [{"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}}, _per_key_group(None)]


_SUM_FIELDS = ["headcount", "units", "leave", "on_duty", "late_count", "late_total", "wh_cents", "wh_n"]


def summary_pipeline(month: str, department: Optional[str]) -> list:
    """Runs on attendance_logs (range index), aggregates per employee, joins employees for the department,
    then unions in the headcount computed from `employees` so people with zero logs still count (R9)."""
    start, end = month_bounds(month)
    pl: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        _per_key_group("$emp_code"),
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department:
        pl.append({"$match": {"emp.department": department}})
    pl += [
        {"$group": {"_id": "$emp.department", **{f: {"$sum": f"${f}"} for f in _SUM_FIELDS if f != "headcount"}}},
        {"$addFields": {"headcount": 0}},
    ]
    head_match: dict = {"joined_on": {"$lte": end}}
    if department:
        head_match["department"] = department
    pl.append({"$unionWith": {"coll": "employees", "pipeline": [
        {"$match": head_match},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
        {"$addFields": {f: 0 for f in _SUM_FIELDS if f != "headcount"}},
    ]}})
    pl += [
        {"$group": {"_id": "$_id", **{f: {"$sum": f"${f}"} for f in _SUM_FIELDS}}},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"_id": 1}},
    ]
    return pl


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    start, end = month_bounds(month)
    pl: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}, "cnt": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department:
        pl.append({"$match": {"emp.department": department}})
    pl += [
        # $rank is standard competition ranking: 1, 2, 2, 4
        {"$setWindowFields": {"sortBy": {"total": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
                      "total_late_minutes": "$total", "late_count": "$cnt"}},
    ]
    return pl


def trend_pipeline(codes: list[str], joined_dates: list[str], frm: date, to: date) -> list:
    lower = datetime(frm.year, frm.month, frm.day, tzinfo=timezone.utc)
    upper = datetime(to.year, to.month, to.day, tzinfo=timezone.utc) + timedelta(days=1)  # bound is exclusive
    window = {"documents": [-6, 0]}
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": frm.isoformat(), "$lte": to.isoformat()}}},
        {"$group": {"_id": "$date", "units": {"$sum": _ANY_DAY_UNITS},
                    "late_count": {"$sum": {"$cond": [_LATE, 1, 0]}}}},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},
        {"$addFields": {
            "units": {"$ifNull": ["$units", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "ds": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
            "dow": {"$dayOfWeek": "$d"},
        }},
        {"$addFields": {
            "working": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
            "headcount": {"$size": {"$filter": {"input": {"$literal": joined_dates}, "as": "j",
                                                "cond": {"$lte": ["$$j", "$ds"]}}}},
        }},
        # rate in 1e-4 units, half-up in integer arithmetic: floor(units/(2*hc) * 10000 + 0.5)
        {"$addFields": {"rate_i": {"$cond": [
            {"$and": ["$working", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [{"$add": [{"$multiply": ["$units", 10000]}, "$headcount"]},
                                    {"$multiply": ["$headcount", 2]}]}},
            None]}}},
        {"$addFields": {"has_rate": {"$cond": [{"$ne": ["$rate_i", None]}, 1, 0]}}},
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "ma_sum": {"$sum": "$rate_i", "window": window},
            "ma_n": {"$sum": "$has_rate", "window": window}}}},
        {"$sort": {"d": 1}},
        {"$project": {"_id": 0, "date": "$ds", "is_working_day": "$working", "headcount": 1, "units": 1,
                      "late_count": 1, "rate_i": 1, "ma_sum": 1, "ma_n": 1}},
    ]


def _half_units(units: int) -> float:
    return units / 2


def trend_inputs(department: str) -> tuple[list[str], list[str]]:
    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "unknown department")
    return [e["emp_code"] for e in emps], sorted(e["joined_on"] for e in emps)


def check_trend_range(frm_s: str, to_s: str) -> tuple[date, date]:
    frm, to = parse_date_or_422(frm_s, "from"), parse_date_or_422(to_s, "to")
    if to < frm:
        raise HTTPException(422, "'to' must not be before 'from'")
    if (to - frm).days + 1 > MAX_TREND_DAYS:
        raise HTTPException(422, f"range must not exceed {MAX_TREND_DAYS} days")
    return frm, to


# --------------------------------------------------------------------------- #
# Endpoints: system, employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    try:
        ensure_indexes_once()  # retry index creation, but never let it fail the health check
    except PyMongoError as exc:
        log.warning("index creation failed: %s", exc)
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:  # the unique index is the guarantee; there is no check-then-insert
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return emp_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    cur = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [emp_out(e) for e in cur], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Endpoints: attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {"emp_code": body.emp_code, "date": d.isoformat(), "status": body.status, "punch_in": ts,
           "punch_out": None, **derive(emp, d, ts, None), "history": []}
    try:  # unique (emp_code, date) index: exactly one concurrent request wins
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    rec = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
                                      sort=[("punch_in", -1)])
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = rec["punch_in"]
    if ts <= p_in or (ts - p_in).total_seconds() > MAX_SHIFT_SECONDS:
        raise HTTPException(422, "punched_at must be after punch-in and within 24 hours of it")
    derived = derive(emp, date.fromisoformat(rec["date"]), p_in, ts)
    # the filter on punch_out: null makes the close atomic, so of two simultaneous punch-outs only one matches
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}}, return_document=ReturnDocument.AFTER)
    if not updated:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None,
                    date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[Status] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    check_attendance_filters(date_from, date_to)
    spec = attendance_find_spec(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(spec["filter"])
    cur = (db.attendance_logs.find(spec["filter"]).sort([("date", -1), ("emp_code", 1)])
           .skip(spec["skip"]).limit(spec["limit"]))
    return {"items": [record_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


def _regularize_once(emp: dict, rec: dict, body: RegularizeIn) -> Optional[dict]:
    """Applies one correction to `rec`. Returns the updated document, or None if the record changed under us."""
    d = date.fromisoformat(rec["date"])
    new_status = body.status or rec["status"]
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "punch times cannot be supplied together with status ABSENT or LEAVE")
        new_in = new_out = None
    else:
        new_in = from_ms(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        new_out = from_ms(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if new_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if body.punch_in is not None and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != d:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None and not (new_in < new_out and (new_out - new_in).total_seconds() <= MAX_SHIFT_SECONDS):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours of it")
    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derive(emp, d, new_in, new_out)}
    old = {"status": rec["status"], "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
           "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
           "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False))}
    changes = {k: {"from": old[k], "to": new[k]} for k in new if new[k] != old[k]}
    if not changes:
        raise HTTPException(422, "the request changes nothing")
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # optimistic guard: only write if history length and punch times are still what we read
    guard = {"_id": rec["_id"], "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
             "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(rec.get("history") or [])]}}
    return db.attendance_logs.find_one_and_update(
        guard, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date_or_422(date, "date")
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    for _ in range(5):  # a concurrent edit makes the guarded update miss; re-read and re-apply on top of it
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if not rec:
            raise HTTPException(404, "no record for that date")
        updated = _regularize_once(emp, rec, body)
        if updated:
            return record_out(updated)
    raise HTTPException(409, "record is being modified concurrently, retry")


# --------------------------------------------------------------------------- #
# Endpoints: analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_RE)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, month)))
    r = rows[0] if rows else {}
    start, end = month_bounds(month)
    first = max(date.fromisoformat(start), date.fromisoformat(emp["joined_on"]))
    last = date.fromisoformat(end)
    working = count_weekdays(first, last) if first <= last else 0
    units = r.get("units", 0)
    pct = half_up(Decimal(units) * 50 / working, 2) if working else None  # units/2/working*100
    return {"emp_code": emp_code, "month": month, "working_days": working, "present_days": _half_units(units),
            "leave_days": r.get("leave", 0), "late_count": r.get("late_count", 0),
            "total_late_minutes": r.get("late_total", 0), "total_overtime_minutes": r.get("ot_total", 0),
            "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_RE), department: Optional[str] = None):
    items = []
    for r in db.attendance_logs.aggregate(summary_pipeline(month, department)):
        n = r["wh_n"]
        items.append({
            "department": r["_id"], "headcount": r["headcount"], "present_days": _half_units(r["units"]),
            "avg_work_hours": half_up(Decimal(r["wh_cents"]) / Decimal(n * 100), 2) if n else None,
            "late_count": r["late_count"], "total_late_minutes": r["late_total"],
            "leave_count": r["leave"], "on_duty_count": r["on_duty"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=MONTH_RE), limit: int = Query(10, ge=1, le=50),
                     department: Optional[str] = None):
    return {"month": month, "items": list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department)))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    frm, to_d = check_trend_range(from_, to)
    codes, joined = trend_inputs(department)
    items = []
    for r in db.attendance_logs.aggregate(trend_pipeline(codes, joined, frm, to_d)):
        rate = None if r["rate_i"] is None else half_up(Decimal(str(r["rate_i"])) / 10000, 4)
        ma = None
        if r["ma_n"]:
            ma = half_up((Decimal(str(r["ma_sum"])) / Decimal(r["ma_n"])).quantize(Decimal(1), ROUND_HALF_UP) / 10000, 4)
        items.append({"date": r["date"], "is_working_day": r["is_working_day"], "headcount": r["headcount"],
                      "present_count": _half_units(r["units"]), "late_count": r["late_count"],
                      "attendance_rate": rate, "moving_avg_7d": ma})
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Endpoint: admin / explain
# --------------------------------------------------------------------------- #
def _need(**params) -> None:
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise HTTPException(422, f"missing required parameter(s): {', '.join(missing)}")


def _explain(cmd: dict) -> dict:
    res = db.command({"explain": cmd, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(res, json_options=json_util.RELAXED_JSON_OPTIONS))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    coll = "attendance_logs"
    if endpoint == "attendance_list":
        check_attendance_filters(date_from, date_to)
        spec = attendance_find_spec(emp_code, date_from, date_to, status, page, page_size)
        cmd = {"find": coll, **spec}
    elif endpoint == "employee_monthly":
        _need(emp_code=emp_code, month=month)
        cmd = {"aggregate": coll, "pipeline": monthly_pipeline(emp_code, month), "cursor": {}}
    elif endpoint == "department_summary":
        _need(month=month)
        cmd = {"aggregate": coll, "pipeline": summary_pipeline(month, department), "cursor": {}}
    elif endpoint == "late_leaderboard":
        _need(month=month)
        cmd = {"aggregate": coll, "pipeline": leaderboard_pipeline(month, limit, department), "cursor": {}}
    else:
        _need(department=department, **{"from": from_, "to": to})
        frm, to_d = check_trend_range(from_, to)
        codes, joined = trend_inputs(department)
        cmd = {"aggregate": coll, "pipeline": trend_pipeline(codes, joined, frm, to_d), "cursor": {}}
    return {"endpoint": endpoint, "collection": coll, "explain": _explain(cmd)}