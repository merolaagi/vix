"""Sign-in for HQ when it is reached from the internet (through the Cloudflare tunnel).

Requests made on the Mac itself (Host 127.0.0.1 or localhost, no Cloudflare headers) are let straight in, as before.
Requests that come through the tunnel need a session cookie, which /login gives out for the HQ password. With no
password set, HQ refuses every internet request, so publishing the hostname never exposes an open admin page.

The password is stored as a salted PBKDF2 hash in data/hq_auth.json (owner-only). Set it with:
    .venv/bin/python -m vix.hqauth
The customer API (/portal/api/v1/...) keeps its own per-customer keys and is not behind the HQ password."""
from __future__ import annotations

import getpass
import hashlib
import hmac
import html
import json
import os
import secrets
import sys
import time
from urllib.parse import quote

from .config import DATA_DIR

AUTH_FILE = DATA_DIR / "hq_auth.json"
COOKIE = "vix_hq"
DAYS = 30
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
OPEN_PATHS = ("/login", "/logout", "/portal/api/v1/", "/favicon.ico")
_fails: dict[str, list[float]] = {}


def _load() -> dict:
    try:
        return json.loads(AUTH_FILE.read_text())
    except (OSError, ValueError):
        return {}


def configured() -> bool:
    return bool(_load().get("hash"))


def set_password(pw: str) -> None:
    if len(pw) < 10:
        raise ValueError("use at least 10 characters")
    salt = secrets.token_hex(16)
    data = {"salt": salt, "hash": hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 300_000).hex(),
            "secret": secrets.token_hex(32), "set": time.strftime("%Y-%m-%d %H:%M")}
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, AUTH_FILE)
    os.chmod(AUTH_FILE, 0o600)


def check_password(pw: str) -> bool:
    a = _load()
    if not a.get("hash"):
        return False
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(a["salt"]), 300_000).hex()
    return hmac.compare_digest(h, a["hash"])


def make_cookie() -> str:
    exp = str(int(time.time()) + DAYS * 86400)
    sig = hmac.new(bytes.fromhex(_load()["secret"]), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def valid_cookie(v: str | None) -> bool:
    a = _load()
    if not v or not a.get("secret") or "." not in v:
        return False
    exp, sig = v.split(".", 1)
    if not exp.isdigit() or int(exp) < time.time():
        return False
    good = hmac.new(bytes.fromhex(a["secret"]), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good)


def is_public(headers: dict, client_host: str | None = None) -> bool:
    """True when the request came through Cloudflare (or any non-local Host)."""
    if headers.get("cf-connecting-ip") or headers.get("cf-ray"):
        return True
    host = (headers.get("host") or "").strip().lower()
    name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    return name not in LOCAL_HOSTS


def too_many(ip: str) -> bool:
    now = time.time()
    _fails[ip] = [t for t in _fails.get(ip, []) if now - t < 900]
    return len(_fails[ip]) >= 8


def failed(ip: str) -> None:
    _fails.setdefault(ip, []).append(time.time())


def safe_next(n: str | None) -> str:
    n = n or "/"
    return n if n.startswith("/") and not n.startswith("//") and "\\" not in n else "/"


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>vix HQ sign in</title><style>
:root{--bg:#0e1116;--panel:#161b22;--line:#2a313c;--ink:#e6edf3;--dim:#8b98a8;--accent:#3fc1b0;--red:#f0686a}
@media (prefers-color-scheme: light){:root{--bg:#f5f7fa;--panel:#fff;--line:#d8dee6;--ink:#18212c;--dim:#5a6878;--accent:#0b8077;--red:#c2393b}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;padding:16px}
form{width:100%;max-width:360px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:24px}
h1{font:600 20px ui-monospace,SFMono-Regular,Menlo,monospace;margin:0 0 4px}h1 b{color:var(--accent)}p{color:var(--dim);margin:0 0 18px;font-size:13px}
label{display:block;font-size:13px;color:var(--dim);margin-bottom:6px}input{width:100%;padding:10px 12px;border-radius:8px;border:1px solid var(--line);
background:var(--bg);color:var(--ink);font-size:15px}button{margin-top:14px;width:100%;padding:10px;border-radius:8px;border:1px solid var(--accent);
background:var(--accent);color:#fff;font-weight:600;font-size:15px;cursor:pointer}.err{color:var(--red);font-size:13px;margin-top:10px}</style></head>
<body><form method="post" action="/login"><h1><b>vix</b> HQ</h1><p>Simulated company · founder sign-in</p>
<label for="pw">Password</label><input id="pw" name="password" type="password" autocomplete="current-password" autofocus required>
<input type="hidden" name="next" value="{next}"><button type="submit">Sign in</button>{err}</form></body></html>"""

CLOSED = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>vix HQ</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0e1116;color:#e6edf3;
font:15px/1.6 -apple-system,BlinkMacSystemFont,sans-serif;padding:16px}div{max-width:460px}code{background:#161b22;padding:2px 6px;border-radius:4px}</style>
</head><body><div><h1>vix HQ is not open to the internet yet</h1><p>No HQ password is set. On the Mac, run
<code>cd ~/Sites/vix &amp;&amp; .venv/bin/python -m vix.hqauth</code> and restart with <code>./serve.sh</code>.</p></div></body></html>"""


def login_page(next_: str = "/", err: str = "") -> str:
    return PAGE.replace("{next}", html.escape(safe_next(next_), quote=True)).replace(
        "{err}", f'<div class="err">{html.escape(err)}</div>' if err else "")


def login_redirect(path: str, query: str) -> str:
    return "/login?next=" + quote(path + (("?" + query) if query else ""), safe="")


def main(argv=None):
    argv = argv or sys.argv[1:]
    if "--if-missing" in argv and configured():
        print("HQ password already set (change it with: .venv/bin/python -m vix.hqauth)")
        return
    if "--status" in argv:
        print("set" if configured() else "not set")
        return
    print("Choose the password for signing in to vix HQ from the internet (10+ characters).")
    for _ in range(3):
        a = getpass.getpass("HQ password: ")
        b = getpass.getpass("Again: ")
        if a != b:
            print("They didn't match; try again.")
            continue
        try:
            set_password(a)
        except ValueError as e:
            print(e)
            continue
        print(f"Saved (hashed) to {AUTH_FILE}. Sign-ins last {DAYS} days.")
        return
    raise SystemExit("HQ password not set.")


if __name__ == "__main__":
    main()
