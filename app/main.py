"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

Contract: openapi.yaml
Data Model: DATA_MODEL.md
"""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List
from contextlib import contextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST_OFFSET = timedelta(hours=5, minutes=30)
IST = timezone(IST_OFFSET)

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
EPOCH_MIN = 100000000000  # 1973-03-03 (reject seconds-looking values)
EPOCH_MAX = 4102444800000  # 2100-01-01
GRACE_MINUTES = 10
OVERTIME_THRESHOLD_MINUTES = 30
HALF_DAY_THRESHOLD = 4.50

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}


# --------------------------------------------------------------------------- #
# Helper Functions
# --------------------------------------------------------------------------- #
def epoch_ms_to_datetime(ms: int) -> datetime:
    """Convert epoch milliseconds to UTC datetime, truncated to whole seconds (R1)."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.replace(microsecond=0)


def datetime_to_epoch_ms(dt: datetime) -> int:
    """Convert UTC datetime to epoch milliseconds."""
    return int(dt.timestamp() * 1000)


def validate_epoch_ms(ms: Optional[int]) -> None:
    """Validate epoch millisecond value."""
    if ms is not None:
        if not isinstance(ms, int) or ms < EPOCH_MIN or ms > EPOCH_MAX:
            raise HTTPException(422, "punched_at must be a valid epoch millisecond timestamp")


def get_ist_datetime(utc_dt: datetime) -> datetime:
    """Convert UTC datetime to IST datetime."""
    return utc_dt.astimezone(IST)


