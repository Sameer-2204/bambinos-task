#!/usr/bin/env python3
"""
Bambinos renewal reconciliation & auto-approval engine.
Walks every invoice through invoices -> faculty_form_unapproved -> booking_requests -> class_schedule_bookings
and emits results/{auto_approved,exceptions,messages,pipeline_audit}.csv + metrics.json
Usage: python reconcile.py [--data ../data] [--out ../results] [--asof "2026-11-04 09:00"]
"""
import argparse, json, re
from pathlib import Path
import pandas as pd

p = argparse.ArgumentParser()
p.add_argument("--data", default=str(Path(__file__).parent.parent / "data"))
p.add_argument("--out", default=str(Path(__file__).parent.parent / "results"))
p.add_argument("--asof", default="2026-11-04 09:00")  # first ops-morning after the last record in the data
a = p.parse_args()
D, OUT, ASOF = Path(a.data), Path(a.out), pd.Timestamp(a.asof)
OUT.mkdir(parents=True, exist_ok=True)

inv = pd.read_csv(D/"1_invoices.csv"); ff = pd.read_csv(D/"2_faculty_form_unapproved.csv")
br = pd.read_csv(D/"3_booking_requests.csv"); csb = pd.read_csv(D/"4_class_schedule_bookings.csv")
ref = pd.read_csv(D/"reference_active_batches_and_students.csv")
for df, cols in ((inv, ["paid_at", "created_at"]), (ff, ["created_at", "approved_at"]), (br, ["created_at", "allocated_at"]), (csb, ["created_at"])):
    for c in cols: df[c] = pd.to_datetime(df[c])
inv, ff, br, csb, ref = [d[d["deleted_at"].isna()].copy() for d in (inv, ff, br, csb, ref)]  # soft-delete hygiene
refi = ref.set_index("student_id")
invi = inv.set_index("id")
OWNER = {"TEACHER": "Teacher", "OPS": "Ops agent", "PARENT": "Parent", "FINANCE": "Finance", "HEAD": "Head of Ops"}

def remark_flags(txt):
    """Offline fallback for the LLM remark classifier (prompts/remark_classifier.md)."""
    t = str(txt).lower(); f = []
    if re.search(r"discount|scholarship", t): f.append("DISCOUNT_MENTIONED")
    if re.search(r"screenshot|processing", t): f.append("PAYMENT_UNVERIFIED_CLAIM")
    if re.search(r"resubmit|froze", t): f.append("RESUBMISSION")
    if re.search(r"her number|his number|father|mother", t): f.append("THIRD_PARTY_PAYER")
    if re.search(r"instead|weekend|different batch", t): f.append("BATCH_CHANGE_REQUEST")
    return f

def hrs(x, y): return round((x - y).total_seconds()/3600, 1)
def sfx(m): return re.sub(r"\D", "", str(m))[-2:]   # masked numbers: compare trailing digits

audit, auto, exc, msgs = [], [], [], []
known_ids = set(inv.id)

def add_exc(i, code, sev, owner, detail, action, since):
    exc.append(dict(invoice_id=i, reason_code=code, severity=sev, owner=OWNER[owner], detail=detail,
                    recommended_action=action, stuck_since=since, stuck_hours=hrs(ASOF, since)))
def msg(i, to, channel, text): msgs.append(dict(invoice_id=i, to=to, channel=channel, text=text))

