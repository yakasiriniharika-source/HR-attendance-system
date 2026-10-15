# Attendance & Analytics API

## Run
pip install -r requirements.txt
export MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_db
uvicorn app.main:app --port 8000

MongoDB 6.0+ is required ($setWindowFields, $densify). Indexes are created at startup.

## Notes
- All code is in app/main.py.
- Derived fields are stored; analytics read the stored values.
- $round in MongoDB uses banker's rounding, so half-up rounding is done in Python on exact integer sums.

## Testing
Tested against MongoDB 7: parallel creates, punch-ins, punch-outs and corrections; overnight shifts; the trend endpoint (gaps, weekends, shorter moving-average windows); and about 100,000 records, where all five explain outputs show IXSCAN and no COLLSCAN.

## Missing
Nothing known.
