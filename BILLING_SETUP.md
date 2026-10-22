# CleanTrack Billing foundation

## What is implemented

- One persisted subscription record per organisation.
- Server-side 14-day Premium trial with a single-use trial marker and automatic status expiry when Billing is next requested.
- Admin-only endpoints to read subscription status, start a trial, and save billing contact/legal/GST details.
- Trial start requires saved billing contact name, valid billing email, legal/organisation name, and billing address; GSTIN remains optional.
- One trial is enforced per organisation, so creating another Admin account in the same organisation does not grant another trial.
- Server-side Premium gating for advanced Analytics and its exports, using the active subscription/trial entitlement.
- Razorpay subscription creation, checkout signature verification, signed webhook validation and provider lifecycle handling for successful charges, failed/halted subscriptions and cancellation.
- PayPal subscription approval flow, server-side webhook signature verification through PayPal, and lifecycle handling for completed sales, failed payments and cancellation/expiry.
- Frontend Billing page connects to both provider endpoints. Razorpay Checkout supports merchant-enabled payment methods such as UPI and net banking; availability depends on the merchant account and subscription eligibility.

## Important limitations

- Final prices are intentionally unset. No price or charge amount is invented.
- Provider checkout is guarded by `BILLING_CHECKOUT_ENABLED=false` by default. Keep it false until test credentials, plan IDs, prices, webhook delivery and lifecycle tests are verified; only then set it to `true` deliberately.
- Provider endpoints have been committed but cannot be tested against your merchant accounts until credentials and plan IDs are configured. Live checkout is not confirmed or enabled by this code commit alone.
- Billing history is empty until provider-backed invoice retrieval is implemented.
- Premium feature enforcement is partial: advanced Analytics endpoints and exports are gated. Other proposed Premium limits (such as extended audit history and integrations) are not yet enforced, and core operational endpoints remain available according to existing permissions.
- No card details are stored by CleanTrack.

## Razorpay configuration

Set these as private backend environment variables in the deployment environment, never in frontend JavaScript or committed source:

- `RAZORPAY_KEY_ID`
- `RAZORPAY_KEY_SECRET`
- `RAZORPAY_PLAN_MONTHLY_ID`
- `RAZORPAY_PLAN_ANNUAL_ID`
- `RAZORPAY_WEBHOOK_SECRET`

Create the monthly and annual subscription plans in the Razorpay dashboard after pricing and billing terms are approved. Configure the webhook URL as:

`https://<your-backend-host>/api/billing/webhook/razorpay`

Also set `BILLING_CHECKOUT_ENABLED=false` until sandbox tests cover successful first payment, failed payment, renewal, cancellation, expiry, duplicate webhook delivery, invalid webhook signatures, and payment/refund edge cases. The provider webhook must be configured to deliver subscription and payment events. Only after these tests and pricing approval should the switch be set to `true`.

## API endpoints

- `GET /api/billing/current` — Admin-only current subscription, entitlements, provider readiness, and pricing status.
- `POST /api/billing/trial/start` — Admin-only 14-day trial start. Accepts `{"billing_interval":"monthly"}` or `{"billing_interval":"annual"}` and requires the four mandatory billing fields to have already been saved.
- `PATCH /api/billing/billing-details` — Admin-only billing contact, legal name, address, and optional GSTIN.
- `POST /api/billing/webhook/razorpay` — public webhook endpoint requiring a valid `X-Razorpay-Signature` and `X-Razorpay-Event-Id`.

## Deployment and verification

The backend imports all SQLAlchemy models and calls `db.create_all()` at application startup, so the new subscription/event tables are created additively on startup. Verify the deployment logs and call `GET /api/billing/current` as an Admin after deployment. Do not describe paid payment processing as live until sandbox transactions and webhook lifecycle tests have passed.


## Planned payment methods

The Billing UI now connects to the Razorpay and PayPal checkout endpoints. Razorpay can present merchant-enabled UPI and net-banking methods; PayPal subscriptions require eligible merchant access and a supported non-INR currency. Checkout remains disabled until provider configuration, pricing approval and sandbox tests are complete. PayPal availability for recurring business subscriptions must be confirmed for the intended merchant country and account.


## PayPal configuration

Set these private backend environment variables:

- `PAYPAL_CLIENT_ID`
- `PAYPAL_CLIENT_SECRET`
- `PAYPAL_PLAN_MONTHLY_ID`
- `PAYPAL_PLAN_ANNUAL_ID`
- `PAYPAL_WEBHOOK_ID`
- `PAYPAL_MODE=sandbox` while testing; set to `live` only for the approved live merchant account.
- `PAYPAL_CURRENCY=USD` when the PayPal plans are priced in USD.
- `CLEANTRACK_PUBLIC_URL` to the public CleanTrack frontend base URL.
- `BILLING_CHECKOUT_ENABLED=false` until provider setup and sandbox verification are complete.

Register the webhook URL as `https://<your-backend-host>/api/billing/webhook/paypal`. PayPal subscription support is restricted to eligible merchant/partner setups, and PayPal recurring subscriptions do not support INR. Confirm that your account is eligible and create matching supported-currency plans before enabling the PayPal button. Razorpay is the intended path for INR checkout and merchant-enabled UPI/net-banking methods.

## Implemented checkout endpoints

- `POST /api/billing/checkout/razorpay/create` — Admin-only creation of a provider subscription.
- `POST /api/billing/checkout/razorpay/verify` — Admin-only checkout signature and payment-state verification.
- `POST /api/billing/checkout/paypal/create` — Admin-only PayPal subscription creation and approval URL.
- `POST /api/billing/webhook/razorpay` — signature-verified Razorpay lifecycle events.
- `POST /api/billing/webhook/paypal` — PayPal REST API signature verification and lifecycle events.

Manual bank transfer is not the same as net banking checkout. It requires verified beneficiary details, proof-of-payment handling and a reconciliation workflow; do not advertise it as available until those are configured.