def get_attendance_date(utc_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    Determine attendance date per R1.
    For overnight shifts (shift_end <= shift_start), a punch-in whose IST time
    is earlier than shift_end belongs to the previous day's shift.
    """
    ist_dt = get_ist_datetime(utc_dt)
    h, m = map(int, shift_end.split(":"))
    shift_end_ist = ist_dt.replace(hour=h, minute=m, second=0, microsecond=0)

    # Overnight shift check
    if shift_end <= shift_start:
        # If current IST time is before shift_end, it belongs to previous day
        if ist_dt < shift_end_ist:
            ist_dt = ist_dt - timedelta(days=1)

    return ist_dt.date().isoformat()


def compute_shift_start_datetime(attendance_date: str, shift_start: str) -> datetime:
    """Get shift start as UTC datetime for the given attendance date."""
    h, m = map(int, shift_start.split(":"))
    # Create datetime in IST, then convert to UTC
    ist_dt = datetime.fromisoformat(attendance_date).replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)
    return ist_dt.astimezone(timezone.utc)


def compute_shift_end_datetime(attendance_date: str, shift_end: str) -> datetime:
    """Get shift end as UTC datetime for the given attendance date (handles overnight)."""
    h, m = map(int, shift_end.split(":"))
    ist_dt = datetime.fromisoformat(attendance_date).replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)
    return ist_dt.astimezone(timezone.utc)


def compute_late_minutes(punch_in_utc: datetime, attendance_date: str, shift_start: str) -> int:
    """
    Compute late minutes per R2.
    Grace period is 10 minutes. Late only if strictly more than 10 minutes after shift start.
    Returns whole minutes (floor) elapsed since shift start.
    """
    shift_start_utc = compute_shift_start_datetime(attendance_date, shift_start)
    diff_seconds = (punch_in_utc - shift_start_utc).total_seconds()
    minutes = int(diff_seconds // 60)
    return minutes if minutes > GRACE_MINUTES else 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    Compute work hours per R4.
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    seconds = (punch_out_utc - punch_in_utc).total_seconds()
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def compute_overtime_minutes(punch_out_utc: datetime, attendance_date: str, shift_end: str) -> int:
    """
    Compute overtime minutes per R3.
    Whole minutes (floor) between shift_end and punch_out, only when >= 30.
    """
    shift_end_utc = compute_shift_end_datetime(attendance_date, shift_end)
    diff_seconds = (punch_out_utc - shift_end_utc).total_seconds()
    minutes = int(diff_seconds // 60)
    return minutes if minutes >= OVERTIME_THRESHOLD_MINUTES else 0


def compute_half_day(work_hours: float) -> bool:
    """Compute half_day per R5: true when rounded work_hours < 4.50."""
    return work_hours < HALF_DAY_THRESHOLD


def is_working_day(date_str: str) -> bool:
    """Check if a date is a working day (Mon-Fri) per R7."""
    dt = datetime.fromisoformat(date_str)
    return dt.weekday() < 5  # 0=Mon, 4=Fri


def round_half_up(value: float, decimals: int = 2) -> float:
    """Round value to specified decimals using half-up rounding (R8)."""
    multiplier = 10 ** decimals
    return float(Decimal(value) * Decimal(multiplier)).quantize(Decimal('1'), rounding=ROUND_HALF_UP) / multiplier


def round_half_up_rate(value: float) -> float:
    """Round rate to 4 decimals half-up (R8)."""
    return round_half_up(value, 4)


# --------------------------------------------------------------------------- #
# Pydantic Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r'^EMP\d{4,6}$')
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    shift_end: str = Field(default="18:30", pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    joined_on: str = Field(..., format='date')

    @field_validator('shift_end')
    @classmethod
    def shift_end_different(cls, v, info):
        if 'shift_start' in info.data and v == info.data['shift_start']:
            raise ValueError('shift_end must differ from shift_start')
        return v


class Employee(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class EmployeePage(BaseModel):
    items: List[Employee]
    total: int
    page: int
    page_size: int


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=EPOCH_MIN, le=EPOCH_MAX)
    status: str = Field(default="PRESENT")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=EPOCH_MIN, le=EPOCH_MAX)


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = Field(None, ge=EPOCH_MIN, le=EPOCH_MAX)
    punch_out: Optional[int] = Field(None, ge=EPOCH_MIN, le=EPOCH_MAX)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


class HistoryEntry(BaseModel):
    at: int
    by: str
    reason: str
    changes: dict


class AttendanceRecord(BaseModel):
    emp_code: str
    date: str
    status: str
    punch_in: Optional[int]
    punch_out: Optional[int]
    work_hours: Optional[float]
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: List[HistoryEntry]


class AttendancePage(BaseModel):
    items: List[AttendanceRecord]
    total: int
    page: int
    page_size: int


class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float]


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float]
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(BaseModel):
    month: str
    items: List[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class Leaderboard(BaseModel):
    month: str
    items: List[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float]
    moving_avg_7d: Optional[float]


class Trend(BaseModel):
    department: str
    items: List[TrendItem]


class ExplainResponse(BaseModel):
    endpoint: str
    collection: str
    explain: dict


# --------------------------------------------------------------------------- #
# Startup: Create Indexes
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def create_indexes():
    """Create required database indexes idempotently."""
    # Employees: unique emp_code
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)

    # Attendance logs: compound unique on (emp_code, date) for punch-in concurrency
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)

    # Indexes for analytics and queries
    db.attendance_logs.create_index([("date", -1)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)])
    db.attendance_logs.create_index([("status", ASCENDING)])
    db.employees.create_index([("department", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Readiness probe - returns 200 only if MongoDB answers a ping."""
    try:
        db.command('ping')
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB not available")


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201, response_model=Employee)
def create_employee(body: EmployeeCreate):
    """Create an employee with unique emp_code."""
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    doc["created_at"] = datetime_to_epoch_ms(doc["created_at"])
    return Employee(**doc)


