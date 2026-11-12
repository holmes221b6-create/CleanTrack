/**
 * MECHNAR transactional email relay.
 *
 * Deploy this Apps Script as a Web App:
 *   Execute as: Me (the mechnar.business@gmail.com account)
 *   Who has access: Anyone
 *
 * In Project Settings -> Script Properties, add:
 *   MECHNAR_SECRET = the same long random secret stored in Render
 *   MECHNAR_SENDER_EMAIL = mail.mechnar.business@gmail.com
 *
 * The actual From account is the Google account that owns the deployment.
 * This script verifies that the effective account matches the configured sender.
 */
function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      return jsonResponse({ success: false, error: "Missing request body." });
    }

    var payload = JSON.parse(e.postData.contents);
    var props = PropertiesService.getScriptProperties();
    var expectedSecret = props.getProperty("MECHNAR_SECRET") || "";
    var configuredSender = (props.getProperty("MECHNAR_SENDER_EMAIL") || "").toLowerCase().trim();
    var effectiveSender = (Session.getEffectiveUser().getEmail() || "").toLowerCase().trim();

    if (!expectedSecret || !payload.secret || payload.secret !== expectedSecret) {
      return jsonResponse({ success: false, error: "Unauthorized relay request." });
    }

    if (!configuredSender || configuredSender !== "mail.mechnar.business@gmail.com") {
      return jsonResponse({ success: false, error: "Business sender is not configured correctly." });
    }

    if (effectiveSender && effectiveSender !== configuredSender) {
      return jsonResponse({ success: false, error: "Web App is not executing as the MECHNAR business account." });
    }

    var recipient = String(payload.to || payload.email || "").trim();
    var subject = String(payload.subject || "MECHNAR notification").trim();
    var textBody = String(payload.text || payload.code || "").trim();
    var htmlBody = String(payload.html || "").trim();

    if (!recipient || !textBody) {
      return jsonResponse({ success: false, error: "Recipient and message body are required." });
    }

    MailApp.sendEmail({
      to: recipient,
      subject: subject,
      body: textBody,
      htmlBody: htmlBody || undefined,
      name: "MECHNAR Business",
      replyTo: configuredSender
    });

    return jsonResponse({ success: true });
  } catch (err) {
    console.error("MECHNAR relay error: " + (err && err.message ? err.message : err));
    return jsonResponse({ success: false, error: "Email relay failed." });
  }
}

function jsonResponse(value) {
  return ContentService
    .createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}
