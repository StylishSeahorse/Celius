"""Email over IMAP and SMTP (ADR C-11). Gmail, Outlook/Microsoft 365, Fastmail and most
providers work with an app password; Gmail can also sign in with Google (XOAUTH2,
services/integrations/google.py).

- Reading never marks messages as read (BODY.PEEK).
- Drafts are saved to the account's Drafts folder: the user reviews and sends them in
  their mail app, or approves sending from Celius.
- Sending is a separate tool that *always* asks the user, whatever the tool profile or
  chat grants (ToolSpec.always_ask, enforced in tools/policy.py).
- Connections are pinned to an address checked by netguard (public only, unless the
  account allows the local network), so a mail host name can't be used to reach the
  server's own network; TLS still verifies the real host name.
- Message content is untrusted: tools hand it to the model inside <email> tags as data.
"""

import email
import email.policy
import imaplib
import re
import smtplib
import socket
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import getaddresses, make_msgid, parseaddr, parsedate_to_datetime

import anyio

from app.config import Settings
from app.models import IntegrationAccount
from app.services.integrations import accounts, google
from app.services.tools.builtin import html_to_text
from app.services.tools.netguard import BlockedDestination, resolve_checked

TIMEOUT = 20
MAX_RESULTS = 25
BODY_CHARS = 20_000
MAX_RECIPIENTS = 20

# Tests replace these with fakes (no real mail server in CI).
IMAP_FACTORY = None
SMTP_FACTORY = None


class EmailError(Exception):
    """A mail server problem, told to the user (and the model) as is."""


@dataclass
class Summary:
    uid: str
    sender: str
    to: str
    subject: str
    date: datetime | None
    snippet: str = ""
    unread: bool = False


@dataclass
class Message(Summary):
    cc: str = ""
    message_id: str = ""
    references: str = ""
    body: str = ""
    attachments: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------- connections


class _PinnedIMAP(imaplib.IMAP4_SSL):
    def __init__(self, host: str, address: str, port: int, context: ssl.SSLContext):
        self._address = address
        super().__init__(host, port, ssl_context=context, timeout=TIMEOUT)

    def _create_socket(self, timeout):
        sock = socket.create_connection((self._address, self.port), timeout)
        return self.ssl_context.wrap_socket(sock, server_hostname=self.host)


class _PinnedSMTP(smtplib.SMTP_SSL):
    def __init__(self, host: str, address: str, port: int, context: ssl.SSLContext):
        self._address = address
        super().__init__(host, port, context=context, timeout=TIMEOUT)

    def _get_socket(self, host, port, timeout):
        sock = socket.create_connection((self._address, port), timeout)
        return self.context.wrap_socket(sock, server_hostname=self._host)


class _PinnedSMTPStartTLS(smtplib.SMTP):
    def __init__(self, host: str, address: str, port: int, context: ssl.SSLContext):
        self._address = address
        self._context = context
        super().__init__(host, port, timeout=TIMEOUT)

    def _get_socket(self, host, port, timeout):
        return socket.create_connection((self._address, port), timeout)


async def _address(host: str, port: int, allow_private: bool) -> str:
    try:
        return (await resolve_checked(host, port, allow_private=allow_private))[0]
    except BlockedDestination as exc:
        raise EmailError(str(exc)) from exc