@app.get("/employees", response_model=EmployeePage)
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """List employees sorted by emp_code ascending."""
    q = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)

    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = []
    for doc in cursor:
        doc["created_at"] = datetime_to_epoch_ms(doc["created_at"])
        items.append(Employee(**doc))

    return EmployeePage(items=items, total=total, page=page, page_size=page_size)


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201, response_model=AttendanceRecord)
def punch_in(body: PunchInRequest):
    """Record a punch-in with concurrency control."""
    validate_epoch_ms(body.punched_at)

    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    if body.status not in PRESENCE_STATUSES:
        raise HTTPException(422, "status must be one of PRESENT, WFH, ON_DUTY")

    # Determine punch time and attendance date
    punch_utc = epoch_ms_to_datetime(body.punched_at) if body.punched_at else datetime.now(timezone.utc)
    attendance_date = get_attendance_date(punch_utc, emp["shift_start"], emp["shift_end"])

    # Compute late minutes
    late_minutes = compute_late_minutes(punch_utc, attendance_date, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return attendance_record_to_response(doc)


@app.post("/attendance/punch-out", response_model=AttendanceRecord)
def punch_out(body: PunchOutRequest):
    """Record a punch-out, computing work hours, overtime, and half-day."""
    validate_epoch_ms(body.punched_at)

    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    punch_utc = epoch_ms_to_datetime(body.punched_at) if body.punched_at else datetime.now(timezone.utc)

    # Find most recent punch-in at or before punched_at
    query = {
        "emp_code": body.emp_code,
        "punch_in": {"$lte": punch_utc},
        "punch_out": None
    }
    record = db.attendance_logs.find_one(query, sort=[("punch_in", DESCENDING)])

    if not record:
        raise HTTPException(404, "no open punch-in found")

    if punch_utc <= record["punch_in"]:
        raise HTTPException(422, "punch_out must be after punch_in")

    if (punch_utc - record["punch_in"]).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    # Check concurrency: if already punched out, return 409
    current = db.attendance_logs.find_one({"_id": record["_id"]})
    if current.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    # Compute derived fields
    attendance_date = record["date"]
    work_hours = compute_work_hours(record["punch_in"], punch_utc)
    overtime_minutes = compute_overtime_minutes(punch_utc, attendance_date, emp["shift_end"])
    half_day = compute_half_day(work_hours)

    update = {
        "$set": {
            "punch_out": punch_utc,
            "work_hours": work_hours,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day
        }
    }

    # Use optimistic concurrency: only update if punch_out is still null
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        update
    )

    if result.modified_count == 0:
        raise HTTPException(409, "already punched out")

    record.update(update["$set"])
    return attendance_record_to_response(record)


