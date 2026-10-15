"""
Modo DBE do Sankhya Schema MCP: consultas pelo DB Explorer do próprio Sankhya.

Em vez de abrir conexão com o banco, o servidor autentica na aplicação com
`MobileLoginSP.login` (usuário e senha do Sankhya), guarda o `jsessionid` e manda
cada consulta para `DbExplorerSP.executeQuery`. Serve para quem alcança o Sankhya
por HTTP mas não tem rota até a porta do Oracle/SQL Server.

O DB Explorer não é um terminal SQL. Isso molda este módulo:

- só aceita SELECT/WITH (a aplicação recusa qualquer escrita);
- não tem bind: o texto vai inteiro, então `inline_params` escreve os valores
  como literal, com escape;
- devolve no máximo 5000 linhas e avisa em `burstLimit`;
- datas chegam como texto `ddMMyyyy HH:mm:ss`.

Nada aqui conhece MCP nem cliente: `server.py` pede linhas e recebe linhas.
"""

import atexit
import base64
import binascii
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from decimal import Decimal
from typing import Optional

# Tempo máximo de uma chamada HTTP. Consulta pesada no DB Explorer demora; o teto
# existe para o servidor MCP não ficar pendurado num socket morto.
TIMEOUT_SEGUNDOS = 120

# A aplicação trunca o resultado neste número de linhas e liga `burstLimit`.
LIMITE_DBE = 5000

_STATUS_OK = "1"
# Sessão inválida ou expirada: a aplicação responde `status 3` ("Não autorizado.").
_STATUS_NAO_AUTORIZADO = "3"


class DbeError(Exception):
    """Falha de comunicação, de autenticação ou erro SQL devolvido pelo DB Explorer."""


class RowCapError(DbeError):
    """
    O resultado passou do teto do DB Explorer e o chamador pediu todas as linhas.

    Devolver as primeiras 5000 como se fossem tudo é o pior desfecho: quem lê
    conclui que o resto não existe. `rows` guarda o que veio, para o chamador
    que aceita resultado parcial.
    """

    def __init__(self, rows: list[dict]):
        super().__init__(
            f"O resultado passou de {LIMITE_DBE} linhas, o teto do DB Explorer, "
            "e foi cortado. Refine a consulta."
        )
        self.rows = rows


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

def normalize_url(value: Optional[str]) -> str:
    """
    Reduz `SANKHYA_DBE_URL` ao endereço base (`http://host:porta`).

    Quem copia a URL do navegador traz `/mge`, `/mge/` ou `/mge/system.jsp`; os
    caminhos de serviço são montados aqui, então o excesso é cortado.
    """
    url = (value or "").strip()
    if not url:
        return ""
    partes = urllib.parse.urlsplit(url if "://" in url else f"http://{url}")
    # `usuario:senha@host` na URL iria parar em log e em listagem: fica só o host.
    return f"{partes.scheme}://{partes.netloc.rpartition('@')[2]}"


# ---------------------------------------------------------------------------
# Valores: literal SQL e tipos de retorno
# ---------------------------------------------------------------------------

def sql_literal(valor) -> str:
    """
    Escreve `valor` como literal SQL. É o que substitui o bind: o DB Explorer só
    recebe texto, então o escape aqui é a única defesa contra injeção.

    Texto: aspas simples dobradas (o escape nos dois bancos). Número entra cru
    e `None` vira NULL. Qualquer outro tipo é recusado — melhor falhar do que
    adivinhar como escrever um `bytes` ou um `datetime` em SQL.
    """
    if valor is None:
        return "NULL"
    # bool é subclasse de int e viraria 1/0 sem avisar: recusado de propósito.
    if isinstance(valor, bool):
        raise TypeError("Parâmetro booleano não suportado no modo DBE.")
    if isinstance(valor, (int, Decimal)):
        return str(valor)
    if isinstance(valor, float):
        return repr(valor)
    if isinstance(valor, str):
        if "\x00" in valor:
            raise ValueError("Parâmetro com caractere nulo.")
        return "'" + valor.replace("'", "''") + "'"
    raise TypeError(f"Parâmetro de tipo não suportado no modo DBE: {type(valor).__name__}.")


# Um token por vez: literal de texto (que não pode ser tocado), `:N` do Oracle,
# `%s` do SQL Server.
_TOKEN_RE = re.compile(r"'(?:[^']|'')*'|:(\d+)|(%s)")