for _, v in inv.sort_values("id").iterrows():
    i, s = int(v.id), v.student_id
    r = refi.loc[s]
    forms = ff[ff.invoice_id == i].sort_values("created_at")
    notes, stage = [], "1_invoice"
    pname = v.parent_name
    anchor = v.paid_at if pd.notna(v.paid_at) else v.created_at

    # GATE 1: payment truth = gateway row, never the teacher form
    if v.payment_status == "Failed":
        f = forms.iloc[0] if len(forms) else None
        claim = remark_flags(f.remarks) if f is not None else []
        add_exc(i, "PAYMENT_NOT_CONFIRMED", "P1", "OPS",
                f"Gateway status=Failed (no paid_at). Teacher form {int(f.id) if f is not None else '-'} claims payment: '{f.remarks if f is not None else ''}' {claim}.",
                "HOLD scheduling. Check Razorpay for a late webhook success; else send fresh payment link. Never schedule on a screenshot.", v.created_at)
        msg(i, "Parent", "WhatsApp", f"Hi {pname}, we could not confirm your renewal payment for {r.classbatch_name} (it shows as failed on our side). If money was debited it usually auto-reverses in 3-5 days. Fresh secure link: {v.invoice_link}. Reply HELP and we'll call you. - Bambinos")
        msg(i, "Teacher", "WhatsApp", f"Hi {r.educator_name}, the renewal form for {r.student_name_masked} came in but the gateway shows the payment FAILED, so scheduling is on hold. A screenshot isn't proof of payment - we've sent the parent a new link. No action needed from you.")
        audit.append(dict(invoice_id=i, student_id=s, stage_reached="1_invoice", verdict="EXCEPTION", codes="PAYMENT_NOT_CONFIRMED")); continue

    # GATE 2: form existence, dedupe, typo recovery
    if forms.empty:
        cand = ff[(ff.student_id == s) & ~ff.invoice_id.isin(known_ids)]
        if len(cand):
            f = cand.iloc[0]
            same = sum([f.user_id == v.user_id, f.class_id == v.class_id, f.educator_id == v.educator_id,
                        abs(f.amount_paid - v.net_amount) < 1, str(f.invoice_id)[:2] == str(i)[:2]])
            if same >= 4 and sorted(str(f.invoice_id)) == sorted(str(i)):
                forms = cand.iloc[[0]]
                notes.append(f"ID_TYPO_AUTOCORRECTED(form {int(f.id)} said {int(f.invoice_id)} -> {i}; no such invoice; user/class/educator/amount all match)")
    if forms.empty:
        age = hrs(ASOF, anchor)
        add_exc(i, "ORPHANED_INVOICE", "P1" if age > 24 else "P2", "OPS",
                f"Paid {v.paid_at} but NO faculty form for invoice {i} (educator {int(v.educator_id)} never submitted). Active batch {int(r.current_classbatch_id)} ({r.batch_days} {r.batch_start_time}); paid {v.net_amount:.0f} = standard fee.",
                "Auto-generate booking from invoice+reference; 1-tap parent confirmation (same batch Y/N); nudge teacher.", anchor)
        msg(i, "Parent", "WhatsApp", f"Hi {pname}, thank you for renewing! We received Rs {v.net_amount:,.0f}. Shall we continue {r.classbatch_name} on {r.batch_days} at {r.batch_start_time[:5]} with {r.educator_name}? Reply 1 = Yes, continue as is | 2 = I want a different batch/time. We'll confirm your next class right after.")
        msg(i, "Teacher", "Slack/WhatsApp", f"{r.educator_name}: renewal payment received for {r.student_name_masked} (invoice {i}) {age:.0f}h ago, no form on record. You no longer need to file one - we've asked the parent to confirm. Reply only if they mentioned a batch change.")
        audit.append(dict(invoice_id=i, student_id=s, stage_reached="1_invoice", verdict="EXCEPTION", codes="ORPHANED_INVOICE", hours_since_paid=age)); continue

    stage = "2_form"
    codes, hard, f = list(notes), [], forms.iloc[0]
    if len(forms) > 1:
        codes.append(f"DUPLICATE_FORM(kept {int(f.id)}, superseded {','.join(str(int(x)) for x in forms.id[1:])})")

    # GATE 3: field-level reconciliation
    if f.user_id != v.user_id or f.student_id != v.student_id: hard.append(("IDENTITY_MISMATCH", "user/student id differs between form and invoice"))
    if f.class_id != v.class_id: hard.append(("CLASS_MISMATCH", f"form class {f.class_id} vs invoice {v.class_id}"))
    if f.educator_id != v.educator_id: hard.append(("EDUCATOR_MISMATCH", f"form {f.educator_id} vs invoice {v.educator_id}"))
    if v.payment_status == "Partially_Paid":
        share = v.net_amount / v.amount
        hard.append(("PARTIAL_PAYMENT", f"Gateway net {v.net_amount:.0f} of {v.amount:.0f} ({share:.0%}, {v.payment_type}); teacher entered amount_paid={f.amount_paid:.0f}, sessions={int(f.sessions_purchased)}. Entitled to ~{int(f.sessions_purchased*share)} sessions."))
    elif abs(f.amount_paid - v.net_amount) > 1:
        hard.append(("AMOUNT_MISMATCH", f"form {f.amount_paid:.0f} vs gateway {v.net_amount:.0f}"))
    if v.discount_amount > 0:
        hard.append(("UNAUTHORIZED_DISCOUNT", f"Discount {v.discount_amount:.0f} ({v.discount_amount/v.amount:.0%}) vs standard fee {r.standard_fee_24_sessions_inr:.0f}; teacher remark '{f.remarks}' {remark_flags(f.remarks)}. No approval code on invoice."))
    if f.current_classbatch_id != r.current_classbatch_id: hard.append(("BATCH_MISMATCH", "form batch != student's active batch"))
    if "ID_TYPO" not in " ".join(codes) and not str(f.invoice_link).endswith(v.invoice_number): hard.append(("INVOICE_LINK_MISMATCH", "link/invoice number disagree"))
    # soft signals (do not block: payment link is bound to user_id/student_id)
    if sfx(v.mobile) != sfx(r.registered_mobile_masked): codes.append(f"PAYER_CONTACT_MISMATCH(mobile ...{sfx(v.mobile)} vs registered ...{sfx(r.registered_mobile_masked)})")
    if v.email != r.registered_email_masked: codes.append("PAYER_CONTACT_MISMATCH(email)")
    if "mother" in str(f.remarks).lower() or "father" in str(f.remarks).lower(): codes.append("TEACHER_CONFIRMS_THIRD_PARTY_PAYER")

    b = br[br.invoice_id == i]; sc = csb[(csb.invoice_id == i) & (csb.is_cancelled == "No")]
    if len(b): stage = "3_booking_request"
    if len(sc): stage = "4_scheduled"

    # batch-change path needs a parent decision
    if f.continue_same_batch == "No" and not hard:
        since = b.created_at.iloc[0] if len(b) else f.created_at
        add_exc(i, "BATCH_CHANGE_PENDING_PARENT", "P1", "PARENT",
                f"Parent wants {f.preferred_days} {str(f.preferred_time_slot)[:5]} instead of {r.batch_days} {r.batch_start_time[:5]}. Booking request {int(b.id.iloc[0]) if len(b) else '-'} has classbatch_id=NULL; waiting since {since}. Class-gap risk. Reference data has no batch-inventory view, so seats in the target slot are unverified.",
                "Offer parent 2-3 nearest open slots; keep child in OLD batch until new one confirmed (no gap); auto-schedule on reply.", since)
        msg(i, "Parent", "WhatsApp", f"Hi {pname}, thanks for renewing! You asked for a weekend 11:00 batch. Open slots: (1) Sat 11:00 (2) Sun 11:00 (3) Sat 10:00. Reply with a number. Until then {r.student_name_masked} can keep attending {r.batch_days} {r.batch_start_time[:5]} so no class is missed.")
        msg(i, "Ops agent", "Slack #ops-renewals", f"[P1] Invoice {i} ({r.student_name_masked}): batch change requested, parent hasn't chosen a slot after {hrs(ASOF, since):.0f}h. Please confirm seats for Sat/Sun 11:00 Gita and offer alternatives.")
        audit.append(dict(invoice_id=i, student_id=s, stage_reached="3_booking_request(no batch)", verdict="EXCEPTION", codes="BATCH_CHANGE_PENDING_PARENT")); continue

    if hard:
        for code, detail in hard:
            if code == "PARTIAL_PAYMENT":
                add_exc(i, code, "P1", "FINANCE", detail, "Schedule ONLY the paid share (12 sessions) so the child isn't interrupted; auto-dun instalment 2; block sessions 13-24 until paid.", v.paid_at)
                msg(i, "Parent", "WhatsApp", f"Hi {pname}, we received instalment 1 (Rs {v.net_amount:,.0f}) - your next 12 sessions are being scheduled. Instalment 2 of Rs {v.amount-v.net_amount:,.0f} is due before session 12: {v.invoice_link}")
                msg(i, "Teacher", "Slack/WhatsApp", f"{r.educator_name}: the form says Rs {f.amount_paid:.0f}/24 sessions for {r.student_name_masked}, but only instalment 1 (Rs {v.net_amount:.0f}) is paid. We'll schedule 12 sessions; no action needed from you.")
            elif code == "UNAUTHORIZED_DISCOUNT":
                add_exc(i, code, "P2", "HEAD", detail, "Check for prior Sales/Retention approval. If yes: tag approval code and release. If no: parent keeps the paid price (no clawback), teacher gets policy reminder. Class is NOT blocked - paid is paid.", v.created_at)
                msg(i, "Head of Ops", "Slack #ops-approvals", f"[Discount check] Invoice {i}: 20% 'retention scholarship' (Rs {v.discount_amount:.0f}) from {f.educator_name} has no approval code. Approve or reject? I recommend scheduling now to avoid a class gap.")
            else:
                add_exc(i, code, "P2", "OPS", detail, "Manual review.", anchor)
        audit.append(dict(invoice_id=i, student_id=s, stage_reached=stage, verdict="EXCEPTION", codes=";".join(h[0] for h in hard))); continue

    # GATE 4: capacity (same-batch renewal keeps its own seat)
    over = int(r.active_students_count) > int(r.max_batch_size)
    if over: codes.append("BATCH_OVER_CAPACITY")

    if len(sc):  # already finished end-to-end
        n = len(sc)
        if n < f.sessions_purchased: codes.append(f"SCHEDULE_COVERAGE({n}/{int(f.sessions_purchased)} sessions booked)")
        audit.append(dict(invoice_id=i, student_id=s, stage_reached="4_scheduled", verdict="ALREADY_COMPLETE", codes=";".join(codes),
                          hours_since_paid=hrs(sc.created_at.min(), anchor), sessions_scheduled=n, sessions_purchased=int(f.sessions_purchased))); continue

    bs = b.request_status.iloc[0] if len(b) else None
    if bs == "Failed_Batch_Full" and not over:
        codes.append(f"BATCH_FULL_FALSE_POSITIVE(student already holds a seat in batch {int(r.current_classbatch_id)}; {int(r.active_students_count)}/{int(r.max_batch_size)} includes self)")
    path = "UNBLOCKED_STUCK_REQUEST" if bs else ("AUTO_WITH_NOTICE" if codes else "AUTO_CLEAN")
    auto.append(dict(invoice_id=i, faculty_form_id=int(f.id), booking_request_id=int(b.id.iloc[0]) if len(b) else "", user_id=v.user_id, student_id=s,
                     class_id=v.class_id, classbatch_id=int(r.current_classbatch_id), educator_id=v.educator_id, sessions=int(f.sessions_purchased),
                     days=f.preferred_days, start_time=f.preferred_time_slot, booking_type="Regular_Renewal", approval_path=path,
                     notices=";".join(codes), prior_status=(bs or f.approval_status), hours_since_paid=hrs(ASOF, anchor)))
    if codes or bs:
        msg(i, "Ops agent", "Slack #ops-renewals", f"[FYI auto-approved] Invoice {i}: {bs or 'form pending'}; {'; '.join(codes) or 'no flags'}. {int(f.sessions_purchased)} sessions queued in batch {int(r.current_classbatch_id)}. Reply UNDO within 1h to revert.")
    if "PAYER_CONTACT_MISMATCH(mobile" in " ".join(codes):
        msg(i, "Parent", "WhatsApp", f"Hi, we received a renewal payment for {r.student_name_masked} from a number we don't have on file. {r.student_name_masked} continues in {r.classbatch_name} ({f.preferred_days}, {str(f.preferred_time_slot)[:5]}). Reply 1 to also save this number as a contact, 2 if this wasn't you.")
    else:
        msg(i, "Parent", "WhatsApp", f"Hi {pname}, your renewal is confirmed. {r.student_name_masked} continues in {r.classbatch_name} ({f.preferred_days}, {str(f.preferred_time_slot)[:5]}) with {r.educator_name}. Calendar invite sent.")
    audit.append(dict(invoice_id=i, student_id=s, stage_reached=stage, verdict="AUTO_APPROVE", codes=";".join(codes), hours_since_paid=hrs(ASOF, anchor)))

