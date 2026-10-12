# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (create guarantee, lookups), `(department, emp_code)` (list, trend), `(joined_on, department)` (headcount). `attendance_logs`: unique `(emp_code, date)` (race guard, per-employee queries), `(date, emp_code)` (list and month scans), `(status, date, emp_code)` (status filter), `(emp_code, punch_in)` (finding the open record to punch out, even across midnight). I rejected a separate index on `date` alone, since `(date, emp_code)` already covers it.

2. **Punch-in race.** Both requests find the employee, compute the same date, then call `insert_one`. The unique `(emp_code, date)` index lets MongoDB accept one; the other gets `DuplicateKeyError`, which I turn into 409. No check-then-insert.

3. **Ties.** `$setWindowFields` with `$rank` ranks by total late minutes, so ties share a rank and the next is skipped. I filter `rank <= limit` after ranking, so a tie at the cutoff returns extra rows, ordered by `emp_code`.

4. **Headcount.** The summary starts from the logs, then `$unionWith` adds a separate headcount count from `employees` (`joined_on <= month end`). Employees with no logs are counted there.

5. **100x data.** I would pre-aggregate a monthly per-employee rollup, updated on punch and correction, instead of scanning raw logs on each analytics call.
