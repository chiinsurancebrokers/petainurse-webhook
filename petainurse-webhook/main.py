"""
PetAiNurse — Stripe Webhook Service
FastAPI microservice που δέχεται Stripe events και ενημερώνει
τη Supabase `subscriptions` table αυτόματα.

Events που χειρίζεται:
  - customer.subscription.created  → plan='insurance', valid_until=period_end
  - customer.subscription.updated  → ανανέωση/αλλαγή plan
  - customer.subscription.deleted  → plan='free', valid_until=None
  - invoice.payment_failed         → log μόνο (δεν ακυρώνει αμέσως)
"""

import os, logging
from datetime import datetime, timezone

import stripe
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse
from supabase import create_client, Client

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("webhook")

# ── Env vars ──────────────────────────────────────────────────────────────────
STRIPE_SECRET_KEY      = os.environ["STRIPE_SECRET_KEY"]
STRIPE_WEBHOOK_SECRET  = os.environ["STRIPE_WEBHOOK_SECRET"]
SUPABASE_URL           = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY   = os.environ["SUPABASE_SERVICE_KEY"]   # service_role key!
STRIPE_PRICE_MONTHLY   = os.environ.get("STRIPE_PRICE_MONTHLY", "")
STRIPE_PRICE_YEARLY    = os.environ.get("STRIPE_PRICE_YEARLY", "")

stripe.api_key = STRIPE_SECRET_KEY

app = FastAPI(title="PetAiNurse Webhook", version="1.0.0")

def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def get_customer_email(customer_id: str) -> str:
    """Ανακτά email από Stripe customer."""
    try:
        customer = stripe.Customer.retrieve(customer_id)
        return customer.get("email", "")
    except Exception as e:
        log.error(f"Failed to retrieve customer {customer_id}: {e}")
        return ""


def upsert_subscription(email: str, plan: str, valid_until=None, notes: str = ""):
    """Γράφει/ενημερώνει εγγραφή στο Supabase subscriptions table."""
    if not email:
        log.warning("upsert_subscription called with empty email — skipping")
        return
    sb = get_supabase()
    row = {
        "user_email":  email,
        "plan":        plan,
        "valid_until": valid_until,
        "notes":       notes,
    }
    try:
        sb.table("subscriptions").upsert(row, on_conflict="user_email").execute()
        log.info(f"Subscription upserted: {email} → plan={plan}, valid_until={valid_until}")
    except Exception as e:
        log.error(f"Supabase upsert failed for {email}: {e}")
        raise


def ts_to_iso(unix_ts) -> str | None:
    """Μετατρέπει Unix timestamp σε ISO 8601 string (UTC)."""
    if not unix_ts:
        return None
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat()


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "petainurse-webhook"}


@app.get("/subscription/{email}")
def check_subscription(email: str):
    """Admin endpoint: έλεγχος συνδρομής για email."""
    sb = get_supabase()
    try:
        res = (sb.table("subscriptions")
                 .select("*")
                 .eq("user_email", email)
                 .limit(1)
                 .execute())
        rows = res.data or []
        if not rows:
            return {"email": email, "found": False}
        return {"email": email, "found": True, "subscription": rows[0]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(None, alias="stripe-signature"),
):
    """
    Κύριο Stripe webhook endpoint.
    Επαληθεύει την υπογραφή και χειρίζεται τα events.
    """
    payload = await request.body()

    # ── Signature verification ─────────────────────────────────────────────
    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, STRIPE_WEBHOOK_SECRET
        )
    except stripe.error.SignatureVerificationError as e:
        log.warning(f"Invalid Stripe signature: {e}")
        raise HTTPException(status_code=400, detail="Invalid signature")
    except Exception as e:
        log.error(f"Webhook parse error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

    event_type = event["type"]
    data       = event["data"]["object"]
    log.info(f"Received event: {event_type} | id={event.get('id')}")

    # ── customer.subscription.created ─────────────────────────────────────
    if event_type == "customer.subscription.created":
        email       = get_customer_email(data["customer"])
        valid_until = ts_to_iso(data.get("current_period_end"))
        price_id    = data["items"]["data"][0]["price"]["id"] if data.get("items") else ""
        plan_label  = "insurance_monthly" if price_id == STRIPE_PRICE_MONTHLY else "insurance_yearly"
        upsert_subscription(
            email, plan="insurance",
            valid_until=valid_until,
            notes=f"Stripe sub created | price={price_id} | plan={plan_label}"
        )

    # ── customer.subscription.updated (ανανέωση, αλλαγή plan) ────────────
    elif event_type == "customer.subscription.updated":
        email       = get_customer_email(data["customer"])
        status      = data.get("status", "")
        valid_until = ts_to_iso(data.get("current_period_end"))
        price_id    = data["items"]["data"][0]["price"]["id"] if data.get("items") else ""
        if status in ("active", "trialing"):
            upsert_subscription(
                email, plan="insurance",
                valid_until=valid_until,
                notes=f"Stripe sub updated | status={status} | price={price_id}"
            )
        else:
            # past_due, unpaid, paused κλπ → υποβαθμισμός
            upsert_subscription(
                email, plan="free",
                valid_until=None,
                notes=f"Stripe sub status={status} — downgraded"
            )

    # ── customer.subscription.deleted (ακύρωση) ────────────────────────────
    elif event_type == "customer.subscription.deleted":
        email = get_customer_email(data["customer"])
        upsert_subscription(
            email, plan="free",
            valid_until=None,
            notes="Stripe sub cancelled"
        )

    # ── invoice.payment_failed ─────────────────────────────────────────────
    elif event_type == "invoice.payment_failed":
        customer_id = data.get("customer", "")
        email       = get_customer_email(customer_id)
        log.warning(f"Payment failed for {email} | invoice={data.get('id')}")
        # Δεν ακυρώνουμε αμέσως — το Stripe θα στείλει subscription.updated/deleted
        # αν αποτύχουν και οι retry attempts.

    else:
        log.info(f"Unhandled event type: {event_type} — ignored")

    return JSONResponse({"received": True, "event": event_type})