class Mailbox:
    """One account's IMAP (and SMTP for sending). Blocking libraries run in a thread."""

    def __init__(self, account: IntegrationAccount, settings: Settings):
        self.account = account
        self.config = account.config
        self.settings = settings
        self.stored = accounts.credentials(settings, account)
        self.password = self.stored.get("password", "")
        self.oauth = account.provider == "gmail"

    async def _token(self) -> str | None:
        """An OAuth access token (Gmail), or None for password accounts."""
        if not self.oauth:
            return None
        try:
            return await google.access_token(self.settings, self.account.id, self.stored)
        except google.GoogleError as exc:
            raise EmailError(f"{self.account.name}: {exc}") from exc

    async def _imap(self):
        host, port = self.config["imap_host"], int(self.config["imap_port"])
        if IMAP_FACTORY is not None:
            return IMAP_FACTORY(self.account)
        address = await _address(host, port, self.account.allow_private_network)
        context = ssl.create_default_context()
        return await anyio.to_thread.run_sync(lambda: _PinnedIMAP(host, address, port, context))

    async def _run(self, work):
        """Connect, log in, run `work(imap)` in a thread, log out."""
        token = await self._token()
        try:
            imap = await self._imap()
        except (OSError, imaplib.IMAP4.error) as exc:
            raise EmailError(f"{self.account.name}: can't connect ({exc})") from exc

        def session():
            try:
                if token is not None:
                    auth = google.xoauth2(self.config["username"], token).encode()
                    # Gmail's first challenge is empty; a later one is an error to answer empty.
                    imap.authenticate("XOAUTH2", lambda challenge: b"" if challenge else auth)
                else:
                    imap.login(self.config["username"], self.password)
            except imaplib.IMAP4.error as exc:
                raise EmailError(f"{self.account.name}: the server refused the login") from exc
            try:
                return work(imap)
            finally:
                try:
                    imap.logout()
                except (OSError, imaplib.IMAP4.error):
                    pass

        try:
            return await anyio.to_thread.run_sync(session)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise EmailError(f"{self.account.name}: {exc}") from exc

    # ------------------------------------------------------------------ reading

    async def check(self) -> None:
        await self._run(lambda imap: _select(imap, self.config["inbox_folder"]))

    async def search(
        self,
        *,
        text: str | None = None,
        sender: str | None = None,
        subject: str | None = None,
        since_days: int | None = None,
        unread_only: bool = False,
        limit: int = MAX_RESULTS,
    ) -> list[Summary]:
        criteria = search_criteria(
            text=text,
            sender=sender,
            subject=subject,
            since_days=since_days,
            unread_only=unread_only,
        )

        def work(imap):
            _select(imap, self.config["inbox_folder"])
            status, data = imap.uid("SEARCH", *criteria)
            if status != "OK":
                raise EmailError("The search failed")
            uids = (data[0] or b"").split()[-limit:][::-1]  # newest first
            return [_summary(imap, uid.decode()) for uid in uids]

        return await self._run(work)

    async def read(self, uid: str) -> Message:
        if not re.fullmatch(r"\d+", uid or ""):
            raise EmailError("Unknown message id")

        def work(imap):
            _select(imap, self.config["inbox_folder"])
            status, data = imap.uid("FETCH", uid, "(FLAGS BODY.PEEK[])")
            raw = _raw(data)
            if status != "OK" or raw is None:
                raise EmailError("Message not found")
            return parse_message(uid, raw, unread=b"\\Seen" not in _flags(data))

        return await self._run(work)

    async def new_since(self, last_uid: int | None) -> tuple[list[Message], int]:
        """Messages that arrived after `last_uid` (for automations), and the newest UID.
        The first call only records where the inbox is: old mail never fires."""

        def work(imap):
            _select(imap, self.config["inbox_folder"])
            status, data = imap.uid("SEARCH", "ALL")
            uids = [int(u) for u in (data[0] or b"").split()] if status == "OK" else []
            newest = max(uids, default=last_uid or 0)
            if last_uid is None:
                return [], newest
            fresh = [u for u in uids if u > last_uid][-MAX_RESULTS:]
            found = []
            for uid in fresh:
                s, d = imap.uid("FETCH", str(uid), "(FLAGS BODY.PEEK[])")
                raw = _raw(d)
                if s == "OK" and raw is not None:
                    found.append(parse_message(str(uid), raw, unread=b"\\Seen" not in _flags(d)))
            return found, newest

        return await self._run(work)

    # ------------------------------------------------------------------ writing

    def compose(
        self,
        to: list[str],
        subject: str,
        body: str,
        *,
        cc: list[str] | None = None,
        reply_to: Message | None = None,
    ) -> EmailMessage:
        recipients = check_recipients(to) + check_recipients(cc or [])
        if not recipients:
            raise EmailError("Add at least one recipient")
        message = EmailMessage(policy=email.policy.SMTP)
        message["From"] = self.config["from_address"]
        message["To"] = ", ".join(check_recipients(to))
        if cc:
            message["Cc"] = ", ".join(check_recipients(cc))
        subject = " ".join((subject or "").split())[:300]
        if reply_to is not None:
            if not subject:
                subject = reply_to.subject
            if not subject.lower().startswith("re:"):
                subject = f"Re: {subject}"
            if reply_to.message_id:
                message["In-Reply-To"] = reply_to.message_id
                message["References"] = f"{reply_to.references} {reply_to.message_id}".strip()
        message["Subject"] = subject
        message["Message-ID"] = make_msgid(domain=self.config["from_address"].split("@")[-1])
        message["Date"] = email.utils.formatdate(localtime=False)
        message.set_content(body or "")
        return message

    async def save_draft(self, message: EmailMessage) -> None:
        folder = self.config["drafts_folder"]

        def work(imap):
            status, _ = imap.append(
                _quote(folder),
                "(\\Draft)",
                imaplib.Time2Internaldate(datetime.now(UTC)),
                message.as_bytes(),
            )
            if status != "OK":
                raise EmailError(f"Couldn't save to {folder}")

        await self._run(work)

    async def send(self, message: EmailMessage) -> None:
        host, port = self.config["smtp_host"], int(self.config["smtp_port"])
        token = await self._token()

        def work(smtp):
            try:
                if token is not None:
                    auth = google.xoauth2(self.config["username"], token)
                    # After a failed XOAUTH2 the server sends a challenge; answer empty.
                    smtp.auth("XOAUTH2", lambda challenge=None: auth if challenge is None else "")
                else:
                    smtp.login(self.config["username"], self.password)
                smtp.send_message(message)
            finally:
                try:
                    smtp.quit()
                except (OSError, smtplib.SMTPException):
                    pass

        try:
            if SMTP_FACTORY is not None:
                smtp = SMTP_FACTORY(self.account)
            else:
                address = await _address(host, port, self.account.allow_private_network)
                context = ssl.create_default_context()
                if self.config.get("smtp_starttls"):

                    def connect():
                        client = _PinnedSMTPStartTLS(host, address, port, context)
                        client.starttls(context=context)
                        return client

                    smtp = await anyio.to_thread.run_sync(connect)
                else:
                    smtp = await anyio.to_thread.run_sync(
                        lambda: _PinnedSMTP(host, address, port, context)
                    )
            await anyio.to_thread.run_sync(lambda: work(smtp))
        except smtplib.SMTPAuthenticationError as exc:
            raise EmailError(f"{self.account.name}: the mail server refused the login") from exc
        except (OSError, smtplib.SMTPException) as exc:
            raise EmailError(f"{self.account.name}: sending failed ({exc})") from exc