def inline_params(sql: str, params: Optional[list], db_type: str) -> str:
    """
    Troca os placeholders de `sql` pelos valores de `params`, como literal.

    Oracle usa `:1`, `:2`... (posicional, pode repetir); SQL Server usa `%s`,
    na ordem. O que estiver entre aspas fica intacto — `'a:1'` é dado, não bind.

    Sem `params`, devolve o SQL como veio: o `%s` de um `LIKE '%s%'` escrito à
    mão pelo usuário em `run_query` não é placeholder.
    """
    if not params:
        return sql

    proximo = iter(range(len(params)))

    def troca(m: re.Match) -> str:
        if db_type == "oracle" and m.group(1) is not None:
            indice = int(m.group(1)) - 1
        elif db_type != "oracle" and m.group(2) is not None:
            indice = next(proximo, None)
        else:
            return m.group(0)
        if indice is None or not 0 <= indice < len(params):
            raise ValueError("Mais placeholders do que parâmetros.")
        return sql_literal(params[indice])

    return _TOKEN_RE.sub(troca, sql)


_DATA_RE = re.compile(r"^(\d{2})(\d{2})(\d{4})(?: (\d{2}):(\d{2}):(\d{2}))?$")


def _parse_data(texto) -> object:
    """`14102026 23:13:51` → datetime. Devolve o valor intacto se não casar."""
    if not isinstance(texto, str):
        return texto
    m = _DATA_RE.match(texto)
    if not m:
        return texto
    dia, mes, ano, hora, minuto, segundo = m.groups()
    try:
        return datetime(int(ano), int(mes), int(dia), int(hora or 0), int(minuto or 0), int(segundo or 0))
    except ValueError:
        return texto


# `userType` das colunas de data/hora. Só nelas o texto `ddMMyyyy` é data: um
# VARCHAR com oito dígitos (CEP, por exemplo) não pode virar datetime.
_TIPOS_DATA = ("H", "D")


