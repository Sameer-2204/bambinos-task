-- Ops Head morning dashboard (Metabase, MySQL). Parameter: {{asof}} defaults to NOW().
-- 1) Orphaned invoices: paid, no faculty form after 4h
SELECT i.id, i.invoice_number, i.educator_id, i.paid_at, TIMESTAMPDIFF(HOUR,i.paid_at,NOW()) AS hrs_waiting
FROM invoices i LEFT JOIN faculty_form_unapproved f ON f.invoice_id=i.id AND f.deleted_at IS NULL
WHERE i.deleted_at IS NULL AND i.payment_status IN ('Paid','Partially_Paid') AND i.is_renewal='Yes'
  AND f.id IS NULL AND i.paid_at < NOW() - INTERVAL 4 HOUR;

-- 2) Forms whose invoice_id points to no invoice (typos) + near-match suggestion
SELECT f.id AS form_id, f.invoice_id AS typed_invoice_id, i.id AS suggested_invoice_id
FROM faculty_form_unapproved f
LEFT JOIN invoices i0 ON i0.id=f.invoice_id
LEFT JOIN invoices i ON i.student_id=f.student_id AND i.net_amount=f.amount_paid AND i.id<>f.invoice_id
WHERE f.deleted_at IS NULL AND i0.id IS NULL;

-- 3) Duplicate forms per invoice
SELECT invoice_id, COUNT(*) n, GROUP_CONCAT(id) form_ids FROM faculty_form_unapproved
WHERE deleted_at IS NULL GROUP BY invoice_id HAVING n>1;

-- 4) Forms unapproved > 2h
SELECT id, educator_id, invoice_id, TIMESTAMPDIFF(HOUR,created_at,NOW()) hrs FROM faculty_form_unapproved
WHERE approval_status='Pending' AND deleted_at IS NULL AND created_at < NOW() - INTERVAL 2 HOUR;

-- 5) Booking requests not Completed > 2h (incl. Failed_Batch_Full / Awaiting_Parent_Batch_Selection)
SELECT id, invoice_id, request_status, TIMESTAMPDIFF(HOUR,created_at,NOW()) hrs FROM booking_requests
WHERE request_status<>'Completed' AND deleted_at IS NULL AND created_at < NOW() - INTERVAL 2 HOUR;

-- 6) Paid but unscheduled > 12h (headline KPI)
SELECT i.id, i.paid_at, TIMESTAMPDIFF(HOUR,i.paid_at,NOW()) hrs_since_paid FROM invoices i
LEFT JOIN classschedulebookings c ON c.invoice_id=i.id AND c.is_cancelled='No' AND c.deleted_at IS NULL
WHERE i.payment_status IN ('Paid','Partially_Paid') AND i.is_renewal='Yes' AND i.deleted_at IS NULL
  AND c.id IS NULL AND i.paid_at < NOW() - INTERVAL 12 HOUR;

-- 7) Payment-to-Schedule turnaround (median/p90 via Metabase)
SELECT i.id, TIMESTAMPDIFF(MINUTE,i.paid_at,MIN(c.created_at))/60 AS pay_to_schedule_h
FROM invoices i JOIN classschedulebookings c ON c.invoice_id=i.id AND c.deleted_at IS NULL
WHERE i.payment_status IN ('Paid','Partially_Paid') GROUP BY i.id;

-- 8) Sessions purchased vs scheduled (coverage gap)
SELECT f.invoice_id, f.sessions_purchased, COUNT(c.id) scheduled FROM faculty_form_unapproved f
LEFT JOIN classschedulebookings c ON c.invoice_id=f.invoice_id AND c.is_cancelled='No' AND c.deleted_at IS NULL
WHERE f.deleted_at IS NULL GROUP BY f.invoice_id, f.sessions_purchased HAVING scheduled < f.sessions_purchased;