# ---------------------------------------------------------------------- helpers


def _quote(value: str) -> str:
    if re.search(r"[\r\n]", value):
        raise EmailError("Invalid text")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def search_criteria(
    *, text=None, sender=None, subject=None, since_days=None, unread_only=False
) -> list[str]:
    criteria: list[str] = []
    if unread_only:
        criteria.append("UNSEEN")
    if since_days:
        day = (datetime.now(UTC) - timedelta(days=int(since_days))).strftime("%d-%b-%Y")
        criteria += ["SINCE", day]
    for key, value in (("FROM", sender), ("SUBJECT", subject), ("TEXT", text)):
        if value:
            criteria += [key, _quote(value[:200])]
    return criteria or ["ALL"]


def _select(imap, folder: str) -> None:
    status, _ = imap.select(_quote(folder), readonly=True)
    if status != "OK":
        raise EmailError(f"Folder {folder!r} not found")


def _raw(data) -> bytes | None:
    for item in data or []:
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bytes):
            return item[1]
    return None


def _flags(data) -> bytes:
    for item in data or []:
        if isinstance(item, tuple):
            return item[0]
    return b""


def _summary(imap, uid: str) -> Summary:
    status, data = imap.uid(
        "FETCH",
        uid,
        "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)] BODY.PEEK[TEXT]<0.400>)",
    )
    headers = b""
    text = b""
    for item in data or []:
        if isinstance(item, tuple):
            label = item[0].upper()
            if b"HEADER" in label:
                headers = item[1]
            elif b"TEXT" in label:
                text = item[1]
    parsed = email.message_from_bytes(headers, policy=email.policy.default)
    return Summary(
        uid=uid,
        sender=str(parsed.get("From", "")),
        to=str(parsed.get("To", "")),
        subject=str(parsed.get("Subject", "")),
        date=_date(parsed.get("Date")),
        snippet=" ".join(text.decode(errors="replace").split())[:200],
        unread=b"\\Seen" not in _flags(data),
    )


def _date(value) -> datetime | None:
    try:
        return parsedate_to_datetime(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def parse_message(uid: str, raw: bytes, *, unread: bool = False) -> Message:
    parsed = email.message_from_bytes(raw, policy=email.policy.default)
    body = ""
    plain = parsed.get_body(preferencelist=("plain",))
    if plain is not None:
        body = plain.get_content()
    else:
        html = parsed.get_body(preferencelist=("html",))
        if html is not None:
            body = html_to_text(html.get_content())[1]
    attachments = [part.get_filename() or "attachment" for part in parsed.iter_attachments()]
    return Message(
        uid=uid,
        sender=str(parsed.get("From", "")),
        to=str(parsed.get("To", "")),
        cc=str(parsed.get("Cc", "") or ""),
        subject=str(parsed.get("Subject", "")),
        date=_date(parsed.get("Date")),
        message_id=str(parsed.get("Message-ID", "") or ""),
        references=str(parsed.get("References", "") or ""),
        body=body[:BODY_CHARS],
        attachments=attachments,
        unread=unread,
        snippet=" ".join(body.split())[:200],
    )


def check_recipients(values: list[str]) -> list[str]:
    found = []
    for name, address in getaddresses([v for v in values if v]):
        if not re.fullmatch(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+", address or ""):
            raise EmailError(f"Not an email address: {address or name!r}")
        found.append(address)
    if len(found) > MAX_RECIPIENTS:
        raise EmailError(f"At most {MAX_RECIPIENTS} recipients")
    return found


def describe(summary: Summary) -> str:
    when = summary.date.strftime("%Y-%m-%d %H:%M") if summary.date else "?"
    name, address = parseaddr(summary.sender)
    who = name or address
    flag = "unread, " if summary.unread else ""
    return f"[{summary.uid}] {when} {flag}from {who}: {summary.subject}"