def decode_message(texto: Optional[str]) -> str:
    """
    A aplicação às vezes manda `statusMessage` em base64 (a falha de login, por
    exemplo). Decodifica quando é base64 de texto legível; senão devolve o original.
    """
    if not texto:
        return ""
    candidato = texto.strip()
    if len(candidato) % 4 == 0 and re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", candidato):
        try:
            decodificado = base64.b64decode(candidato, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return texto
        if decodificado.isprintable():
            return decodificado
    return texto


# ---------------------------------------------------------------------------
# Sessão
# ---------------------------------------------------------------------------

class DbeSession:
    """
    Uma sessão do Sankhya aberta por `MobileLoginSP.login`, reaproveitada em todas
    as consultas do processo.

    O `jsessionid` fica em memória e nunca vai a log nem a mensagem de erro. Se a
    aplicação o recusar (expirou, o servidor reiniciou), a consulta refaz o login
    uma vez e tenta de novo.
    """

    def __init__(self, url: str, usuario: str, senha: str):
        self._url = url
        self._usuario = usuario
        self._senha = senha
        self._jsessionid: Optional[str] = None
        self._lock = threading.RLock()

    # -- HTTP ---------------------------------------------------------------

    def _post(self, servico: str, corpo: dict, jsessionid: Optional[str] = None) -> dict:
        query = {"serviceName": servico, "outputType": "json"}
        headers = {"Content-Type": "application/json; charset=UTF-8"}
        if jsessionid:
            query["mgeSession"] = jsessionid
            headers["Cookie"] = f"JSESSIONID={jsessionid}"
        if servico.startswith("DbExplorerSP"):
            query.update(application="DbExplorer", resourceID="br.com.sankhya.DbExplorer")
        endereco = f"{self._url}/mge/service.sbr?{urllib.parse.urlencode(query)}"
        requisicao = urllib.request.Request(
            endereco,
            data=json.dumps({"serviceName": servico, "requestBody": corpo}).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(requisicao, timeout=TIMEOUT_SEGUNDOS) as resposta:
                bruto = resposta.read()
        except urllib.error.HTTPError as exc:
            raise DbeError(f"O Sankhya respondeu HTTP {exc.code} em {servico}.") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            motivo = getattr(exc, "reason", exc)
            raise DbeError(f"Não foi possível falar com o Sankhya em {self._url}: {motivo}") from None
        try:
            # parse_float=Decimal: o Oracle entrega NUMBER como Decimal e o float
            # perderia casas em valor monetário.
            return json.loads(bruto, parse_float=Decimal)
        except ValueError:
            raise DbeError(
                f"Resposta de {servico} não é JSON. A URL aponta para o Sankhya?"
            ) from None

    # -- login / logout -----------------------------------------------------

    def login(self) -> None:
        with self._lock:
            resposta = self._post(
                "MobileLoginSP.login",
                {
                    "NOMUSU": {"$": self._usuario},
                    "INTERNO": {"$": self._senha},
                    "KEEPCONNECTED": {"$": "N"},
                },
            )
            if str(resposta.get("status")) != _STATUS_OK:
                motivo = decode_message(resposta.get("statusMessage")) or "sem mensagem"
                raise DbeError(f"Login recusado pelo Sankhya (MobileLoginSP.login): {motivo}")
            try:
                self._jsessionid = resposta["responseBody"]["jsessionid"]["$"]
            except (KeyError, TypeError):
                raise DbeError("O login respondeu sem jsessionid.") from None

    def logout(self) -> None:
        """Encerra a sessão no servidor. Melhor esforço: falhar aqui não ensina nada."""
        with self._lock:
            sessao, self._jsessionid = self._jsessionid, None
            if not sessao:
                return
            try:
                self._post("MobileLoginSP.logout", {}, sessao)
            except Exception:
                pass

    # -- consulta -----------------------------------------------------------

    def execute(self, sql: str) -> tuple[list[str], list[list], list[str], bool]:
        """
        Roda `sql` no DB Explorer. Devolve (colunas, linhas, userTypes, burst).

        `burst` é True quando a aplicação cortou o resultado em 5000 linhas.
        """
        # O `;` final que a allowlist de `run_query` tolera o DB Explorer recusa
        # (ORA-00933): é terminador de cliente SQL, não parte do comando. A
        # recusa de escrita é da própria aplicação (CORE_E03690).
        sql = sql.strip().rstrip(";").rstrip()
        with self._lock:
            if not self._jsessionid:
                self.login()
            resposta = self._consulta(sql)
            if str(resposta.get("status")) == _STATUS_NAO_AUTORIZADO:
                self.login()
                resposta = self._consulta(sql)
        if str(resposta.get("status")) != _STATUS_OK:
            raise DbeError(decode_message(resposta.get("statusMessage")) or "Erro sem mensagem.")
        corpo = resposta.get("responseBody") or {}
        metadados = corpo.get("fieldsMetadata") or []
        return (
            [m["name"] for m in metadados],
            corpo.get("rows") or [],
            [m.get("userType") for m in metadados],
            bool(corpo.get("burstLimit")),
        )

    def _consulta(self, sql: str) -> dict:
        return self._post("DbExplorerSP.executeQuery", {"sql": sql}, self._jsessionid)


# ---------------------------------------------------------------------------
# Sessão do processo
# ---------------------------------------------------------------------------

# Uma sessão por (URL, usuário, senha): cada cliente tem a sua, e dois clientes
# que apontam para o mesmo Sankhya com a mesma credencial compartilham o login.
_sessions: dict[tuple[str, str, str], DbeSession] = {}
_sessions_lock = threading.Lock()
_logout_registrado = False


def _logout_todas() -> None:
    for sessao in list(_sessions.values()):
        sessao.logout()


def get_session(url: str, usuario: str, senha: str) -> DbeSession:
    """A sessão do processo para essa credencial, criada na primeira consulta (o login é lazy)."""
    global _logout_registrado
    if not (url and usuario and senha):
        raise DbeError(
            "Modo DBE sem configuração: defina SANKHYA_DBE_URL, "
            "SANKHYA_DBE_USER e SANKHYA_DBE_PASSWORD."
        )
    with _sessions_lock:
        chave = (url, usuario, senha)
        if chave not in _sessions:
            _sessions[chave] = DbeSession(url, usuario, senha)
            if not _logout_registrado:
                atexit.register(_logout_todas)
                _logout_registrado = True
        return _sessions[chave]


def run(session: DbeSession, sql: str, limit: Optional[int]) -> tuple[list[dict], bool]:
    """
    Executa `sql` e devolve (linhas como dict de chave minúscula, truncado).

    Mesmo contrato do `fetch_rows` do modo direto, com uma diferença: com
    `limit=None` o `truncado` pode ser True, quando o resultado bateu no teto de
    5000 linhas do DB Explorer. No modo direto isso nunca acontece.
    """
    colunas, linhas, tipos, burst = session.execute(sql)
    chaves = [c.lower() for c in colunas]
    eh_data = [t in _TIPOS_DATA for t in tipos]
    resultado = [
        {
            chave: _parse_data(valor) if data else valor
            for chave, valor, data in zip(chaves, linha, eh_data)
        }
        for linha in (linhas if limit is None else linhas[: limit + 1])
    ]
    if limit is None:
        return resultado, burst
    return resultado[:limit], len(resultado) > limit or burst
