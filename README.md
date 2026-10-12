# Attendance & Analytics API

```bash
pip install -r requirements.txt
export MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_db   # or use a local .env
uvicorn app.main:app --port 8000
```
MongoDB 6.0+ required (`$setWindowFields`, `$densify`). Indexes are created at startup.

Notes
- Everything is in `app/main.py`; `/admin/explain/*` builds queries with the same functions the endpoints use.
- Derived fields are stored and analytics read the stored values.
- `$round` in MongoDB is banker's rounding, so half-up rounding of reported numbers is done on exact integer sums (cents, half-day units, 1e-4 rate units) in Python at the very end.
- Not tested against a live MongoDB in my environment; the helper rules and validation paths are unit-tested.
# HR-attendance-system
