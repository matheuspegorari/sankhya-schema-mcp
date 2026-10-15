"""
Clientes do Sankhya Schema MCP.

Um cliente é um destino de consulta: o dialeto do banco, o schema das tabelas e a
forma de chegar lá. O modo de acesso é de cada cliente: `direct` conecta no banco
pelo driver, `dbe` consulta pelo DB Explorer do Sankhya, e um mesmo servidor pode
misturar os dois. O servidor sempre tem ao menos um cliente:

- com `clients.json`: um cliente por entrada do arquivo, e cada entrada declara o
  próprio `mode` (obrigatório) e traz as credenciais dele (`db_*` no `direct`,
  `dbe_*` no `dbe`). É o único caso com mais de um;
- sem `clients.json`: um cliente só, `default`, montado das variáveis `SANKHYA_*`
  do `.env`, onde `SANKHYA_MODE` (obrigatório) define o modo.

O cliente da chamada em curso vive num `ContextVar`: cada tool o define na
entrada, e `query()`, `connect()` e companhia o leem sem receber parâmetro.

Nada aqui conhece MCP nem banco: só configuração.
"""

import os
import re
import stat
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from dbe import normalize_url

# Lido aqui, e não só no `dialects.py`, porque o cadastro de clientes é montado no
# import deste módulo e precisa do `.env` já carregado.
load_dotenv()

DB_TYPES = ("oracle", "sqlserver")
_MODES = ("direct", "dbe")

PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_CLIENTS_FILE = PROJECT_ROOT / "clients.json"
DEFAULT_CLIENT_NAME = "default"


class ClientError(ValueError):
    """Cliente inexistente, ambíguo ou configuração de cliente inválida."""


# ---------------------------------------------------------------------------
# Validação dos valores
# ---------------------------------------------------------------------------

def resolve_db_type(value: Optional[str]) -> str:
    """
    Normaliza o valor de `SANKHYA_DB_TYPE`.

    Ausente ou vazio cai em `oracle`: era o único banco suportado, e uma
    instalação existente não pode passar a exigir configuração nova.
    Valor desconhecido falha alto — conectar no banco errado é pior que não subir.
    """
    tipo = (value or "").strip().lower() or "oracle"
    if tipo not in DB_TYPES:
        raise ValueError(
            f"SANKHYA_DB_TYPE inválido: {value!r}. Use 'oracle' ou 'sqlserver'."
        )
    return tipo


_IDENTIFIER_RE = re.compile(r"^[A-Z][A-Z0-9_$#]*$")


def resolve_schema(value: Optional[str]) -> Optional[str]:
    """
    Normaliza `SANKHYA_DB_SCHEMA`. Ausente ou vazio devolve None.

    O nome é interpolado no texto das queries e do `ALTER SESSION` — nenhum dos
    dois aceita bind para identificador —, então é validado aqui. Valor com
    ponto, espaço ou pontuação falha alto em vez de virar SQL.
    """
    schema = (value or "").strip().upper()
    if not schema:
        return None
    if not _IDENTIFIER_RE.match(schema):
        raise ValueError(
            f"SANKHYA_DB_SCHEMA inválido: {value!r}. Use apenas o nome do "
            "schema, sem ponto nem espaço."
        )
    return schema


