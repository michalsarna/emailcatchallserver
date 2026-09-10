# Email Catch-All

A tiny catch-all SMTP server that stores every incoming message as an `.eml` file
and lets you read them in a web inbox.

Python 3.10+, **no third-party packages**.

## Requirements

- Python 3.10 or newer
- No `pip install` needed — everything is standard library

## Installation

```bash
git clone https://github.com/michalsarna/emailcatchallserver.git
cd emailcatchallserver
```

That's it — there's nothing to build or install.

## Run

```bash
python3 mailcatch.py
```

| Service | Default |
| --- | --- |
| Web inbox | [http://127.0.0.1:8001](http://127.0.0.1:8001) |
| SMTP | `127.0.0.1:1025` (accepts any recipient, no auth) |
| Storage | `./data/*.eml` |

```bash
python3 mailcatch.py --host 0.0.0.0 --http-port 8001 --smtp-port 1025 --data-dir ./data
```

| Flag | Default | Description |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Bind address for both SMTP and the web inbox |
| `--http-port` | `8001` | Web inbox port |
| `--smtp-port` | `1025` | SMTP port |
| `--data-dir` | `./data` | Directory where `.eml` files are stored |

Stop the server with `Ctrl+C`.

SMTP has no authentication, and the web inbox has no login — anyone who can
reach the bound host/port can send mail through it or read/delete every
stored message. Keep `--host 127.0.0.1` (the default) unless you
intentionally want other machines on the network to reach **both** services;
`--host 0.0.0.0` exposes the SMTP relay *and* the web inbox (including
"Empty tray") to the whole network, not just SMTP.

## Point an app at it

SMTP host `127.0.0.1`, port `1025`, encryption off, auth off.

```python
import smtplib
from email.message import EmailMessage

msg = EmailMessage()
msg["From"] = "dev@localhost"
msg["To"] = "anyone@example.com"
msg["Subject"] = "Hello"
msg.set_content("Caught you.")

with smtplib.SMTP("127.0.0.1", 1025) as smtp:
    smtp.send_message(msg)
```

## License

MIT — see [LICENSE](LICENSE).
