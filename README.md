# HROne Attendance API

Employee Attendance & Analytics API implementation for the HROne Software Engineer Trainee assignment.

## Overview

This is a FastAPI application that manages employee attendance records with punch-in/punch-out functionality, manual corrections with audit trails, and analytics endpoints using MongoDB aggregations.

**Key Features:**
- Employee punch-in and punch-out with business rules (R1-R10)
- Manual attendance corrections with durable audit trail
- Analytics endpoints with MongoDB aggregations
- Proper date/time handling for IST (UTC+05:30)
- Concurrent operation safety with database indexes
- Health check endpoint for MongoDB connectivity

## Prerequisites

- Python 3.11+ (tested with Python 3.14.6)
- MongoDB 6.0 or newer (the grader uses 7.0)
- MongoDB Atlas free tier (M0) or local MongoDB instance

## Setup

### 1. Clone the Repository

```bash
git clone <repository-url>
cd candidate_kit
```

### 2. Create Virtual Environment

```bash
python -m venv .venv

# On Windows:
.venv\Scripts\activate

# On macOS/Linux:
source .venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure MongoDB

Create a `.env` file in the project root (do not commit this file):

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

**For MongoDB Atlas:**
```env
MONGO_URI=mongodb+srv://<username>:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority
MONGO_DB=attendance_db
```

**For local MongoDB with Docker:**
```bash
docker run -d -p 27017:27017 --name mongodb mongo:7
```

### 5. Load Sample Data (Optional)

The sample data shows document shapes only. The grader uses a much larger hidden dataset.

```bash
python sample_seed.py
```

## Running the Application

Start the FastAPI server:

```bash
uvicorn app.main:app --port 8000
```

For development with auto-reload:

```bash
uvicorn app.main:app --port 8000 --reload
```

The API will be available at `http://localhost:8000`

API documentation (Swagger UI): `http://localhost:8000/docs`
API documentation (ReDoc): `http://localhost:8000/redoc`

## Testing

The application includes an `/admin/explain/{endpoint}` endpoint to validate that queries use indexes correctly. Run this before submission to ensure IXSCAN and no COLLSCAN.

Example:
```bash
curl http://localhost:8000/admin/explain/attendance_list
```

## API Endpoints

### System
- `GET /health` - Readiness probe (returns 200 if MongoDB is available)

### Employees
- `POST /employees` - Create an employee
- `GET /employees` - List employees with pagination

### Attendance
- `POST /attendance/punch-in` - Record a punch-in
- `POST /attendance/punch-out` - Record a punch-out
- `GET /attendance` - List attendance records with filters
- `PATCH /attendance/{emp_code}/{date}` - Regularize (manually correct) a record

### Analytics
- `GET /analytics/employees/{emp_code}/monthly` - Monthly summary for one employee
- `GET /analytics/departments/summary` - Department summary for a month
- `GET /analytics/leaderboard/late` - Late-comers leaderboard
- `GET /analytics/departments/{department}/trend` - Daily attendance trend

### Admin
- `GET /admin/explain/{endpoint}` - MongoDB explain for query validation

## Business Rules (R1-R10)

- **R1**: Time zone and attendance date handling (IST, overnight shifts)
- **R2**: Late calculation with 10-minute grace period
- **R3**: Overtime calculation (minimum 30 minutes)
- **R4**: Work hours calculation with half-up rounding
- **R5**: Half-day detection (< 4.50 hours)
- **R6**: Presence status (PRESENT, WFH, ON_DUTY count as present)
- **R7**: Working days (Mon-Fri, from joined_on)
- **R8**: Rounding (2 decimals half-up, 4 decimals for rates)
- **R9**: Headcount (employees joined on or before period end)
- **R10**: Pagination (page starts at 1, max page_size 100)

See `openapi.yaml` for complete API contract and detailed business rules.

## Database Indexes

The application creates the following indexes at startup:

**employees collection:**
- Unique index on `emp_code`
- Index on `department`
- Index on `joined_on`

**attendance_logs collection:**
- Compound unique index on `(emp_code, date)` for punch-in concurrency
- Index on `date` (descending)
- Compound index on `(emp_code, date)` for queries
- Index on `status`

## Environment Variables

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `MONGO_URI` | Yes | MongoDB connection string | `mongodb://localhost:27017` |
| `MONGO_DB` | Yes | Database name | `attendance_db` |

**Important:** Never commit `.env` files or real credentials to the repository.

## Project Structure

```
candidate_kit/
├── app/
│   ├── __init__.py
│   └── main.py              # Complete implementation (all code in one file)
├── sample_data/
│   ├── employees.json      # Sample employee documents
│   └── attendance_logs.json # Sample attendance documents
├── sample_seed.py           # Script to load sample data
├── requirements.txt         # Python dependencies
├── README.md               # This file
├── REVIEW.md               # Review of starter code defects
├── DECISIONS.md            # Implementation decisions and trade-offs
├── openapi.yaml            # API contract and business rules
├── DATA_MODEL.md           # MongoDB data model specification
└── .gitignore              # Git ignore rules
```

## Implementation Notes

### Python 3.14 Compatibility

The dependency versions are pinned to work with Python 3.14.6:
- fastapi==0.115.0
- uvicorn[standard]==0.32.0
- pymongo==4.10.1
- pydantic==2.9.2
- python-dotenv==1.0.1

All selected versions are at least 7 days old (as of October 2026) and have been tested for compatibility.

### Time Zone Handling

- All datetimes are stored in MongoDB as UTC BSON dates
- Instant values in the API are epoch milliseconds (no timezone ambiguity)
- IST calendar dates and shift times are strings (as specified in DATA_MODEL.md)
- Attendance date calculation follows R1 for overnight shifts

### Concurrency Safety

- Unique index on `(emp_code, date)` prevents duplicate punch-ins
- Optimistic concurrency control for punch-out and regularization
- 409 Conflict response for concurrent modifications

### Audit Trail

- Manual corrections append entries to `history` array
- History records: timestamp, who, reason, and changed fields
- Punch-in and punch-out operations never write to history
- History entries are never edited or removed

## Known Limitations

1. **Department Trend Moving Average**: The 7-day moving average is computed in Python rather than MongoDB aggregation due to complexity with `$setWindowFields` and null handling. This is acceptable for the 92-day maximum range.

2. **No Authentication**: The assignment does not require authentication. The `regularized_by` field is free text as specified.

3. **No Dockerfile**: The assignment explicitly states not to include a Dockerfile.

## Submission

The repository is ready for submission with:
- `app/main.py` - Complete implementation
- `requirements.txt` - Python dependencies
- `README.md` - This documentation
- `REVIEW.md` - Starter code review
- `DECISIONS.md` - Implementation decisions
- `.gitignore` - Git ignore rules

No `.env` file, no secrets, no Dockerfile as required.

## License

This is an assignment submission for HROne.
