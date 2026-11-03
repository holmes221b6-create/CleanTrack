"""Manual Gmail API smoke-test script.

This file is intentionally safe for pytest discovery: it does not import
Google client libraries, read credentials, or send email when imported.
Run it directly only when deliberately testing a configured Gmail API account.
"""


def main():
    import base64
    from email.message import EmailMessage

    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/gmail.send"]
    credentials = Credentials.from_authorized_user_file("token.json", scopes)
    service = build("gmail", "v1", credentials=credentials)

    recipient = input("Recipient email address: ").strip()
    if not recipient or "@" not in recipient:
        raise ValueError("Enter a valid recipient email address.")

    message = EmailMessage()
    message["To"] = recipient
    message["Subject"] = "CleanTrack Gmail API Test"
    message.set_content("This is a manually requested Gmail API test.")

    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": encoded}).execute()
    print("Email sent successfully.")


if __name__ == "__main__":
    main()
