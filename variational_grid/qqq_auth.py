"""Credentials stay at the fixed Variational read-only transport boundary."""
import hashlib

from .client import Client
from .models import GridError


class SessionUnavailable(GridError):
    pass


class VarSession:
    def __init__(self, session_file):
        self.client = Client(session_file) if session_file is not None else None
        self._identity = None
        self.confirmed = False
        self.rejected = False
        self.error = ""

    def read(self):
        try:
            if self.client is None:
                raise GridError("Missing session file")
            token, agent = self.client.session()
        except GridError:
            self.confirmed = False
            self.error = "Var 会话缺失、过期或不可读；请使用 init-session 更新 vr-token"
            raise SessionUnavailable(self.error) from None
        identity = hashlib.sha256(token.encode()).digest()
        if identity != self._identity:
            self._identity, self.confirmed, self.rejected = identity, False, False
        if self.rejected:
            self.error = "Var 会话被拒绝（HTTP 401/403）；请使用 init-session 更新 vr-token"
            raise SessionUnavailable(self.error)
        self.error = "" if self.confirmed else "等待 Var token 鉴权报价确认"
        return token, agent

    def reject(self):
        self.confirmed, self.rejected = False, True
        self.error = "Var 会话被拒绝（HTTP 401/403）；请使用 init-session 更新 vr-token"
        raise SessionUnavailable(self.error)

    def accept(self):
        self.confirmed, self.error = True, ""

    def cache_allowed(self):
        try:
            self.read()
            return self.confirmed
        except SessionUnavailable:
            return False