@app.get("/attendance", response_model=AttendancePage)
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """List attendance records with database-level pagination."""
    q = {}

    if emp_code:
        q["emp_code"] = emp_code

    if date_from or date_to:
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from cannot be greater than date_to")
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    if status:
        if status not in ALL_STATUSES:
            raise HTTPException(422, f"invalid status: {status}")
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size

    cursor = db.attendance_logs.find(q, {"_id": 0}) \
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)]) \
        .skip(skip) \
        .limit(page_size)

    items = [attendance_record_to_response(doc) for doc in cursor]

    return AttendancePage(items=items, total=total, page=page, page_size=page_size)


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceRecord)
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    """Manually correct an attendance record with audit trail."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "attendance record not found")

    # Track changes
    changes = {}
    updates = {}

    # Handle status change
    if body.status is not None:
        if body.status not in ALL_STATUSES:
            raise HTTPException(422, f"invalid status: {body.status}")

        if body.status != record.get("status"):
            changes["status"] = {"from": record.get("status"), "to": body.status}
            updates["status"] = body.status

            # If changing to ABSENT or LEAVE, clear punch times
            if body.status in {"ABSENT", "LEAVE"}:
                if body.punch_in is not None or body.punch_out is not None:
                    raise HTTPException(422, "cannot provide punch times with ABSENT or LEAVE status")
                if record.get("punch_in") is not None:
                    changes["punch_in"] = {"from": datetime_to_epoch_ms(record["punch_in"]), "to": None}
                    updates["punch_in"] = None
                if record.get("punch_out") is not None:
                    changes["punch_out"] = {"from": datetime_to_epoch_ms(record["punch_out"]), "to": None}
                    updates["punch_out"] = None
                updates["work_hours"] = None
                changes["work_hours"] = {"from": record.get("work_hours"), "to": None}
                updates["late_minutes"] = 0
                changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": 0}
                updates["overtime_minutes"] = 0
                changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": 0}
                updates["half_day"] = False
                changes["half_day"] = {"from": record.get("half_day", False), "to": False}

    # Handle punch_in change
    if body.punch_in is not None:
        new_punch_in = epoch_ms_to_datetime(body.punch_in)
        new_date = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if new_date != date:
            raise HTTPException(422, "punch_in must be on the attendance date")

        # If current status is ABSENT/LEAVE, punch_in not allowed
        if record.get("status") in {"ABSENT", "LEAVE"}:
            raise HTTPException(422, "cannot set punch_in for ABSENT or LEAVE status")

        if record.get("punch_in") is None or datetime_to_epoch_ms(record["punch_in"]) != body.punch_in:
            old_val = datetime_to_epoch_ms(record["punch_in"]) if record.get("punch_in") else None
            changes["punch_in"] = {"from": old_val, "to": body.punch_in}
            updates["punch_in"] = new_punch_in

            # Recompute late_minutes
            new_late = compute_late_minutes(new_punch_in, date, emp["shift_start"])
            if new_late != record.get("late_minutes", 0):
                changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late}
                updates["late_minutes"] = new_late

    # Handle punch_out change
    if body.punch_out is not None:
        new_punch_out = epoch_ms_to_datetime(body.punch_out)

        # If current status is ABSENT/LEAVE, punch_out not allowed
        if record.get("status") in {"ABSENT", "LEAVE"}:
            raise HTTPException(422, "cannot set punch_out for ABSENT or LEAVE status")

        # Must have punch_in
        punch_in = updates.get("punch_in", record.get("punch_in"))
        if punch_in is None:
            raise HTTPException(422, "punch_in required before punch_out")

        if new_punch_out <= punch_in:
            raise HTTPException(422, "punch_out must be after punch_in")

        if (new_punch_out - punch_in).total_seconds() > 24 * 3600:
            raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

        if record.get("punch_out") is None or datetime_to_epoch_ms(record["punch_out"]) != body.punch_out:
            old_val = datetime_to_epoch_ms(record["punch_out"]) if record.get("punch_out") else None
            changes["punch_out"] = {"from": old_val, "to": body.punch_out}
            updates["punch_out"] = new_punch_out

            # Recompute work_hours, overtime, half_day
            final_punch_in = updates.get("punch_in", record.get("punch_in"))
            new_work_hours = compute_work_hours(final_punch_in, new_punch_out)
            new_overtime = compute_overtime_minutes(new_punch_out, date, emp["shift_end"])
            new_half_day = compute_half_day(new_work_hours)

            if new_work_hours != record.get("work_hours"):
                changes["work_hours"] = {"from": record.get("work_hours"), "to": new_work_hours}
                updates["work_hours"] = new_work_hours

            if new_overtime != record.get("overtime_minutes", 0):
                changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": new_overtime}
                updates["overtime_minutes"] = new_overtime

            if new_half_day != record.get("half_day", False):
                changes["half_day"] = {"from": record.get("half_day", False), "to": new_half_day}
                updates["half_day"] = new_half_day

    # If no changes, return 422
    if not changes:
        raise HTTPException(422, "no changes to apply")

    # Append history entry
    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    updates["$push"] = {"history": history_entry}

    result = db.attendance_logs.update_one({"_id": record["_id"]}, updates)
    if result.modified_count == 0:
        raise HTTPException(409, "concurrent modification")

    record.update(updates)
    record["history"] = record.get("history", [])
    return attendance_record_to_response(record)


def attendance_record_to_response(doc: dict) -> AttendanceRecord:
    """Convert MongoDB document to API response."""
    # Convert datetimes to epoch milliseconds
    punch_in = datetime_to_epoch_ms(doc["punch_in"]) if doc.get("punch_in") else None
    punch_out = datetime_to_epoch_ms(doc["punch_out"]) if doc.get("punch_out") else None

    # Convert history entries
    history = []
    for entry in doc.get("history", []):
        hist_changes = {}
        for field, change in entry.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):
                # Convert BSON dates to epoch ms
                from_val = datetime_to_epoch_ms(change["from"]) if change.get("from") else None
                to_val = datetime_to_epoch_ms(change["to"]) if change.get("to") else None
                hist_changes[field] = {"from": from_val, "to": to_val}
            else:
                hist_changes[field] = change

        history.append(HistoryEntry(
            at=datetime_to_epoch_ms(entry["at"]),
            by=entry["by"],
            reason=entry["reason"],
            changes=hist_changes
        ))

    return AttendanceRecord(
        emp_code=doc["emp_code"],
        date=doc["date"],
        status=doc["status"],
        punch_in=punch_in,
        punch_out=punch_out,
        work_hours=doc.get("work_hours"),
        late_minutes=doc.get("late_minutes", 0),
        overtime_minutes=doc.get("overtime_minutes", 0),
        half_day=doc.get("half_day", False),
        history=history
    )


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthly)
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$')):
    """Monthly attendance summary for one employee."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    # Determine working days from joined_on to end of month (R7)
    year, month_num = map(int, month.split("-"))
    if month_num == 12:
        month_end = f"{year + 1}-01-01"
    else:
        month_end = f"{year}-{month_num + 1:02d}-01"

    joined_on = emp["joined_on"]
    if joined_on > month_end:
        # Employee joined after this month
        return EmployeeMonthly(
            emp_code=emp_code,
            month=month,
            working_days=0,
            present_days=0.0,
            leave_days=0,
            late_count=0,
            total_late_minutes=0,
            total_overtime_minutes=0,
            attendance_pct=None
        )

    # Count working days (Mon-Fri) from joined_on to end of month
    start_date = max(joined_on, f"{year}-{month_num:02d}-01")
    working_days = 0
    current = datetime.fromisoformat(start_date)
    end = datetime.fromisoformat(month_end)

    while current < end:
        if is_working_day(current.date().isoformat()):
            working_days += 1
        current += timedelta(days=1)

    # Aggregate attendance logs for the month
    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": f"{year}-{month_num:02d}-01", "$lt": month_end}
        }},
        {"$project": {
            "status": 1,
            "late_minutes": 1,
            "overtime_minutes": 1,
            "half_day": 1,
            "is_weekday": {"$cond": [
                {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                True, False
            ]}
        }},
        {"$group": {
            "_id": None,
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            "present_days": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$eq": ["$is_weekday", True]}
                    ]},
                    {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                    0
                ]
            }}
        }}
    ]

    result = list(db.attendance_logs.aggregate(pipeline))
    stats = result[0] if result else {
        "leave_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0,
        "present_days": 0.0
    }

    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up((stats["present_days"] / working_days) * 100)

    return EmployeeMonthly(
        emp_code=emp_code,
        month=month,
        working_days=working_days,
        present_days=round_half_up(stats["present_days"]),
        leave_days=stats["leave_days"],
        late_count=stats["late_count"],
        total_late_minutes=stats["total_late_minutes"],
        total_overtime_minutes=stats["total_overtime_minutes"],
        attendance_pct=attendance_pct
    )


