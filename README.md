# Bambinos AI PM (Ops) - Renewal -> Scheduling Reconciliation Engine
Prototype for Deliverable C + supporting artifacts for A, B, D. Case report: `Case_Report.pdf`.

## Run
```bash
pip install pandas
cd workflows_or_scripts && python reconcile.py --asof "2026-11-04 09:00"
```
Reads `data/` (the 5 official CSVs), writes `results/`:
| File | Contents |
|---|---|
| `auto_approved.csv` | 6 renewals ready for `class_schedule_bookings` (1 clean, 3 with notice, 2 unblocked stuck requests) |
| `exceptions.csv` | 6 flagged items: reason code, severity, single owner, recommended action, hours stuck |
| `messages.csv` | drafted WhatsApp / Slack messages per case |
| `pipeline_audit.csv` | every invoice, stage reached, verdict |
| `metrics.json` | orphan rate, TAT, stuck counts |

## Design notes
- Deterministic rules decide; the LLM only classifies free-text remarks (`prompts/remark_classifier.md`), explains and routes (`triage_agent.md`) and drafts (`message_drafter.md`). An offline regex fallback is in the script.
- Payment truth is the gateway row on `invoices`; form text/screenshots are never trusted.
- Paid children are never left without a class: partial payment schedules the paid share, batch change keeps the old batch until the new one is confirmed.
- `--asof` is a parameter (default: first ops-morning after the last record, 2026-11-04 09:00 IST) so ages are reproducible.

## Limits (honest)
- 13-invoice sample: rules are demonstrated, thresholds are not statistically validated.
- Reference table has no batch inventory, so batch-change seat availability is flagged as unverifiable rather than guessed.
- Scheduling output is a ready-to-insert row set (batch, days, time, sessions); generating individual `class_date` rows needs the live `classschedules` calendar, which is not in the dataset.
- n8n flow is specified (`workflows_or_scripts/n8n_workflow_spec.md`), not exported/tested.
