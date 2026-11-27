"""
Modo API do Sankhya Schema MCP: o mesmo DB Explorer do modo DBE, pelo gateway
oficial `api.sankhya.com.br`.

Existe porque alguns clientes bloquearam o `MobileLoginSP.login` no Sankhya deles.
O gateway autentica de outra forma e roteia para o Sankhya do cliente pelo token
dele:

1. `POST /login` com os headers `AppKey`, `Token`, `Username` e `Password` devolve
   `{"bearerToken": ...}`. `Token` é o `api_token` do cliente; os outros três são
   a credencial da Vidya no gateway, a mesma para todos, e vêm do ambiente;
2. cada consulta vai em `gateway/v1/mge/service.sbr` com `Authorization: Bearer`.

Daí em diante é igual ao DBE (só SELECT/WITH, 5000 linhas, sem bind): `ApiSession`
herda `execute` de `DbeSession`, e `dbe.run` converte o resultado. Ao encerrar o
processo, `atexit` faz logout de cada sessão aberta.
"""

import atexit
import json
import os
import threading
import urllib.error
import urllib.request
from decimal import Decimal

from dbe import DbeError, DbeSession, TIMEOUT_SEGUNDOS

GATEWAY = "https://api.sankhya.com.br"
GATEWAY_SANDBOX = "https://api.sandbox.sankhya.com.br"

# Variáveis do ambiente com a credencial da Vidya no gateway, na ordem de AppKey,
# Username e Password.
_ENV = ("SANKHYA_API_APPKEY", "SANKHYA_API_USER", "SANKHYA_API_PASSWORD")


class ApiSession(DbeSession):
    """
    Uma sessão no gateway, reaproveitada em todas as consultas do cliente.

    O bearer fica em `_jsessionid` (o campo que o `execute` herdado já confere e
    refaz) e nunca vai a log nem a mensagem de erro. HTTP 401 vira `status 3`, o
    "sessão expirada" do DBE, e a consulta refaz o login uma vez.
    """

    def __init__(self, token: str, appkey: str, usuario: str, senha: str, sandbox: bool = False):
        self._gateway = GATEWAY_SANDBOX if sandbox else GATEWAY
        super().__init__(self._gateway, usuario, senha)
        self._token = token
        self._appkey = appkey

    def _abre(self, requisicao: urllib.request.Request, servico: str) -> dict:
        try:
            with urllib.request.urlopen(requisicao, timeout=TIMEOUT_SEGUNDOS) as resposta:
                bruto = resposta.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and servico != "login":
                return {"status": "3"}
            raise DbeError(f"O gateway do Sankhya respondeu HTTP {exc.code} em {servico}.") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            motivo = getattr(exc, "reason", exc)
            raise DbeError(f"Não foi possível falar com o gateway do Sankhya: {motivo}") from None
        try:
            # parse_float=Decimal pelo mesmo motivo do DBE: NUMBER não pode virar float.
            return json.loads(bruto, parse_float=Decimal)
        except ValueError:
            raise DbeError(f"Resposta de {servico} no gateway não é JSON.") from None

    def _post(self, servico: str, corpo: dict, jsessionid=None) -> dict:
        requisicao = urllib.request.Request(
            f"{self._gateway}/gateway/v1/mge/service.sbr?serviceName={servico}&outputType=json",
            data=json.dumps({"serviceName": servico, "requestBody": corpo}).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "Authorization": f"Bearer {jsessionid}",
            },
            method="POST",
        )
        return self._abre(requisicao, servico)

    def login(self) -> None:
        with self._lock:
            requisicao = urllib.request.Request(
                f"{self._gateway}/login",
                data=b"",
                headers={
                    "Content-Type": "application/json",
                    "AppKey": self._appkey,
                    "Token": self._token,
                    "Username": self._usuario,
                    "Password": self._senha,
                },
                method="POST",
            )
            resposta = self._abre(requisicao, "login")
            bearer = resposta.get("bearerToken")
            if not bearer:
                motivo = resposta.get("error") or "sem mensagem"
                raise DbeError(f"Login recusado pelo gateway do Sankhya: {motivo}")
            self._jsessionid = bearer

    def logout(self) -> None:
        """
        Encerra a sessão no gateway (`MobileLoginSP.logout`, como a doc do fluxo
        legado recomenda). Melhor esforço: se falhar, a sessão expira sozinha em
        30 minutos de inatividade.
        """
        with self._lock:
            bearer, self._jsessionid = self._jsessionid, None
            if not bearer:
                return
            try:
                self._post("MobileLoginSP.logout", {}, bearer)
            except Exception:
                pass


# Uma sessão por token e ambiente: cada cliente tem o seu bearer.
_sessions: dict[tuple, ApiSession] = {}
_sessions_lock = threading.Lock()
_logout_registrado = False


def _logout_todas() -> None:
    for sessao in list(_sessions.values()):
        sessao.logout()


def get_api_session(token: str, sandbox: bool = False) -> ApiSession:
    """
    A sessão do processo para esse token, criada na primeira consulta (o login é lazy).

    A credencial da Vidya é conferida aqui, e não no boot: um servidor que mistura
    clientes `direct`, `dbe` e `api` precisa subir mesmo sem ela.
    """
    appkey, usuario, senha = (os.environ.get(nome, "").strip() for nome in _ENV)
    faltam = [nome for nome, valor in zip(_ENV, (appkey, usuario, senha)) if not valor]
    if faltam:
        raise DbeError("Modo API sem configuração: defina " + ", ".join(faltam) + ".")
    if not token:
        raise DbeError("Cliente api sem api_token.")
    global _logout_registrado
    with _sessions_lock:
        chave = (token, bool(sandbox))
        if chave not in _sessions:
            _sessions[chave] = ApiSession(token, appkey, usuario, senha, bool(sandbox))
            if not _logout_registrado:
                # shortcut: não roda em SIGKILL nem SIGTERM sem handler; a sessão expira sozinha.
                atexit.register(_logout_todas)
                _logout_registrado = True
        return _sessions[chave]
