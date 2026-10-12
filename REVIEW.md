# REVIEW.md

Defects found in the starter `app/main.py`. All are fixed in the submitted file.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Late test is `floor(minutes) > 10`, so 09:40:01-09:40:59 (and exactly 10:xx s) is treated as on time; R2 says strictly more than 10:00. | Punch-in 09:40:01 on a 09:30 shift returns 0, expected 10. | Compare elapsed **seconds** `> 600`, then floor to minutes. |
| 2 | `compute_late_minutes` | Builds the shift start with `punch_in.replace(hour=...)` on a UTC/naive datetime, so the shift start is in the wrong zone and on the wrong date. | A 09:28 IST punch (03:58 UTC) is compared with 09:30 *UTC*, so everything looks early and nobody is ever late. | Convert to IST, build the shift start from the attendance date in IST. |
| 3 | `compute_late_minutes` | No overnight handling; the start is always on the punch-in's own calendar day. | Shift 22:00-06:00, punch-in 02:00 IST: attendance date and lateness are both wrong. | `attendance_date()` implements R1; start is built on that date. |
| 4 | `compute_work_hours` | Python `round()` on a float (banker's rounding plus binary float error), no second truncation. | 16182 s = 4.495 h rounds to 4.49, so a wrong `half_day` as well. | `Decimal` division, `ROUND_HALF_UP`; instants truncated to seconds on input. |
| 5 | `compute_overtime` | No 30-minute minimum; uses `datetime.fromisoformat(date_str)` with a hand-set `tzinfo` and no overnight handling; mixing aware and naive datetimes raises `TypeError` -> 500. | Punch-out 18:45 returns 15, expected 0; any call crashes on naive vs aware. | New `compute_overtime` with the threshold, IST shift end on the next day for overnight shifts. |
| 6 | Starter has no `half_day` logic | `half_day` was hard-coded false; R5 is never applied. | Punch-out after 3.5 h still says `half_day: false`. | `derive()` computes all four derived fields in one place. |
| 7 | `create_employee` | Check-then-insert race; no unique index. Concurrent creates both return 201. | 20 parallel POSTs with one `emp_code`. | Unique index on `emp_code`; `insert_one` and catch `DuplicateKeyError` -> 409. |
| 8 | `create_employee` | `EmployeeIn` has no validation: emp_code format, e-mail, shift pattern, real `joined_on`, `shift_start == shift_end`. | `shift_start: "99:99"` is accepted. | Field patterns and validators -> 422. |
| 9 | `create_employee` | `created_at = datetime.now()` is naive local time, and the response returns it as an ISO string, not epoch ms. | `created_at` in the response is a string. | `now_utc()` stored; `emp_out()` returns epoch ms. |
| 10 | `list_employees` | `skip = page * page_size`: page 1 skips the first page. | `GET /employees` (page 1) drops the first 20 rows. | `(page - 1) * page_size`. |
| 11 | `list_employees` | `total` counts the whole collection, ignoring `department`. | `?department=Sales` shows total = all employees. | `count_documents(q)` with the same filter. |
| 12 | `list_employees` | No sort (order is arbitrary), `page`/`page_size` unvalidated (0, negative, huge). | `page_size=100000`, `page=0`. | `.sort("emp_code")`, `Query(ge=1, le=100)`. |
| 13 | `punch_in` | Unknown employee: `emp` is `None` so `emp["shift_start"]` raises `TypeError` -> 500. | Punch in with a bad `emp_code`. | Explicit 404. |
| 14 | `punch_in` | `datetime.fromtimestamp(ms/1000)` uses the **server's local zone** and a float; `.date()` is not the IST date and ignores overnight (R1). Seconds-looking, float and string values are not rejected. | Server in UTC: punch at 00:30 IST gets yesterday's date. | `from_ms()` (UTC, truncated); strict `EpochMs` type -> 422; `attendance_date()`. |
| 15 | `punch_in` | Check-then-insert race on (`emp_code`, `date`). | Two parallel punch-ins both get 201 and create duplicates. | Unique index `(emp_code, date)`; `DuplicateKeyError` -> 409. |
| 16 | `punch_in` | `status` is a free string, so `ABSENT`, `LEAVE` or junk is accepted. | `{"status": "banana"}` gets 201. | `Literal["PRESENT","WFH","ON_DUTY"]`. |
| 17 | `punch_in` / `list_attendance` | Response returns `id` (the `_id`), and `punch_in` as a datetime string, not epoch ms. | Look at any response. | `record_out()`: no `id`, epoch ms, defaults for legacy documents. |
| 18 | `list_attendance` | `find(q)` loads the whole collection into memory, sorts and slices in Python; no indexes. | 100k records: slow and memory-heavy. | Server-side sort/skip/limit, backed by indexes. |
| 19 | `list_attendance` | Sorts by `date` only (no `emp_code` tiebreak); `date_from > date_to`, bad dates, bad `status`, page bounds are not validated. | Same-day rows reorder between pages; `date_from=junk` returns 200. | Sort `(date desc, emp_code asc)`, 422 on bad input. |
| 20 | `list_attendance` | `d.pop("_id")` on legacy docs and `d["history"]`, `half_day` are returned as stored (may be missing). | Legacy record without `history` produces a response missing required fields. | `record_out()` defaults. |
| 21 | `/health` | Always returns ok, even if MongoDB is down. | Stop Mongo; health still 200. | `ping`; 503 on failure. |
| 22 | Client | Default PyMongo returns naive datetimes. | Arithmetic with aware datetimes raises. | `MongoClient(tz_aware=True)`. |
| 23 | Startup | No indexes, and none created at startup. | `/admin/explain` shows COLLSCAN. | `ensure_indexes()` in the lifespan, idempotent. |

**Looked at and judged not a defect:**
- `doc.pop("_id")` after `insert_one` in `create_employee`: `insert_one` adds `_id` to the dict, so the pop is correct.
- Reading `MONGO_URI` / `MONGO_DB` with `load_dotenv()`: the default does not override real environment variables, which is what the contract asks for.
- `IST = timezone(timedelta(hours=5, minutes=30))`: correct, India has no DST, so a fixed offset is enough.
- Helpers were reused only in spirit; their signatures changed because they needed the attendance date and could not be trusted as written.
