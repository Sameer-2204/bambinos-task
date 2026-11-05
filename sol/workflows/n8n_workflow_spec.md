# n8n / Make workflow spec (build order)
Trigger A: Razorpay/Stripe webhook `payment.captured` (+ nightly sweep of `invoices` for missed webhooks)
1. Filter is_renewal=Yes -> look up reference row (student_id).
2. Gate: payment_status (Paid -> continue | Partially_Paid -> branch P | Failed -> branch F).
3. Pull any faculty form for invoice; if none -> branch O (orphan: create booking_request from invoice+reference, send parent 1-tap WhatsApp).
4. Run rule engine (reconcile.py logic as a Code node / Python microservice): ID, amount, discount, contact, duplicates, typo.
5. Outcome router:
   - AUTO_CLEAN / AUTO_WITH_NOTICE -> insert booking_requests (Completed) + class_schedule_bookings; WhatsApp parent; Slack FYI w/ UNDO (1h).
   - EXCEPTION -> LLM triage (prompts/triage_agent.md) -> owner-specific message -> Slack #ops-renewals + row in "Exceptions" sheet with SLA timer.
6. Timers: Wait 4h -> if exception unresolved, re-ping owner; 12h -> escalate to Head of Ops; 24h -> call parent.
7. Nightly: refresh Metabase dashboard (dashboard_queries.sql); 9am IST Slack digest to Head of Ops.
Shadow mode flag: when SHADOW=true, steps 5 write to `shadow_*` tables and Slack only; no live writes.