def resolve_mode(value: Optional[str], origem: str = "SANKHYA_MODE") -> str:
    """
    Normaliza o modo de acesso: `SANKHYA_MODE` no `.env`, `mode` no `clients.json`.
    É obrigatório: não há modo padrão.

    Ausente falha alto, como valor desconhecido. Um padrão tentaria abrir
    conexão com o banco justamente onde só o DB Explorer é alcançável, e o erro
    apareceria como "conexão recusada" em vez de dizer o que falta.
    """
    modo = (value or "").strip().lower()
    if not modo:
        raise ValueError(f"{origem} é obrigatório: use 'direct' ou 'dbe'.")
    if modo not in _MODES:
        raise ValueError(f"{origem} inválido: {value!r}. Use 'direct' ou 'dbe'.")
    return modo


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Client:
    """
    Um destino de consulta. Imutável: ninguém muda o banco de um cliente no meio
    de uma conversa.

    Senhas ficam fora do `repr` — um cliente impresso em log ou em traceback não
    pode entregá-las.
    """

    name: str
    mode: str = "direct"
    db_type: str = "oracle"
    schema: Optional[str] = None
    descricao: str = ""
    default: bool = False

    # Conexão direta (usada quando `mode == "direct"`)
    host: str = "localhost"
    port: int = 1521
    sid: str = "XE"
    service_name: Optional[str] = None
    database: str = "jiva"
    user: str = "SANKHYA"
    # `developer` é a senha dos containers de desenvolvimento distribuídos pela
    # Sankhya (skdev-oracle e skdev-mssql), que é o cenário padrão deste MCP.
    password: str = field(default="developer", repr=False)

    # DB Explorer (usado quando `mode == "dbe"`)
    dbe_url: str = ""
    dbe_user: str = ""
    dbe_password: str = field(default="", repr=False)

    @property
    def rotulo(self) -> str:
        """`acme · Oracle · DBE` — o que vai no cabeçalho das respostas."""
        banco = "Oracle" if self.db_type == "oracle" else "SQL Server"
        acesso = "DBE" if self.mode == "dbe" else "direto"
        return f"{self.name} · {banco} · {acesso}"

    @property
    def destino(self) -> str:
        """Onde o cliente aponta, sem credencial: URL do Sankhya ou host do banco."""
        if self.mode == "dbe":
            return self.dbe_url
        return f"{self.host}:{self.port}"


# ---------------------------------------------------------------------------
# Montagem a partir do ambiente (modo direct e dbe sem cadastro)
# ---------------------------------------------------------------------------

def client_from_env(env=None) -> Client:
    """
    O cliente `default` descrito pelas variáveis `SANKHYA_*`.

    No modo `dbe` exige as variáveis do DB Explorer; no `direct` valem os padrões
    dos containers de desenvolvimento da Sankhya. Falta de variável derruba o boot
    em vez de virar erro em cada tool — sem usuário e senha do Sankhya nenhuma
    consulta vai funcionar.
    """
    env = os.environ if env is None else env
    modo = resolve_mode(env.get("SANKHYA_MODE"))
    tipo = resolve_db_type(env.get("SANKHYA_DB_TYPE"))
    try:
        porta = int(env.get("SANKHYA_DB_PORT") or ("1433" if tipo == "sqlserver" else "1521"))
    except ValueError:
        raise ValueError(f"SANKHYA_DB_PORT inválido: {env.get('SANKHYA_DB_PORT')!r}.") from None

    dbe = {
        "url": normalize_url(env.get("SANKHYA_DBE_URL")),
        "user": (env.get("SANKHYA_DBE_USER") or "").strip(),
        "password": env.get("SANKHYA_DBE_PASSWORD") or "",
    }
    if modo == "dbe":
        faltam = [
            nome
            for nome, chave in (
                ("SANKHYA_DBE_URL", "url"),
                ("SANKHYA_DBE_USER", "user"),
                ("SANKHYA_DBE_PASSWORD", "password"),
            )
            if not dbe[chave]
        ]
        if faltam:
            raise ValueError("SANKHYA_MODE=dbe exige as variáveis: " + ", ".join(faltam) + ".")

    return Client(
        name=DEFAULT_CLIENT_NAME,
        mode=modo,
        db_type=tipo,
        schema=resolve_schema(env.get("SANKHYA_DB_SCHEMA")),
        default=True,
        host=env.get("SANKHYA_DB_HOST") or "localhost",
        port=porta,
        sid=env.get("SANKHYA_DB_SERVICE") or "XE",
        service_name=env.get("SANKHYA_DB_SERVICE_NAME") or None,
        database=env.get("SANKHYA_DB_DATABASE") or "jiva",
        user=env.get("SANKHYA_DB_USER") or "SANKHYA",
        password=env.get("SANKHYA_DB_PASSWORD") or "developer",
        dbe_url=dbe["url"],
        dbe_user=dbe["user"],
        dbe_password=dbe["password"],
    )


# ---------------------------------------------------------------------------
# Cadastro (clients.json)
# ---------------------------------------------------------------------------