@app.get("/analytics/departments/summary", response_model=DepartmentSummary)
def department_summary(
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    department: Optional[str] = None
):
    """Department summary for a month."""
    year, month_num = map(int, month.split("-"))
    if month_num == 12:
        month_end = f"{year + 1}-01-01"
    else:
        month_end = f"{year}-{month_num + 1:02d}-01"
    month_start = f"{year}-{month_num:02d}-01"

    # Filter for department if specified
    dept_filter = {"department": department} if department else {}

    # Get headcount per department (R9: employees joined on or before month end)
    pipeline_employees = [
        {"$match": {
            "joined_on": {"$lte": month_end},
            **dept_filter
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1}
        }}
    ]

    dept_headcounts = {doc["_id"]: doc["headcount"] for doc in db.employees.aggregate(pipeline_employees)}

    if not dept_headcounts:
        return DepartmentSummary(month=month, items=[])

    # Aggregate attendance logs
    pipeline_logs = [
        {"$match": {
            "date": {"$gte": month_start, "$lt": month_end}
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"},
        {"$match": dept_filter if department else {}},
        {"$project": {
            "department": "$emp.department",
            "status": 1,
            "work_hours": 1,
            "late_minutes": 1,
            "is_weekday": {"$cond": [
                {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                True, False
            ]},
            "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            "half_day": 1
        }},
        {"$group": {
            "_id": "$department",
            "present_days": {"$sum": {
                "$cond": [
                    {"$and": [{"$eq": ["$is_present", True]}, {"$eq": ["$is_weekday", True]}]},
                    {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                    0
                ]
            }},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            "work_hours_sum": {"$sum": {"$cond": [
                {"$and": [{"$eq": ["$is_present", True]}, {"$ne": ["$work_hours", None]}]},
                "$work_hours", 0
            ]}},
            "work_hours_count": {"$sum": {"$cond": [
                {"$and": [{"$eq": ["$is_present", True]}, {"$ne": ["$work_hours", None]}]},
                1, 0
            ]}}
        }}
    ]

    log_stats = {doc["_id"]: doc for doc in db.attendance_logs.aggregate(pipeline_logs)}

    # Combine results
    items = []
    for dept, headcount in sorted(dept_headcounts.items()):
        if headcount == 0:
            continue

        stats = log_stats.get(dept, {
            "present_days": 0.0,
            "late_count": 0,
            "total_late_minutes": 0,
            "leave_count": 0,
            "on_duty_count": 0,
            "work_hours_sum": 0,
            "work_hours_count": 0
        })

        avg_work_hours = None
        if stats["work_hours_count"] > 0:
            avg_work_hours = round_half_up(stats["work_hours_sum"] / stats["work_hours_count"])

        items.append(DepartmentSummaryItem(
            department=dept,
            headcount=headcount,
            present_days=round_half_up(stats["present_days"]),
            avg_work_hours=avg_work_hours,
            late_count=stats["late_count"],
            total_late_minutes=stats["total_late_minutes"],
            leave_count=stats["leave_count"],
            on_duty_count=stats["on_duty_count"]
        ))

    items.sort(key=lambda x: x.department)
    return DepartmentSummary(month=month, items=items)


@app.get("/analytics/leaderboard/late", response_model=Leaderboard)
def late_leaderboard(
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    """Late-comers leaderboard with competition ranking."""
    year, month_num = map(int, month.split("-"))
    if month_num == 12:
        month_end = f"{year + 1}-01-01"
    else:
        month_end = f"{year}-{month_num + 1:02d}-01"
    month_start = f"{year}-{month_num:02d}-01"

    # Aggregate late minutes per employee
    pipeline = [
        {"$match": {
            "date": {"$gte": month_start, "$lt": month_end}
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"},
        {"$match": {"department": department} if department else {}},
        {"$group": {
            "_id": "$emp_code",
            "name": {"$first": "$emp.name"},
            "department": {"$first": "$emp.department"},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}}
    ]

    results = list(db.attendance_logs.aggregate(pipeline))

    # Apply competition ranking
    ranked = []
    current_rank = 1
    prev_late = None
    prev_rank = 1

    for i, row in enumerate(results):
        if prev_late is not None and row["total_late_minutes"] < prev_late:
            current_rank = i + 1

        if current_rank <= limit:
            ranked.append(LeaderboardItem(
                rank=current_rank,
                emp_code=row["_id"],
                name=row["name"],
                department=row["department"],
                total_late_minutes=row["total_late_minutes"],
                late_count=row["late_count"]
            ))

        prev_late = row["total_late_minutes"]
        prev_rank = current_rank

    return Leaderboard(month=month, items=ranked)


@app.get("/analytics/departments/{department}/trend", response_model=Trend)
def department_trend(
    department: str,
    from_date: str = Query(..., alias='from'),
    to_date: str = Query(..., alias='to')
):
    """Daily attendance trend for a department with gap-filling and moving average."""
    # Validate department exists
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(404, "department not found")

    if from_date > to_date:
        raise HTTPException(422, "from date cannot be greater than to date")

    # Validate range <= 92 days
    from_dt = datetime.fromisoformat(from_date)
    to_dt = datetime.fromisoformat(to_date)
    if (to_dt - from_dt).days > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")

    # Use $densify to fill gaps
    pipeline = [
        # First, get daily attendance stats
        {"$match": {
            "date": {"$gte": from_date, "$lte": to_date}
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$project": {
            "date": 1,
            "status": 1,
            "late_minutes": 1,
            "half_day": 1,
            "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
        }},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {
                "$cond": [{"$eq": ["$is_present", True]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]}, 0]
            }},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}
        }},
        {"$sort": {"_id": ASCENDING}},
        # Densify to fill missing days
        {"$densify": {
            "field": "_id",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [from_date, to_date]
            }
        }},
        # Fill nulls with defaults
        {"$set": {
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]}
        }},
        # Add working day flag
        {"$addFields": {
            "is_working_day": {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$_id"}}}, 6]}
        }},
        # Lookup headcount for each day
        {"$lookup": {
            "from": "employees",
            "let": {"date": "$_id"},
            "pipeline": [
                {"$match": {
                    "department": department,
                    "joined_on": {"$lte": "$$date"}
                }},
                {"$count": "headcount"}
            ],
            "as": "hc"
        }},
        {"$set": {"headcount": {"$arrayElemAt": ["$hc.headcount", 0]}}},
        {"$set": {"headcount": {"$ifNull": ["$headcount", 0]}}},
        # Calculate attendance rate
        {"$addFields": {
            "attendance_rate": {
                "$cond": [
                    {"$and": [{"$eq": ["$is_working_day", True]}, {"$gt": ["$headcount", 0]}]},
                    {"$divide": ["$present_count", "$headcount"]},
                    None
                ]
            }
        }},
        # Calculate 7-day moving average
        {"$setWindowFields": {
            "sortBy": {"_id": 1},
            "output": {
                "prev_rates": {
                    "$collect": {
                        "input": "$attendance_rate",
                        "into": "rates"
                    }
                }
            }
        }}
    ]

    # Since $setWindowFields with $collect is complex, we'll compute moving average in Python
    # Simpler approach: get daily data first, then compute moving average
    daily_pipeline = [
        {"$match": {
            "date": {"$gte": from_date, "$lte": to_date}
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$project": {
            "date": 1,
            "status": 1,
            "late_minutes": 1,
            "half_day": 1,
            "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
        }},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {
                "$cond": [{"$eq": ["$is_present", True]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]}, 0]
            }},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}
        }},
        {"$sort": {"_id": ASCENDING}}
    ]

    daily_stats = {doc["_id"]: doc for doc in db.attendance_logs.aggregate(daily_pipeline)}

    # Get headcount for each day
    headcounts = {}
    current = from_dt
    while current <= to_dt:
        date_str = current.date().isoformat()
        hc = db.employees.count_documents({
            "department": department,
            "joined_on": {"$lte": date_str}
        })
        headcounts[date_str] = hc
        current += timedelta(days=1)

    # Build trend items
    items = []
    rates = []

    current = from_dt
    while current <= to_dt:
        date_str = current.date().isoformat()
        is_working = is_working_day(date_str)
        headcount = headcounts[date_str]

        stats = daily_stats.get(date_str, {"present_count": 0, "late_count": 0})
        present_count = stats["present_count"]
        late_count = stats["late_count"]

        # Calculate attendance rate
        attendance_rate = None
        if is_working and headcount > 0:
            attendance_rate = round_half_up_rate(present_count / headcount)
            rates.append(attendance_rate)
        else:
            rates.append(None)

        # Calculate 7-day moving average
        moving_avg = None
        window = [r for r in rates[-7:] if r is not None]
        if window:
            moving_avg = round_half_up_rate(sum(window) / len(window))

        items.append(TrendItem(
            date=date_str,
            is_working_day=is_working,
            headcount=headcount,
            present_count=round_half_up(present_count),
            late_count=late_count,
            attendance_rate=attendance_rate,
            moving_avg_7d=moving_avg
        ))

        current += timedelta(days=1)

    return Trend(department=department, items=items)


