import imaplib
import email
import os
import tempfile
from email.header import decode_header
from datetime import datetime, timedelta

def connect_imap(email_address, app_password, imap_server="imap.gmail.com",
                 timeout=15):
    """Connect to the IMAP server over SSL and log in.

    A connect timeout keeps a hung or unreachable mail server from blocking the
    request indefinitely — this feature is optional and must never freeze the app.
    """
    mail = imaplib.IMAP4_SSL(imap_server, timeout=timeout)
    mail.login(email_address, app_password)
    return mail

def fetch_receipt_attachment(mail, merchant_keyword, days_back=7):
    """
    Search the inbox for an email from a specific merchant within the last X days
    and download its PDF or Image attachment to a temporary file.
    
    Returns the path to the downloaded temporary file, or None if not found.
    """
    mail.select("inbox")

    # Calculate the date strictly for IMAP SEARCH (e.g. "01-Sep-2026")
    date_since = (datetime.now() - timedelta(days=days_back)).strftime("%d-%b-%Y")

    # Strip the two characters that are special inside an IMAP quoted string so a
    # merchant keyword can never break out of the BODY "..." literal. Callers pass
    # keywords from a fixed list today, but this keeps the search safe regardless.
    safe_keyword = merchant_keyword.replace('\\', '').replace('"', '').strip()
    if not safe_keyword:
        return None

    # Search for emails whose body mentions the merchant since the cutoff date.
    status, messages = mail.search(None, f'(SINCE "{date_since}" BODY "{safe_keyword}")')
    
    if status != "OK" or not messages[0]:
        return None
        
    email_ids = messages[0].split()
    # We only process the most recent email matching the keyword to save time
    latest_email_id = email_ids[-1]
    
    status, msg_data = mail.fetch(latest_email_id, "(RFC822)")
    if status != "OK":
        return None
        
    for response_part in msg_data:
        if isinstance(response_part, tuple):
            msg = email.message_from_bytes(response_part[1])
            
            if msg.is_multipart():
                for part in msg.walk():
                    # If this part is an attachment
                    if part.get_content_maintype() == 'multipart':
                        continue
                    if part.get('Content-Disposition') is None:
                        continue
                        
                    filename = part.get_filename()
                    if filename:
                        # Decode the filename if it's encoded
                        decoded_parts = decode_header(filename)
                        filename = decoded_parts[0][0]
                        if isinstance(filename, bytes):
                            filename = filename.decode()
                            
                        # Only accept images or PDFs
                        ext = os.path.splitext(filename)[1].lower()
                        if ext in ['.pdf', '.jpg', '.jpeg', '.png', '.heic']:
                            # Save to a temporary file
                            fd, temp_path = tempfile.mkstemp(suffix=ext)
                            with os.fdopen(fd, 'wb') as f:
                                f.write(part.get_payload(decode=True))
                            return temp_path
                            
    return None
