"""In-process stand-ins for a CalDAV server and an IMAP/SMTP mail server."""

import base64
import email
import email.policy
import re
from email.message import EmailMessage

import httpx

from app.services.integrations.ical import parse_events

USER, PASSWORD = "robin", "app-password-123"
CALENDAR_URL = "https://cal.example.com/dav/calendars/robin/personal/"


class FakeCalDAV:
    def __init__(self):
        self.objects: dict[str, str] = {}  # path -> iCalendar text
        self.requests: list[httpx.Request] = []
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            return httpx.Response(503)
        auth = request.headers.get("authorization", "")
        if auth != "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode():
            return httpx.Response(401)
        path = request.url.path
        if request.method == "PROPFIND":
            return httpx.Response(207, content=b"<d:multistatus xmlns:d='DAV:'/>")
        if request.method == "REPORT":
            parts = []
            for href, data in self.objects.items():
                body = data.replace("&", "&amp;").replace("<", "&lt;")
                parts.append(
                    f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
                    f'<d:getetag>"1"</d:getetag><c:calendar-data>{body}</c:calendar-data>'
                    "</d:prop></d:propstat></d:response>"
                )
            xml = (
                '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                + "".join(parts)
                + "</d:multistatus>"
            )
            return httpx.Response(207, content=xml.encode())
        if request.method == "PUT":
            if request.headers.get("if-none-match") == "*" and path in self.objects:
                return httpx.Response(412)
            self.objects[path] = request.content.decode()
            return httpx.Response(201)
        if request.method == "GET":
            if path not in self.objects:
                return httpx.Response(404)
            return httpx.Response(200, text=self.objects[path], headers={"etag": '"1"'})
        if request.method == "DELETE":
            self.objects.pop(path, None)
            return httpx.Response(204)
        return httpx.Response(405)

    def titles(self) -> list[str]:
        return sorted(e.title for text in self.objects.values() for e in parse_events(text))


def message(uid_subject: str, sender: str, body: str, to: str = "robin@example.com") -> bytes:
    msg = EmailMessage(policy=email.policy.SMTP)
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = uid_subject
    msg["Date"] = "Thu, 05 Nov 2026 09:00:00 +0000"
    msg["Message-ID"] = f"<{abs(hash(uid_subject))}@example.com>"
    msg.set_content(body)
    return msg.as_bytes()


class FakeMailServer:
    def __init__(self):
        self.inbox: dict[int, tuple[bytes, bool]] = {}  # uid -> (raw, seen)
        self.folders: dict[str, list[tuple[str, bytes]]] = {"Drafts": []}
        self.sent: list[EmailMessage] = []
        self.logins: list[tuple[str, str]] = []

    def deliver(self, raw: bytes, seen: bool = False) -> int:
        uid = max(self.inbox, default=0) + 1
        self.inbox[uid] = (raw, seen)
        return uid

    def imap(self, account):
        return FakeIMAP(self)

    def smtp(self, account):
        return FakeSMTP(self)


class FakeIMAP:
    def __init__(self, server: FakeMailServer):
        self.server = server

    def login(self, user, password):
        import imaplib

        self.server.logins.append((user, password))
        if (user, password) != (USER, PASSWORD):
            raise imaplib.IMAP4.error("authentication failed")
        return "OK", [b"logged in"]

    def select(self, folder, readonly=False):
        return ("OK", [b"1"]) if folder.strip('"') in ("INBOX", "Drafts") else ("NO", [b""])

    def uid(self, command, *args):
        if command == "SEARCH":
            wanted = list(args)
            uids = []
            for uid, (raw, seen) in sorted(self.server.inbox.items()):
                parsed = email.message_from_bytes(raw, policy=email.policy.default)
                text = raw.decode(errors="replace").lower()
                ok = True
                i = 0
                while i < len(wanted):
                    key = wanted[i]
                    if key == "UNSEEN":
                        ok &= not seen
                        i += 1
                    elif key == "ALL":
                        i += 1
                    elif key == "SINCE":
                        i += 2
                    else:
                        value = wanted[i + 1].strip('"').lower()
                        if key == "FROM":
                            ok &= value in str(parsed["From"]).lower()
                        elif key == "SUBJECT":
                            ok &= value in str(parsed["Subject"]).lower()
                        else:
                            ok &= value in text
                        i += 2
                if ok:
                    uids.append(str(uid).encode())
            return "OK", [b" ".join(uids)]
        if command == "FETCH":
            uid, spec = int(args[0]), args[1]
            if uid not in self.server.inbox:
                return "OK", [None]
            raw, seen = self.server.inbox[uid]
            flags = b"\\Seen" if seen else b""
            if "HEADER.FIELDS" in spec:
                head, _, body = raw.partition(b"\r\n\r\n")
                if not body:
                    head, _, body = raw.partition(b"\n\n")
                return "OK", [
                    (
                        f"{uid} (FLAGS ({flags.decode()}) BODY[HEADER.FIELDS]".encode(),
                        head + b"\r\n\r\n",
                    ),
                    (b"BODY[TEXT]<0>", body[:400]),
                    b")",
                ]
            return "OK", [(f"{uid} (FLAGS ({flags.decode()}) BODY[]".encode(), raw), b")"]
        return "NO", [b""]

    def append(self, folder, flags, date, raw):
        self.server.folders.setdefault(folder.strip('"'), []).append((flags, raw))
        return "OK", [b"appended"]

    def logout(self):
        return "BYE", [b""]


class FakeSMTP:
    def __init__(self, server: FakeMailServer):
        self.server = server

    def login(self, user, password):
        import smtplib

        if (user, password) != (USER, PASSWORD):
            raise smtplib.SMTPAuthenticationError(535, b"no")

    def send_message(self, msg):
        self.server.sent.append(msg)

    def quit(self):
        pass


EMAIL_CONFIG = {
    "imap_host": "imap.example.com",
    "smtp_host": "smtp.example.com",
    "username": USER,
    "from_address": "robin@example.com",
}


def tidy(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