# --------------------------------------------------------------------------- #
# Admin Endpoints
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", response_model=ExplainResponse)
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias='from'),
    to_date: Optional[str] = Query(None, alias='to'),
    page: Optional[int] = None,
    page_size: Optional[int] = None
):
    """MongoDB explain for an endpoint's main query."""
    valid_endpoints = ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
    if endpoint not in valid_endpoints:
        raise HTTPException(422, f"invalid endpoint: {endpoint}")

    collection = "attendance_logs"
    pipeline = None

    if endpoint == "attendance_list":
        collection = "attendance_logs"
        q = {}
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

        skip = (page - 1) * page_size if page and page_size else 0
        limit_val = page_size if page_size else 20

        pipeline = [
            {"$match": q},
            {"$sort": {"date": -1, "emp_code": 1}},
            {"$skip": skip},
            {"$limit": limit_val}
        ]

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month required for employee_monthly")

        year, month_num = map(int, month.split("-"))
        if month_num == 12:
            month_end = f"{year + 1}-01-01"
        else:
            month_end = f"{year}-{month_num + 1:02d}-01"

        pipeline = [
            {"$match": {
                "emp_code": emp_code,
                "date": {"$gte": f"{year}-{month_num:02d}-01", "$lt": month_end}
            }},
            {"$project": {
                "status": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "half_day": 1,
                "is_weekday": {"$cond": [
                    {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                    True, False
                ]}
            }},
            {"$group": {
                "_id": None,
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                "present_days": {"$sum": {
                    "$cond": [
                        {"$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$eq": ["$is_weekday", True]}
                        ]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                        0
                    ]
                }}
            }}
        ]

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month required for department_summary")

        year, month_num = map(int, month.split("-"))
        if month_num == 12:
            month_end = f"{year + 1}-01-01"
        else:
            month_end = f"{year}-{month_num + 1:02d}-01"
        month_start = f"{year}-{month_num:02d}-01"

        dept_filter = {"department": department} if department else {}

        pipeline = [
            {"$match": {
                "date": {"$gte": month_start, "$lt": month_end}
            }},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp"
            }},
            {"$unwind": "$emp"},
            {"$match": dept_filter if department else {}},
            {"$project": {
                "department": "$emp.department",
                "status": 1,
                "work_hours": 1,
                "late_minutes": 1,
                "is_weekday": {"$cond": [
                    {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                    True, False
                ]},
                "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "half_day": 1
            }},
            {"$group": {
                "_id": "$department",
                "present_days": {"$sum": {
                    "$cond": [
                        {"$and": [{"$eq": ["$is_present", True]}, {"$eq": ["$is_weekday", True]}]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                        0
                    ]
                }},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                "work_hours_sum": {"$sum": {"$cond": [
                    {"$and": [{"$eq": ["$is_present", True]}, {"$ne": ["$work_hours", None]}]},
                    "$work_hours", 0
                ]}},
                "work_hours_count": {"$sum": {"$cond": [
                    {"$and": [{"$eq": ["$is_present", True]}, {"$ne": ["$work_hours", None]}]},
                    1, 0
                ]}}
            }}
        ]

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month required for late_leaderboard")

        year, month_num = map(int, month.split("-"))
        if month_num == 12:
            month_end = f"{year + 1}-01-01"
        else:
            month_end = f"{year}-{month_num + 1:02d}-01"
        month_start = f"{year}-{month_num:02d}-01"

        pipeline = [
            {"$match": {
                "date": {"$gte": month_start, "$lt": month_end}
            }},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp"
            }},
            {"$unwind": "$emp"},
            {"$match": {"department": department} if department else {}},
            {"$group": {
                "_id": "$emp_code",
                "name": {"$first": "$emp.name"},
                "department": {"$first": "$emp.department"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}
            }},
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}}
        ]

    elif endpoint == "department_trend":
        if not from_date or not to_date:
            raise HTTPException(422, "from and to required for department_trend")

        pipeline = [
            {"$match": {
                "date": {"$gte": from_date, "$lte": to_date}
            }},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp"
            }},
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
            {"$project": {
                "date": 1,
                "status": 1,
                "late_minutes": 1,
                "half_day": 1,
                "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
            }},
            {"$group": {
                "_id": "$date",
                "present_count": {"$sum": {
                    "$cond": [{"$eq": ["$is_present", True]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]}, 0]
                }},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}
            }},
            {"$sort": {"_id": 1}}
        ]

    explain = db[collection].aggregate(pipeline).explain()
    # Convert MongoDB explain to serializable format
    explain_dict = convert_bson(explain)

    return ExplainResponse(
        endpoint=endpoint,
        collection=collection,
        explain=explain_dict
    )


def convert_bson(obj):
    """Convert BSON types to Python native types for JSON serialization."""
    if isinstance(obj, dict):
        return {k: convert_bson(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_bson(item) for item in obj]
    elif hasattr(obj, 'isoformat'):  # datetime
        return obj.isoformat()
    elif hasattr(obj, '__int__'):  # ObjectId, Int64, etc.
        return int(obj)
    elif hasattr(obj, '__float__'):
        return float(obj)
    else:
        return obj
