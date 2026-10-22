# CleanTrack Billing foundation

## What is implemented

- One persisted subscription record per organisation.
- Server-side 14-day Premium trial with a single-use trial marker and automatic status expiry when Billing is next requested.
- Admin-only endpoints to read subscription status, start a trial, and save billing contact/legal/GST details.
- Trial start requires saved billing contact name, valid billing email, legal/organisation name, and billing address; GSTIN remains optional.
- One trial is enforced per organisation, so creating another Admin account in the same organisation does not grant another trial.
- Server-side Premium gating for advanced Analytics and its exports, using the active subscription/trial entitlement.
- Razorpay webhook signature verification and an idempotency ledger for verified webhook event IDs.
- Frontend Billing page with monthly/annual selection, Free/Premium comparison, trial status, editable billing details, and honest provider readiness messaging.

## Important limitations

- Final prices are intentionally unset. No price or charge amount is invented.
- Razorpay checkout is hard-disabled in this foundation, even if environment variables are present.
- The webhook endpoint records signature-verified events but intentionally does not change subscription state. Event-to-subscription lifecycle mapping must be implemented and tested before paid entitlements can be granted.
- Billing history is empty until provider-backed invoice retrieval is implemented.
- Premium feature enforcement is partial: advanced Analytics endpoints and exports are gated. Other proposed Premium limits (such as extended audit history and integrations) are not yet enforced, and core operational endpoints remain available according to existing permissions.
- No card details are stored by CleanTrack.

## Razorpay configuration required before checkout can be enabled

Set these as private backend environment variables in the deployment environment, never in frontend JavaScript or committed source:

- `RAZORPAY_KEY_ID`
- `RAZORPAY_KEY_SECRET`
- `RAZORPAY_PLAN_MONTHLY_ID`
- `RAZORPAY_PLAN_ANNUAL_ID`
- `RAZORPAY_WEBHOOK_SECRET`

Create the monthly and annual subscription plans in the Razorpay dashboard after pricing and billing terms are approved. Configure the webhook URL as:

`https://<your-backend-host>/api/billing/webhook/razorpay`

Do not enable checkout until sandbox tests cover successful first payment, failed payment, renewal, cancellation, expiry, duplicate webhook delivery, invalid webhook signatures, and payment/refund edge cases. The provider integration must verify payment state server-side before marking an organisation Premium.

## API endpoints

- `GET /api/billing/current` — Admin-only current subscription, entitlements, provider readiness, and pricing status.
- `POST /api/billing/trial/start` — Admin-only 14-day trial start. Accepts `{"billing_interval":"monthly"}` or `{"billing_interval":"annual"}` and requires the four mandatory billing fields to have already been saved.
- `PATCH /api/billing/billing-details` — Admin-only billing contact, legal name, address, and optional GSTIN.
- `POST /api/billing/webhook/razorpay` — public webhook endpoint requiring a valid `X-Razorpay-Signature` and `X-Razorpay-Event-Id`.

## Deployment and verification

The backend imports all SQLAlchemy models and calls `db.create_all()` at application startup, so the new subscription/event tables are created additively on startup. Verify the deployment logs and call `GET /api/billing/current` as an Admin after deployment. Do not describe paid payment processing as live until sandbox transactions and webhook lifecycle tests have passed.


## Planned payment methods

The Billing UI now lists bank transfer/net banking, UPI, and PayPal as planned methods. They are marked Coming soon and cannot initiate payment. Razorpay checkout remains disabled until the provider integration, recurring billing lifecycle, and sandbox tests are complete. PayPal availability for recurring business subscriptions must be confirmed for the intended merchant country and account.
