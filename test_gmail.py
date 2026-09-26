import base64
from email.message import EmailMessage

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

creds = Credentials.from_authorized_user_file(
    "token.json",
    SCOPES
)

service = build("gmail", "v1", credentials=creds)

message = EmailMessage()
message["To"] = "oninoroger@gmail.com"
message["Subject"] = "CleanTrack Gmail API Test"
message.set_content("This is a test email from CleanTrack.")

encoded_message = base64.urlsafe_b64encode(
    message.as_bytes()
).decode()

service.users().messages().send(
    userId="me",
    body={"raw": encoded_message}
).execute()

print("EMAIL SENT SUCCESSFULLY")