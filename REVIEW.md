# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health` (line 72-74) | Returns 200 without checking MongoDB connectivity | Endpoint should return 503 if MongoDB is down | Added `db.command('ping')` to verify MongoDB connection |
| 2 | `create_employee` (line 78-85) | No unique index on emp_code, vulnerable to race conditions | Concurrent duplicate creates could both succeed instead of one 409 | Created unique index on emp_code at startup, used try/except for duplicate key error |
| 3 | `create_employee` (line 82) | `created_at` set to naive datetime without timezone | Should be UTC datetime consistent with data model | Used `datetime.now(timezone.utc)` |
| 4 | `create_employee` (line 84) | Returns "id" field which is not in the OpenAPI schema | API contract specifies no id field in response | Removed the "id" assignment, only return fields from schema |
| 5 | `list_employees` (line 93) | Pagination formula is wrong: `skip = page * page_size` should be `(page - 1) * page_size` | Page 1 would skip 20 records instead of 0 | Changed to `skip = (page - 1) * page_size` |
| 6 | `list_employees` (line 94) | `total` counts all documents instead of filtered set | Total should reflect applied department filter | Moved count after filter is applied |
| 7 | `list_employees` (line 95) | No sorting by emp_code as required by spec | Results not in guaranteed order | Added `.sort("emp_code", 1)` |
| 8 | `compute_late_minutes` (line 33-37) | Incorrect late calculation: uses punch_in time as base instead of shift_start on correct date | Late minutes will be wrong for overnight shifts | Reimplemented to calculate shift start as datetime on the punch date in IST |
| 9 | `compute_late_minutes` (line 37) | Grace period check is `> 10` but should be `> 10*60` (10 minutes in seconds) | Would only detect lateness after 10 seconds instead of 10 minutes | Changed comparison to check whole minutes elapsed > 10 |
| 10 | `compute_work_hours` (line 40-41) | Uses Python's round which is banker's rounding, not half-up as required by R4 | 8.125 would round to 8.12 instead of 8.13 | Implemented half-up rounding using Decimal |
| 11 | `compute_overtime` (line 44-47) | Incorrect shift_end calculation: uses `date_str` without considering overnight shifts | Wrong overtime for overnight shifts | Reimplemented to handle overnight shifts correctly using IST |
| 12 | `compute_overtime` (line 47) | Doesn't enforce the 30-minute minimum from R3 | Overtime less than 30 minutes should be 0 | Added check: only count overtime if >= 30 minutes |
| 13 | `punch_in` (line 102) | No validation that punched_at is in valid epoch millisecond range | Invalid timestamps like seconds or floats would be accepted | Added validation: must be between 100000000000 and 4102444800000 |
| 14 | `punch_in` (line 102) | Uses naive datetime conversion without timezone | Datetimes won't be consistent with IST/UTC handling | Convert to UTC datetime properly |
| 15 | `punch_in` (line 103) | Date calculation doesn't account for overnight shift rule R1 | Overnight shift punch-ins would get wrong date | Implemented R1 logic to determine attendance date in IST |
| 16 | `punch_in` (line 104) | No unique index enforcement, race condition possible | Concurrent punch-ins could both succeed | Created compound unique index on (emp_code, date) |
| 17 | `punch_in` (line 119) | Returns "id" field not in schema | API violation | Removed id field from response |
| 18 | `punch_in` (line 110) | Stores punch_in as naive datetime | Should be UTC datetime | Store as UTC datetime |
| 19 | `list_attendance` (line 144) | Loads entire collection into memory before pagination | Will fail or be slow on ~100k records | Use MongoDB skip/limit at database level |
| 20 | `list_attendance` (line 145) | Sorts in Python instead of database | Inefficient for large datasets | Added MongoDB sort |
| 21 | `list_attendance` (line 149) | Returns "id" field not in schema | API violation | Removed id field |
| 22 | Global (line 25) | IST timezone defined but not used consistently in helpers | Inconsistent time zone handling | Centralized IST handling in R1 logic |
| 23 | Global (missing) | No database indexes created at startup | Queries will be slow on large dataset | Added startup event to create all required indexes |
| 24 | Global (missing) | No validation for shift_start != shift_end | Employee creation with same start/end should be invalid | Added validation in create_employee |
| 25 | Global (missing) | Epoch milliseconds not truncated to whole seconds as required by R1 | Punch times with sub-second precision stored incorrectly | Truncate to whole seconds before storing |

**Not a defect:**
- The use of `body.model_dump()` is correct for Pydantic v2
- The basic structure of the starter (single file, FastAPI pattern) is appropriate for this assignment
- Using environment variables with dotenv is the right approach for configuration
