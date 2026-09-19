"""Private session handling and a deliberately narrow, non-executing API client."""
import base64
import json
import os
from pathlib import Path
import re
import shlex
import time
from http.cookies import SimpleCookie
import urllib.error
import urllib.parse
import urllib.request

from .models import D, GridError, HOUR, WINDOW, Quote, utc
from .protection import decode_session, encode_session

ORIGIN = "https://omni.variational.io"
ME = ORIGIN + "/api/me"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36"


def token_expiry(token):
    try:
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
            raise ValueError()
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        exp = claims["exp"]
        if type(exp) not in (int, float) or not 0 < exp < 253402300799:
            raise ValueError()
        return exp
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise GridError("Invalid session token or expiry") from None


def import_curl(text):
    """Parse Chrome Copy as cURL (bash) as data. Never execute any supplied command."""
    try:
        args = shlex.split(text.replace("\\\r\n", "").replace("\\\n", ""))
        if not args or args.pop(0) not in ("curl", "curl.exe"):
            raise ValueError()
        headers, urls = {}, []
        i = 0
        while i < len(args):
            arg = args[i]
            i += 1
            if arg in ("--compressed", "--http1.1", "--http2"):
                continue
            if arg in ("-H", "--header", "-b", "--cookie", "-A", "--user-agent", "-X", "--request", "--url"):
                value = args[i]
                i += 1
                if "\n" in value or "\r" in value:
                    raise ValueError()
                if arg in ("-H", "--header"):
                    key, separator, value = value.partition(":")
                    if not separator:
                        raise ValueError()
                    headers[key.strip().lower()] = value.strip()
                elif arg in ("-b", "--cookie"):
                    headers["cookie"] = value
                elif arg in ("-A", "--user-agent"):
                    headers["user-agent"] = value
                elif arg in ("-X", "--request"):
                    if value.upper() != "GET":
                        raise ValueError()
                else:
                    urls.append(value)
            elif arg == ME:
                urls.append(arg)
            else:
                raise ValueError()
        if urls != [ME]:
            raise ValueError()
        cookies = SimpleCookie()
        cookies.load(headers.get("cookie", ""))
        token = cookies["vr-token"].value
        token_expiry(token)
        return {"token": token, "user_agent": headers.get("user-agent", USER_AGENT)}
    except (ValueError, KeyError, IndexError):
        raise GridError("Import requires Chrome Copy as cURL (bash) for GET /api/me, with vr-token") from None


def save_session(path, data):
    token_expiry(data.get("token"))
    # Encrypt before creating a file: plaintext never touches Windows storage.
    stored = encode_session(data)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".new")
    try:
        # O_EXCL rejects an unexpected existing file or symlink.
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(stored, handle)
        os.replace(temporary, path)
    except OSError:
        raise GridError("Could not securely save session; check destination permissions") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, session_file, opener=None):
        self.session_file = Path(session_file)
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def session(self):
        try:
            if self.session_file.stat().st_size > 32768:
                raise GridError("Session file is too large")
            if os.name == "posix" and self.session_file.stat().st_mode & 0o077:
                raise GridError("Session permissions must be 0600")
            data = decode_session(json.loads(self.session_file.read_text(encoding="utf-8-sig")))
            token = data["token"]
            if token_expiry(token) <= time.time() + 30:
                raise GridError("Session expired or expiring; import a fresh browser session")
            agent = data.get("user_agent", USER_AGENT)
            if not isinstance(agent, str) or len(agent) > 2048 or any(ord(c) < 32 or ord(c) > 126 for c in agent):
                raise GridError("Invalid session user agent")
            return token, agent
        except (OSError, ValueError, KeyError, TypeError):
            raise GridError("Cannot read session; run init-session first") from None

    def request(self, method, path, *, query=None, body=None):
        # This allowlist is enforced at the network sink, not only in CLI mode selection.
        if (method, path) not in {("GET", "/me"), ("GET", "/candles"), ("GET", "/metadata/supported_assets"), ("POST", "/quotes/indicative")}:
            raise GridError("Endpoint is not permitted by this paper-only client")
        token, agent = self.session()
        url = ORIGIN + "/api" + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Cookie": "vr-token=" + token, "User-Agent": agent, "Accept": "application/json"}
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body).encode()
        req = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=20) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise GridError("API response exceeds size limit")
                data = json.loads(raw, parse_float=D)
                if not isinstance(data, (dict, list)):
                    raise GridError("Unexpected API response type")
                return data
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            raise GridError(f"API HTTP {code}; check session, service availability, or rate limit") from None
        except (OSError, ValueError, UnicodeError):
            raise GridError("API transport or JSON error; no simulated fills applied") from None

    def check_session(self):
        data = self.request("GET", "/me")
        token = data.get("token") if isinstance(data, dict) else None
        if not token or token_expiry(token) <= time.time():
            raise GridError("Server did not confirm an authenticated session")
        return {"authenticated": True, "expires_utc": utc(token_expiry(token))}

    def market(self, symbol):
        data = self.request("GET", "/metadata/supported_assets", query={"cex_asset": symbol})
        try:
            matches = [a for a in data[symbol] if a.get("asset") == symbol and a.get("has_perp") is True]
            if len(matches) != 1 or matches[0]["instrument_type"] != "perpetual_rwa_future" or matches[0]["asset_class"] != "commodity":
                raise GridError("Venue instrument definition changed")
            item = matches[0]
            if not isinstance(item["is_close_only_mode"], bool):
                raise GridError("Invalid market close-only status")
            return item["market_status"] == "open", item["is_close_only_mode"]
        except (KeyError, TypeError, AttributeError):
            raise GridError("Market metadata schema changed") from None

    def candles(self, symbol, hour_end):
        return self.request("GET", "/candles", query={"cex_asset": symbol, "period": "1h", "start": utc(hour_end - WINDOW * HOUR), "end": utc(hour_end)})

    def quote(self, symbol, qty):
        data = self.request("POST", "/quotes/indicative", body={"instrument": {"underlying": symbol, "instrument_type": "perpetual_rwa_future", "settlement_asset": "USDC", "kind": "commodity"}, "qty": str(qty)})
        return Quote.parse(symbol, qty, data)
