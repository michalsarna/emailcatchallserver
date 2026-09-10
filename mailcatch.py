#!/usr/bin/env python3
"""Catch-all SMTP server with a local web inbox."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import threading
import uuid
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
MAX_MESSAGE_BYTES = 25 * 1024 * 1024
ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}_[a-f0-9]{8}$")
MAIL_FROM_RE = re.compile(r"(?i)^MAIL FROM:\s*<?([^>\s]*)>?")
RCPT_TO_RE = re.compile(r"(?i)^RCPT TO:\s*<?([^>\s]*)>?")
CID_SRC_RE = re.compile(r"""(?i)(\b(?:src|href)\s*=\s*)(['"])cid:([^'"]+)\2""")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_message_id() -> str:
    return f"{utcnow().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"


def decode_header_value(value: str | None) -> str:
    return (value or "").replace("\r", " ").replace("\n", " ").strip()


def part_bytes(part) -> bytes:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        if isinstance(raw, str):
            return raw.encode("utf-8", errors="replace")
        return b""
    return payload


def part_text(part) -> str:
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content
        if isinstance(content, bytes):
            charset = part.get_content_charset() or "utf-8"
            return content.decode(charset, errors="replace")
    except Exception:
        pass
    data = part_bytes(part)
    charset = part.get_content_charset() or "utf-8"
    try:
        return data.decode(charset, errors="replace")
    except Exception:
        return data.decode("utf-8", errors="replace")


def preview_text(text: str, limit: int = 160) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


def strip_tags(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return text


def parse_message(raw: bytes):
    try:
        return BytesParser(policy=policy.default).parsebytes(raw)
    except Exception:
        return BytesParser(policy=policy.compat32).parsebytes(raw)


def addresses(msg, header: str) -> list[str]:
    values = msg.get_all(header, [])
    parsed = getaddresses(values)
    result = []
    for name, addr in parsed:
        if name and addr:
            result.append(f"{name} <{addr}>")
        elif addr:
            result.append(addr)
        elif name:
            result.append(name)
    return result


def message_date(msg, fallback: str) -> str:
    raw = msg.get("Date")
    if raw:
        try:
            return parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat()
        except Exception:
            pass
    return fallback


def walk_leaf_parts(msg):
    if msg.is_multipart():
        for part in msg.iter_parts():
            yield from walk_leaf_parts(part)
    else:
        yield msg


def classify_parts(msg) -> dict:
    text = None
    html = None
    file_parts = []
    for part in walk_leaf_parts(msg):
        ctype = part.get_content_type()
        disp = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        cid = (part.get("Content-ID") or "").strip()
        treat_as_file = disp == "attachment" or disp == "inline" or bool(filename) or bool(cid)
        if treat_as_file and not (ctype in ("text/plain", "text/html") and disp in ("", "inline") and not filename):
            file_parts.append(part)
            continue
        if ctype == "text/plain" and text is None:
            text = part_text(part)
        elif ctype == "text/html" and html is None:
            html = part_text(part)
        else:
            file_parts.append(part)

    attachments = []
    cids: dict[str, int] = {}
    for index, part in enumerate(file_parts):
        cid = (part.get("Content-ID") or "").strip().strip("<>")
        filename = part.get_filename() or (f"inline-{cid}" if cid else f"part-{index + 1}")
        attachments.append(
            {
                "index": index,
                "filename": filename,
                "content_type": part.get_content_type(),
                "size": len(part_bytes(part)),
                "cid": cid,
                "part": part,
            }
        )
        if cid:
            cids[cid] = index
    return {"text": text, "html": html, "attachments": attachments, "cids": cids}


def content_disposition_header(filename: str) -> str:
    clean = re.sub(r"[\r\n]", "", filename).strip() or "download"
    ascii_fallback = re.sub(r'[^\x20-\x7e]', "_", clean).replace('"', "'") or "download"
    encoded = quote(clean, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


def rewrite_cids(html: str, msg_id: str, cids: dict[str, int]) -> str:
    def repl(match: re.Match) -> str:
        attr, quote, cid = match.group(1), match.group(2), match.group(3).strip().strip("<>")
        if cid not in cids:
            return match.group(0)
        return f"{attr}{quote}/api/messages/{msg_id}/cid/{cid}{quote}"

    return CID_SRC_RE.sub(repl, html)


class MailStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def path_for(self, msg_id: str) -> Path:
        if not ID_RE.match(msg_id):
            raise ValueError("invalid id")
        return self.data_dir / f"{msg_id}.eml"

    def save(self, raw: bytes, mail_from: str, rcpt_to: list[str]) -> str:
        if len(raw) > MAX_MESSAGE_BYTES:
            raise ValueError("message too large")
        msg_id = new_message_id()
        received = utcnow().isoformat()
        envelope = (
            f"X-Mailcatch-Id: {msg_id}\r\n"
            f"X-Mailcatch-Received: {received}\r\n"
            f"X-Envelope-From: {mail_from}\r\n"
            f"X-Envelope-To: {', '.join(rcpt_to)}\r\n"
        ).encode("utf-8")
        payload = envelope + raw
        dest = self.path_for(msg_id)
        tmp = dest.with_suffix(".tmp")
        with self._lock:
            tmp.write_bytes(payload)
            tmp.replace(dest)
        return msg_id

    def list_ids(self) -> list[str]:
        files = sorted(self.data_dir.glob("*.eml"), key=lambda p: p.name, reverse=True)
        return [p.stem for p in files if ID_RE.match(p.stem)]

    def raw(self, msg_id: str) -> bytes:
        path = self.path_for(msg_id)
        if not path.is_file():
            raise FileNotFoundError(msg_id)
        return path.read_bytes()

    def delete(self, msg_id: str) -> None:
        path = self.path_for(msg_id)
        with self._lock:
            if not path.is_file():
                raise FileNotFoundError(msg_id)
            path.unlink()

    def delete_all(self) -> int:
        count = 0
        with self._lock:
            for path in self.data_dir.glob("*.eml"):
                if ID_RE.match(path.stem):
                    path.unlink()
                    count += 1
        return count

    def _load(self, msg_id: str):
        raw = self.raw(msg_id)
        msg = parse_message(raw)
        parts = classify_parts(msg)
        return raw, msg, parts

    def _summarize(self, msg_id: str, raw: bytes, msg, parts: dict) -> dict:
        envelope_from = decode_header_value(msg.get("X-Envelope-From"))
        envelope_to = [item.strip() for item in decode_header_value(msg.get("X-Envelope-To")).split(",") if item.strip()]
        header_from = addresses(msg, "From")
        header_to = addresses(msg, "To")
        text = parts["text"] or (strip_tags(parts["html"]) if parts["html"] else "")
        received = decode_header_value(msg.get("X-Mailcatch-Received")) or utcnow().isoformat()
        name, addr = parseaddr(header_from[0] if header_from else envelope_from)
        return {
            "id": msg_id,
            "from": header_from[0] if header_from else envelope_from,
            "from_name": name or addr or envelope_from,
            "to": header_to or envelope_to,
            "cc": addresses(msg, "Cc"),
            "subject": decode_header_value(msg.get("Subject")) or "(no subject)",
            "date": message_date(msg, received),
            "received": received,
            "size": len(raw),
            "preview": preview_text(text),
            "has_html": bool(parts["html"]),
            "has_text": bool(parts["text"]),
            "attachments": len(parts["attachments"]),
        }

    def summarize(self, msg_id: str) -> dict:
        raw, msg, parts = self._load(msg_id)
        return self._summarize(msg_id, raw, msg, parts)

    def detail(self, msg_id: str) -> dict:
        raw, msg, parts = self._load(msg_id)
        summary = self._summarize(msg_id, raw, msg, parts)
        headers = [(str(name), str(value)) for name, value in msg.items()]
        return {
            **summary,
            "text": parts["text"],
            "html": bool(parts["html"]),
            "headers": headers,
            "files": [
                {
                    "index": item["index"],
                    "filename": item["filename"],
                    "content_type": item["content_type"],
                    "size": item["size"],
                }
                for item in parts["attachments"]
            ],
        }

    def html_body(self, msg_id: str) -> str:
        raw = self.raw(msg_id)
        msg = parse_message(raw)
        parts = classify_parts(msg)
        if not parts["html"]:
            raise FileNotFoundError("no html body")
        return rewrite_cids(parts["html"], msg_id, parts["cids"])

    def attachment(self, msg_id: str, index: int) -> tuple[str, str, bytes]:
        raw = self.raw(msg_id)
        msg = parse_message(raw)
        parts = classify_parts(msg)
        if index < 0 or index >= len(parts["attachments"]):
            raise FileNotFoundError("attachment")
        meta = parts["attachments"][index]
        return meta["filename"], meta["content_type"], part_bytes(meta["part"])

    def cid_part(self, msg_id: str, cid: str) -> tuple[str, bytes]:
        raw = self.raw(msg_id)
        msg = parse_message(raw)
        parts = classify_parts(msg)
        if cid not in parts["cids"]:
            raise FileNotFoundError("cid")
        filename, content_type, data = self.attachment(msg_id, parts["cids"][cid])
        return content_type, data


class SMTPSession:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, store: MailStore, hostname: str):
        self.reader = reader
        self.writer = writer
        self.store = store
        self.hostname = hostname
        self.reset_envelope()

    def reset_envelope(self) -> None:
        self.mail_from = ""
        self.rcpt_to: list[str] = []
        self.seen_mail = False

    async def send(self, line: str) -> None:
        self.writer.write((line + "\r\n").encode("ascii", errors="replace"))
        await self.writer.drain()

    async def readline(self) -> bytes:
        line = await asyncio.wait_for(self.reader.readline(), timeout=120)
        if len(line) > 10000:
            raise ValueError("line too long")
        return line

    async def run(self) -> None:
        await self.send(f"220 {self.hostname} ESMTP email catch-all")
        try:
            while True:
                raw = await self.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    await self.send("500 empty command")
                    continue
                verb = line.split(None, 1)[0].upper()
                if verb == "HELO":
                    self.reset_envelope()
                    await self.send(f"250 {self.hostname}")
                elif verb == "EHLO":
                    self.reset_envelope()
                    await self.send(f"250-{self.hostname}")
                    await self.send("250-PIPELINING")
                    await self.send("250-8BITMIME")
                    await self.send(f"250 SIZE {MAX_MESSAGE_BYTES}")
                elif verb == "MAIL":
                    match = MAIL_FROM_RE.match(line)
                    if not match:
                        await self.send("501 malformed MAIL FROM")
                        continue
                    self.mail_from = match.group(1)
                    self.rcpt_to = []
                    self.seen_mail = True
                    await self.send("250 2.1.0 OK")
                elif verb == "RCPT":
                    if not self.seen_mail:
                        await self.send("503 MAIL first")
                        continue
                    match = RCPT_TO_RE.match(line)
                    if not match:
                        await self.send("501 malformed RCPT TO")
                        continue
                    self.rcpt_to.append(match.group(1) or "undisclosed")
                    await self.send("250 2.1.5 OK")
                elif verb == "DATA":
                    if not self.seen_mail or not self.rcpt_to:
                        await self.send("503 need MAIL and RCPT")
                        continue
                    await self.send("354 End data with <CR><LF>.<CR><LF>")
                    await self.read_data()
                elif verb == "RSET":
                    self.reset_envelope()
                    await self.send("250 2.0.0 OK")
                elif verb == "NOOP":
                    await self.send("250 2.0.0 OK")
                elif verb == "VRFY":
                    await self.send("252 2.0.0 will catch anything")
                elif verb == "HELP":
                    await self.send("214-Commands: HELO EHLO MAIL RCPT DATA RSET NOOP QUIT VRFY")
                    await self.send("214 End")
                elif verb == "QUIT":
                    await self.send("221 2.0.0 bye")
                    break
                elif verb == "STARTTLS":
                    await self.send("502 5.5.1 STARTTLS not supported")
                else:
                    await self.send("502 5.5.1 command not recognized")
        except asyncio.TimeoutError:
            try:
                await self.send("421 4.4.2 timeout")
            except Exception:
                pass
        except Exception:
            try:
                await self.send("451 4.3.0 internal error")
            except Exception:
                pass
        finally:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass

    async def read_data(self) -> None:
        chunks: list[bytes] = []
        total = 0
        while True:
            line = await self.readline()
            if not line:
                await self.send("451 connection lost during DATA")
                return
            if line == b".\r\n" or line == b".\n":
                break
            if line.startswith(b"."):
                line = line[1:]
            total += len(line)
            if total > MAX_MESSAGE_BYTES:
                await self.send("552 5.3.4 message too large")
                # drain until dot
                while True:
                    extra = await self.readline()
                    if not extra or extra in (b".\r\n", b".\n"):
                        break
                self.reset_envelope()
                return
            chunks.append(line)
        raw = b"".join(chunks)
        try:
            msg_id = self.store.save(raw, self.mail_from, self.rcpt_to)
        except Exception:
            await self.send("451 4.3.0 failed to store message")
            self.reset_envelope()
            return
        await self.send(f"250 2.0.0 queued as {msg_id}")
        self.reset_envelope()


async def smtp_main(host: str, port: int, store: MailStore) -> None:
    hostname = "mailcatch.local"

    async def on_connect(reader, writer):
        session = SMTPSession(reader, writer, store, hostname)
        await session.run()

    server = await asyncio.start_server(on_connect, host, port)
    async with server:
        await server.serve_forever()


def run_smtp(host: str, port: int, store: MailStore) -> None:
    asyncio.run(smtp_main(host, port, store))


class MailHandler(BaseHTTPRequestHandler):
    store: MailStore
    http_port: int
    smtp_port: int
    bind_host: str

    def log_message(self, fmt: str, *args) -> None:
        sys_stderr = __import__("sys").stderr
        print(f"[http] {self.address_string()} {fmt % args}", file=sys_stderr)

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, status: int, data: bytes, content_type: str, filename: str | None = None, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if filename:
            self.send_header("Content-Disposition", content_disposition_header(filename))
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _text(self, status: int, text: str, content_type: str, extra: dict | None = None) -> None:
        self._bytes(status, text.encode("utf-8"), content_type, extra=extra)

    def _not_found(self, what: str = "not found") -> None:
        self._json(404, {"error": what})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path in ("/", "/index.html"):
                index = STATIC_DIR / "index.html"
                self._bytes(200, index.read_bytes(), "text/html; charset=utf-8")
                return
            if path == "/favicon.svg":
                self._bytes(200, (STATIC_DIR / "favicon.svg").read_bytes(), "image/svg+xml")
                return
            if path == "/api/status":
                self._json(
                    200,
                    {
                        "smtp_host": self.bind_host,
                        "smtp_port": self.smtp_port,
                        "http_port": self.http_port,
                        "messages": len(self.store.list_ids()),
                    },
                )
                return
            if path == "/api/messages":
                messages = []
                for msg_id in self.store.list_ids():
                    try:
                        messages.append(self.store.summarize(msg_id))
                    except Exception:
                        continue
                self._json(200, {"messages": messages})
                return

            parts = [p for p in path.split("/") if p]
            if len(parts) >= 3 and parts[0] == "api" and parts[1] == "messages":
                msg_id = parts[2]
                if not ID_RE.match(msg_id):
                    self._not_found("invalid id")
                    return
                if len(parts) == 3:
                    self._json(200, self.store.detail(msg_id))
                    return
                if len(parts) == 4 and parts[3] == "raw":
                    raw = self.store.raw(msg_id)
                    self._bytes(200, raw, "message/rfc822", filename=f"{msg_id}.eml")
                    return
                if len(parts) == 4 and parts[3] == "html":
                    html = self.store.html_body(msg_id)
                    extra = {
                        "Content-Security-Policy": "default-src 'none'; img-src data: http: https:; style-src 'unsafe-inline' data:; font-src data:; media-src data: http: https:",
                        "X-Content-Type-Options": "nosniff",
                    }
                    self._text(200, html, "text/html; charset=utf-8", extra=extra)
                    return
                if len(parts) == 5 and parts[3] == "attachments":
                    index = int(parts[4])
                    filename, content_type, data = self.store.attachment(msg_id, index)
                    self._bytes(200, data, content_type or "application/octet-stream", filename=filename)
                    return
                if len(parts) == 5 and parts[3] == "cid":
                    cid = parts[4]
                    content_type, data = self.store.cid_part(msg_id, cid)
                    extra = {"Content-Security-Policy": "default-src 'none'"}
                    self._bytes(200, data, content_type or "application/octet-stream", extra=extra)
                    return
            self._not_found()
        except FileNotFoundError:
            self._not_found()
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Exception:
            self._json(500, {"error": "internal error"})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/api/messages":
                deleted = self.store.delete_all()
                self._json(200, {"deleted": deleted})
                return
            parts = [p for p in path.split("/") if p]
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "messages":
                msg_id = parts[2]
                self.store.delete(msg_id)
                self._json(200, {"deleted": msg_id})
                return
            self._not_found()
        except FileNotFoundError:
            self._not_found()
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Exception:
            self._json(500, {"error": "internal error"})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Catch-all SMTP server with a web inbox")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--http-port", type=int, default=8001, help="web UI port (default 8001)")
    parser.add_argument("--smtp-port", type=int, default=1025, help="SMTP port (default 1025)")
    parser.add_argument("--data-dir", default=str(APP_DIR / "data"), help="directory for .eml files")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    store = MailStore(Path(args.data_dir))
    MailHandler.store = store
    MailHandler.http_port = args.http_port
    MailHandler.smtp_port = args.smtp_port
    MailHandler.bind_host = args.host

    smtp_thread = threading.Thread(
        target=run_smtp,
        args=(args.host, args.smtp_port, store),
        name="smtp",
        daemon=True,
    )
    smtp_thread.start()

    class ReuseHTTPServer(ThreadingHTTPServer):
        allow_reuse_address = True

    httpd = ReuseHTTPServer((args.host, args.http_port), MailHandler)
    httpd.daemon_threads = True
    display = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print(f"SMTP  {args.host}:{args.smtp_port}  (catch-all, no auth)")
    print(f"Web   http://{display}:{args.http_port}")
    print(f"Store {store.data_dir}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