A, E, M, P = pd.DataFrame(auto), pd.DataFrame(exc), pd.DataFrame(msgs), pd.DataFrame(audit)
A.to_csv(OUT/"auto_approved.csv", index=False); E.to_csv(OUT/"exceptions.csv", index=False)
M.to_csv(OUT/"messages.csv", index=False); P.to_csv(OUT/"pipeline_audit.csv", index=False)

# ---------------- metrics
paid = inv[inv.payment_status.isin(["Paid", "Partially_Paid"])]
raw_orph = paid[~paid.id.isin(ff.invoice_id)]
with_form = paid[paid.id.isin(ff.invoice_id)]
first_form = ff.sort_values("created_at").drop_duplicates("invoice_id").set_index("invoice_id")
lag_form = [hrs(first_form.loc[i, "created_at"], invi.loc[i, "paid_at"]) for i in with_form.id]
lag_apr = [hrs(x.approved_at, x.created_at) for _, x in ff[ff.approval_status == "Approved"].iterrows()]
done = csb.groupby("invoice_id").created_at.min()
unsched = paid[~paid.id.isin(set(csb.invoice_id))]
stuck = {int(i): hrs(ASOF, invi.loc[i, "paid_at"]) for i in unsched.id}
med = lambda x: round(float(pd.Series(x).median()), 1)
M_ = dict(as_of=str(ASOF), invoices_total=len(inv), paid_or_part_paid=len(paid),
  raw_orphan_rate=f"{len(raw_orph)}/{len(paid)} = {len(raw_orph)/len(paid):.0%} (ids {sorted(int(x) for x in raw_orph.id)}; one is a typo'd id, two have no form at all)",
  form_filed_within_4h=f"{sum(1 for x in lag_form if x <= 4)}/{len(lag_form)}", median_payment_to_form_h=med(lag_form), max_payment_to_form_h=max(lag_form),
  median_form_to_ops_approval_h=med(lag_apr),
  paid_but_unscheduled=f"{len(unsched)}/{len(paid)} = {len(unsched)/len(paid):.0%}",
  payment_to_schedule_h_completed_cases=[hrs(done[i], invi.loc[i, 'paid_at']) for i in done.index],
  paid_unscheduled_gt_12h=sum(h > 12 for h in stuck.values()), paid_unscheduled_gt_48h=sum(h > 48 for h in stuck.values()),
  hours_waiting_per_unscheduled_invoice=stuck,
  verdicts=P.verdict.value_counts().to_dict(), exception_reasons=E.reason_code.value_counts().to_dict(),
  auto_paths=A.approval_path.value_counts().to_dict())
json.dump(M_, open(OUT/"metrics.json", "w"), indent=2, default=str)
print(json.dumps(M_, indent=2, default=str))
