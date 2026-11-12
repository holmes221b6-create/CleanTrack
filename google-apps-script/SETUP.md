# MECHNAR Google Apps Script mail relay

The backend uses this Web App for password recovery and operation-review notifications.

## 1. Apps Script deployment

1. Open the Apps Script project using the Google account `mail.mechnar.business@gmail.com`.
2. Replace the editor contents with `Code.gs` from this folder.
3. Open **Project Settings -> Script Properties** and add:
   - `MECHNAR_SECRET`: a long random secret. Use the exact same value in Render.
   - `MECHNAR_SENDER_EMAIL`: `mail.mechnar.business@gmail.com`
4. Save the project.
5. Choose **Deploy -> New deployment -> Web app**:
   - Execute as: **Me**
   - Who has access: **Anyone**
6. Authorize the Gmail sending permission when Google prompts.
7. Copy the deployed Web App URL ending in `/exec`.

If you edit the script after deploying, create a new version through **Deploy -> Manage deployments -> Edit -> New version -> Deploy**. Keep the same deployment URL when editing the existing deployment.

## 2. Render backend environment

In the MECHNAR backend service, add these environment variables:

- `MECHNAR_MAIL_WEBAPP_URL`: the deployed Apps Script Web App URL ending in `/exec`
- `MECHNAR_SECRET`: the same random secret stored in Apps Script properties
- `MECHNAR_MAIL_FROM_EMAIL`: `mail.mechnar.business@gmail.com`

Save the changes and allow Render to redeploy.

The backend intentionally treats Apps Script as the selected provider when either relay variable is present. If the relay is misconfigured or fails, it reports a delivery failure instead of silently sending through Resend/SMTP and potentially creating duplicate emails.

## 3. Sender identity

Apps Script sends mail as the Google account that owns the Web App deployment when **Execute as: Me** is selected. Therefore, deploy while signed into `mail.mechnar.business@gmail.com`. The script verifies the configured business sender and checks the effective account when Google exposes it.

## 4. End-to-end test

1. Trigger a password-recovery email to an address you control.
2. Check the inbox and spam folder; confirm the sender is `mail.mechnar.business@gmail.com`.
3. Submit a test operation as Staff and approve/reject it as the assigned Supervisor.
4. Confirm the registered Staff email receives the outcome and message.
5. If delivery fails, inspect the Render backend logs and Apps Script **Executions** page. Never paste the secret into GitHub, chat, or source code.