# Chaves aceitas em cada entrada. Seguem o nome da variável de ambiente sem o
# `SANKHYA_` (`db_type` ↔ `SANKHYA_DB_TYPE`), então quem conhece o `.env` já
# conhece o arquivo. `mode` é a exceção: só existe no arquivo, e escolhe qual
# conjunto de credencial vale (`db_*` ou `dbe_*`). Uma entrada pode trazer os dois,
# e o outro fica guardado para quando ela trocar de modo. Chave desconhecida é
# erro, não é ignorada.
_REGISTRY_KEYS = {
    "mode", "descricao", "default", "db_type", "db_schema",
    "db_host", "db_port", "db_service", "db_service_name", "db_database",
    "db_user", "db_password",
    "dbe_url", "dbe_user", "dbe_password",
}

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _client_from_entry(name: str, entry: dict) -> Client:
    contexto = f"Cliente {name!r} em clients.json"
    if not _NAME_RE.match(name):
        raise ClientError(
            f"{contexto}: nome inválido. Use letras, números, ponto, hífen e sublinhado."
        )
    if not isinstance(entry, dict):
        raise ClientError(f"{contexto}: esperava um objeto com as chaves do cliente.")
    desconhecidas = sorted(set(entry) - _REGISTRY_KEYS)
    if desconhecidas:
        raise ClientError(
            f"{contexto}: chave(s) desconhecida(s): {', '.join(desconhecidas)}. "
            f"Aceitas: {', '.join(sorted(_REGISTRY_KEYS))}."
        )
    padrao = entry.get("default", False)
    if not isinstance(padrao, bool):
        raise ClientError(f'{contexto}: "default" deve ser true ou false.')

    def texto(chave: str) -> str:
        return str(entry.get(chave) or "").strip()

    try:
        modo = resolve_mode(entry.get("mode"), "mode")
    except ValueError as exc:
        raise ClientError(f"{contexto}: {exc}") from None

    # Só as credenciais do modo da entrada são exigidas: o outro conjunto é opcional.
    obrigatorias = {
        "dbe": ("dbe_url", "dbe_user", "dbe_password"),
        "direct": ("db_host", "db_user", "db_password"),
    }[modo]
    faltam = [chave for chave in obrigatorias if not texto(chave)]
    if faltam:
        raise ClientError(f"{contexto}: o modo {modo} exige {', '.join(faltam)}.")

    try:
        tipo = resolve_db_type(entry.get("db_type"))
        porta = texto("db_port")
        try:
            porta = int(porta) if porta else (1433 if tipo == "sqlserver" else 1521)
        except ValueError:
            raise ValueError(f"db_port inválido: {entry.get('db_port')!r}.") from None
        return Client(
            name=name,
            mode=modo,
            db_type=tipo,
            schema=resolve_schema(entry.get("db_schema")),
            descricao=texto("descricao"),
            default=padrao,
            host=texto("db_host") or "localhost",
            port=porta,
            sid=texto("db_service") or "XE",
            service_name=texto("db_service_name") or None,
            database=texto("db_database") or "jiva",
            user=texto("db_user") or "SANKHYA",
            password=str(entry.get("db_password") or ""),
            dbe_url=normalize_url(texto("dbe_url")),
            dbe_user=texto("dbe_user"),
            dbe_password=str(entry.get("dbe_password") or ""),
        )
    except ValueError as exc:
        raise ClientError(f"{contexto}: {exc}") from None


def parse_registry(data) -> dict[str, Client]:
    """
    Transforma o conteúdo do `clients.json` em clientes, cada um no seu `mode`,
    validando tudo.

    O nome é a chave do objeto; duas chaves iguais em caixa diferente (`Acme` e
    `acme`) seriam o mesmo cliente para quem digita, então são recusadas.
    No máximo um cliente pode ser `"default": true`.
    """
    if not isinstance(data, dict) or not data:
        raise ClientError("clients.json deve ser um objeto com ao menos um cliente.")

    clientes: dict[str, Client] = {}
    vistos: dict[str, str] = {}
    for nome, entrada in data.items():
        cliente = _client_from_entry(nome, entrada)
        if nome.lower() in vistos:
            raise ClientError(
                f"Clientes {vistos[nome.lower()]!r} e {nome!r} diferem só na caixa: "
                "o nome é case-insensitive."
            )
        vistos[nome.lower()] = nome
        clientes[nome] = cliente

    padroes = [c.name for c in clientes.values() if c.default]
    if len(padroes) > 1:
        raise ClientError(
            "Só um cliente pode ser default, e há " f"{len(padroes)}: {', '.join(padroes)}."
        )
    return clientes


