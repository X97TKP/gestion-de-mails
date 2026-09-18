import base64
import email
import os
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv
import json
import imaplib
import re
import smtplib
load_dotenv()
# --- CONFIGURATION MULTI-COMPTES ---
raw_accounts = os.getenv("ACCOUNTS")

ACCOUNTS = json.loads(raw_accounts) if raw_accounts else {}
    


def get_account_list():
  """Renvoie la liste des comptes disponibles pour l'interface."""
  return [
      {"key": k, "label": v["label"], "email": v["email"]}
      for k, v in ACCOUNTS.items()
  ]


def clean_header(val):
  if not val:
    return ""
  decoded_parts = decode_header(val)
  result = []
  for decoded, enc in decoded_parts:
    if isinstance(decoded, bytes):
      result.append(decoded.decode(enc or "utf-8", errors="ignore"))
    else:
      result.append(str(decoded))
  return "".join(result)


def get_latest_emails(account_key="pro", limit=20):
  """Récupère les 20 derniers e-mails avec conversion fiable des images en base64."""
  if account_key not in ACCOUNTS:
    account_key = "pro"

  acc = ACCOUNTS[account_key]
  emails_list = []

  try:
    mail = imaplib.IMAP4_SSL(acc["imap_server"], acc["imap_port"])
    mail.login(acc["email"], acc["password"])
    mail.select("INBOX")

    _, search_data = mail.search(None, "ALL")
    mail_ids = search_data[0].split()

    recent_ids = mail_ids[-limit:] if len(mail_ids) >= limit else mail_ids

    for m_id in reversed(recent_ids):
      _, data = mail.fetch(m_id, "(RFC822)")
      for response_part in data:
        if isinstance(response_part, tuple):
          msg = email.message_from_bytes(response_part[1])
          subject = clean_header(msg.get("Subject"))
          sender = clean_header(msg.get("From"))

          text_content = ""
          html_content = ""
          images = {}

          if msg.is_multipart():
            for part in msg.walk():
              content_type = part.get_content_type().lower()

              if content_type.startswith("image/"):
                cid = part.get("Content-Id") or part.get("Content-ID")
                img_bytes = part.get_payload(decode=True)
                if cid and img_bytes:
                  cid_clean = cid.strip("<>").strip()
                  b64_data = base64.b64encode(img_bytes).decode("utf-8")
                  images[cid_clean] = f"data:{content_type};base64,{b64_data}"

              if content_type == "text/html" and not html_content:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                if payload:
                  html_content = payload.decode(charset, errors="ignore")
              elif content_type == "text/plain" and not text_content:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                if payload:
                  text_content = payload.decode(charset, errors="ignore")
          else:
            charset = msg.get_content_charset() or "utf-8"
            payload = msg.get_payload(decode=True)
            if payload:
              decoded = payload.decode(charset, errors="ignore")
              if msg.get_content_type() == "text/html":
                html_content = decoded
              else:
                text_content = decoded

          # Remplacement sécurisé des images inline
          if html_content and images:
            for cid_clean, data_uri in images.items():
              html_content = re.sub(
                  rf'src=["\']cid:<?{re.escape(cid_clean)}>?(["\'])',
                  rf'src="{data_uri}\1',
                  html_content,
                  flags=re.IGNORECASE,
              )

          if not html_content:
            html_content = (
                "<body style='font-family:sans-serif; color:#111;"
                f" background:#fff; white-space:pre-wrap;'>{text_content}</body>"
            )

          plain_summary = text_content
          if not plain_summary and html_content:
            plain_summary = re.sub("<[^<]+?>", " ", html_content)

          emails_list.append({
              "id": m_id.decode(),
              "from": sender,
              "subject": subject,
              "html_body": html_content,
              "body": plain_summary[:1500],
          })
    mail.logout()
  except Exception as e:
    print(f"Erreur IMAP ({acc['email']}) : {e}")
  return emails_list


def send_email(account_key, to_email, subject, body_text):
  """Envoie un e-mail avec le compte choisi."""
  if account_key not in ACCOUNTS:
    account_key = "pro"

  acc = ACCOUNTS[account_key]

  try:
    msg = MIMEMultipart()
    msg["From"] = acc["email"]
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain", "utf-8"))

    with smtplib.SMTP_SSL(acc["smtp_server"], acc["smtp_port"]) as server:
      server.login(acc["email"], acc["password"])
      server.send_message(msg)
    return True, "Email envoyé avec succès"
  except Exception as e:
    return False, str(e)