def load_registry(caminho: Path) -> dict[str, Client]:
    """Lê e valida o arquivo. JSON quebrado vira erro com o arquivo e a linha."""
    import json

    try:
        data = json.loads(caminho.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ClientError(f"Não foi possível ler {caminho}: {exc.strerror or exc}.") from None
    except ValueError as exc:
        raise ClientError(f"{caminho} não é um JSON válido: {exc}") from None
    _avisa_permissao(caminho)
    return parse_registry(data)


def _avisa_permissao(caminho: Path) -> None:
    """
    O arquivo guarda senhas em texto puro. Se outros usuários da máquina podem lê-lo,
    avisa — não recusa: bloquear a subida por permissão seria pior que o aviso.
    """
    try:
        modo = caminho.stat().st_mode
    except OSError:
        return
    if os.name == "posix" and modo & (stat.S_IRWXG | stat.S_IRWXO):
        print(
            f"AVISO: {caminho} guarda senhas e é legível por outros usuários. "
            f"Rode: chmod 600 {caminho}",
            file=sys.stderr,
        )


def load_clients(env=None) -> dict[str, Client]:
    """
    Os clientes do processo, na ordem de precedência:

    1. `SANKHYA_CLIENTS_FILE`, que precisa existir;
    2. `clients.json` na raiz do projeto;
    3. as variáveis `SANKHYA_*`, como um cliente só chamado `default`.

    Com cadastro, o `.env` não é lido para modo nem conexão: cada cliente traz os
    seus. `SANKHYA_MODE` só é exigido no terceiro caso.
    """
    env = os.environ if env is None else env

    indicado = (env.get("SANKHYA_CLIENTS_FILE") or "").strip()
    if indicado:
        caminho = Path(indicado).expanduser()
        if not caminho.is_file():
            raise ClientError(f"SANKHYA_CLIENTS_FILE aponta para um arquivo que não existe: {caminho}")
    elif DEFAULT_CLIENTS_FILE.is_file():
        caminho = DEFAULT_CLIENTS_FILE
    else:
        base = client_from_env(env)
        return {base.name: base}
    return load_registry(caminho)


CLIENTS: dict[str, Client] = load_clients()


# ---------------------------------------------------------------------------
# Seleção
# ---------------------------------------------------------------------------

def is_multi() -> bool:
    return len(CLIENTS) > 1


def default_client() -> Optional[Client]:
    """O cliente usado quando a chamada não informa nenhum, ou None se não houver."""
    if len(CLIENTS) == 1:
        return next(iter(CLIENTS.values()))
    return next((c for c in CLIENTS.values() if c.default), None)


def _lista_de_clientes() -> str:
    return ", ".join(f"`{n}`" for n in CLIENTS)


def select_client(name: Optional[str]) -> Client:
    """
    Resolve o nome informado pela chamada. Sem nome, vale o default; sem default
    e com mais de um cliente, é erro — escolher por conta própria devolveria dado
    do cliente errado sem nenhum erro de SQL.
    """
    nome = (name or "").strip()
    if not nome:
        cliente = default_client()
        if cliente is None:
            raise ClientError(
                f"Informe o cliente (parâmetro `client`): {_lista_de_clientes()}. "
                "Nenhuma consulta foi executada."
            )
        return cliente
    for candidato in CLIENTS.values():
        if candidato.name.lower() == nome.lower():
            return candidato
    raise ClientError(
        f"Cliente `{nome}` não cadastrado. Disponíveis: {_lista_de_clientes()}. "
        "Nenhuma consulta foi executada."
    )


_ativo: ContextVar[Optional[Client]] = ContextVar("sankhya_cliente_ativo", default=None)


def active_client() -> Client:
    """
    O cliente da chamada em curso. Fora de uma tool vale o default, se existir:
    com mais de um cliente e nenhum default, levanta em vez de escolher.
    """
    return _ativo.get() or select_client(None)


@contextmanager
def use_client(cliente: Client):
    """Faz `cliente` valer como o ativo dentro do bloco, e restaura o anterior ao sair."""
    token = _ativo.set(cliente)
    try:
        yield cliente
    finally:
        _ativo.reset(token)
