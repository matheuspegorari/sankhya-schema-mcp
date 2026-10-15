r"""
Autoteste das funções puras do servidor MCP (não requer banco).

Execute:
    Windows: .\.venv\Scripts\python.exe test_server.py
    Linux:   .venv/bin/python test_server.py
"""

import os
import dataclasses
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

# Hermético: o modo e o cadastro de clientes vêm daqui, e não do `.env` nem de um
# `clients.json` que o desenvolvedor tenha na pasta. Precisa vir antes dos imports
# abaixo: `clients.py` monta o cadastro quando é importado.
os.environ["SANKHYA_MODE"] = "direct"
_cadastro_dos_testes = Path(tempfile.mkdtemp()) / "clients.json"
_cadastro_dos_testes.write_text(
    '{"default": {"default": true, "db_type": "oracle", "db_host": "localhost",'
    ' "db_user": "SANKHYA", "db_password": "developer"}}',
    encoding="utf-8",
)
os.environ["SANKHYA_CLIENTS_FILE"] = str(_cadastro_dos_testes)


# Os testes rodam contra o cliente `default` montado do ambiente, e cada trecho
# que precisa de outro dialeto, schema ou modo o troca por um instante.
@contextmanager
def _como(**mudancas):
    """Ativa uma cópia do cliente ativo com `mudancas` aplicadas, só dentro do bloco."""
    with use_client(dataclasses.replace(active_client(), **mudancas)) as cliente:
        yield cliente


import clients  # noqa: E402
import dbe  # noqa: E402
import dialects  # noqa: E402
import server  # noqa: E402
import updates  # noqa: E402
from clients import Client, active_client, use_client  # noqa: E402
from dialects import (  # noqa: E402
    BEGIN_READ_ONLY,
    QUERIES,
    group_prefix_counts,
    group_prefixes,
    resolve_db_type,
    resolve_mode,
    resolve_schema,
    schema_prefix,
)
from updates import parse_version, pick_latest  # noqa: E402
from version import __version__  # noqa: E402
from server import (  # noqa: E402
    assert_read_only_query,
    assert_safe_identifier,
    pick_owner,
    rows_to_markdown,
    select_columns,
    truncation_note,
    DEFAULT_ROW_LIMIT,
    SAMPLE_COLUMN_LIMIT,
)


def test_read_only_aceita_leitura():
    assert assert_read_only_query("SELECT 1 FROM DUAL") is None
    assert assert_read_only_query("  select nunota from tgfcab  ") is None
    assert assert_read_only_query("WITH x AS (SELECT 1 A FROM DUAL) SELECT * FROM x") is None
    assert assert_read_only_query("SELECT 1 FROM DUAL;") is None


def test_read_only_bloqueia_escrita():
    for sql in [
        "",
        "   ",
        "DELETE FROM TGFCAB",
        "UPDATE TGFCAB SET VLRNOTA = 0",
        "INSERT INTO TGFCAB (NUNOTA) VALUES (1)",
        "DROP TABLE TGFCAB",
        "BEGIN NULL; END;",
        "SELECT 1 FROM DUAL; DELETE FROM TGFCAB",
        "WITH FUNCTION f RETURN NUMBER IS BEGIN RETURN 1; END; SELECT f FROM DUAL",
        "-- SELECT 1\nDELETE FROM TGFCAB",
        "/* SELECT 1 */ DELETE FROM TGFCAB",
    ]:
        assert assert_read_only_query(sql) is not None, f"deveria bloquear: {sql!r}"


def test_identificador_seguro():
    for nome in ["TGFCAB", "AD_MINHA_TABELA", "SANKHYA.TGFCAB", "T$X#1"]:
        assert assert_safe_identifier(nome) is None, f"deveria aceitar: {nome!r}"

    for nome in [
        "",
        None,
        "TGFCAB WHERE 1=1",
        "DUAL UNION SELECT PASSWORD FROM TSIUSU",
        "TGFCAB; DELETE FROM TGFCAB",
        "tgfcab",  # resolve_table_name sempre entrega maiúsculas
    ]:
        assert assert_safe_identifier(nome) is not None, f"deveria bloquear: {nome!r}"


def test_markdown():
    assert rows_to_markdown([]) == "_Nenhum resultado encontrado._"
    md = rows_to_markdown([{"a": 1, "b": None}])
    assert md.splitlines() == ["| a | b |", "| --- | --- |", "| 1 |  |"]


def test_markdown_preserva_valores_falsy():
    # Só NULL pode virar célula vazia: zero é um valor, e quem lê a tabela
    # não tem como distinguir "0" apagado de "sem valor".
    md = rows_to_markdown([
        {"zero": 0, "falso": False, "decimal": Decimal("0.00"), "vazio": "", "nulo": None},
    ])
    assert md.splitlines()[-1] == "| 0 | False | 0.00 |  |  |"


def test_markdown_escapa_pipe():
    # Comentário de coluna do dicionário Sankhya pode conter `|`, que sem escape
    # cria colunas fantasma e desalinha a tabela inteira.
    md = rows_to_markdown([{"comentario": "Situação: A|I|C"}])
    assert md.splitlines()[-1] == r"| Situação: A\|I\|C |"


def test_owner_unico_por_tabela():
    # Com a tabela em vários schemas visíveis, vence o do usuário conectado.
    with _como(user="sankhya"):
        assert pick_owner({"AUDIT", "SANKHYA", "TESTE"}) == "SANKHYA"
        # Sem o schema conectado entre eles, vence o primeiro em ordem alfabética.
        assert pick_owner({"TESTE", "AUDIT"}) == "AUDIT"
        assert pick_owner({"AUDIT"}) == "AUDIT"


def test_aviso_de_truncamento():
    assert truncation_note(False, DEFAULT_ROW_LIMIT) == ""
    assert str(DEFAULT_ROW_LIMIT) in truncation_note(True, DEFAULT_ROW_LIMIT)


# --- Projeção de colunas da amostra (select_columns) ----------------------


def _linhas_largas(qtd_colunas=100, qtd_linhas=3):
    """Linhas no formato que fetch_rows devolve: chaves minúsculas, na ordem da tabela."""
    return [
        {f"col{i:03d}": f"v{linha}_{i}" for i in range(qtd_colunas)}
        for linha in range(qtd_linhas)
    ]


def test_projecao_corta_no_teto_e_informa_o_resto():
    linhas = _linhas_largas(qtd_colunas=600)
    projetadas, ausentes, cortadas = select_columns(linhas)
    assert len(projetadas) == len(linhas)  # corta coluna, nunca linha
    assert list(projetadas[0]) == [f"col{i:03d}" for i in range(SAMPLE_COLUMN_LIMIT)]
    assert ausentes == []
    assert cortadas == 600 - SAMPLE_COLUMN_LIMIT


def test_projecao_sem_teto_quando_tabela_e_estreita():
    projetadas, ausentes, cortadas = select_columns(_linhas_largas(qtd_colunas=3))
    assert len(projetadas[0]) == 3
    assert (ausentes, cortadas) == ([], 0)


def test_projecao_respeita_colunas_pedidas():
    linhas = _linhas_largas(qtd_colunas=600)
    projetadas, ausentes, cortadas = select_columns(linhas, "col005,col001")
    # Ordem de exibição é a que o usuário pediu, não a da tabela.
    assert list(projetadas[0]) == ["col005", "col001"]
    assert projetadas[0]["col005"] == "v0_5"
    # Pedido explícito não é cortado pelo teto, mesmo acima dele.
    assert (ausentes, cortadas) == ([], 0)
    acima_do_teto = ",".join(f"col{i:03d}" for i in range(SAMPLE_COLUMN_LIMIT + 10))
    projetadas, _, cortadas = select_columns(linhas, acima_do_teto)
    assert len(projetadas[0]) == SAMPLE_COLUMN_LIMIT + 10
    assert cortadas == 0


def test_projecao_e_case_insensitive_e_ignora_espacos():
    linhas = [{"nunota": 1, "codparc": 2, "vlrnota": 3.0}]
    projetadas, ausentes, _ = select_columns(linhas, " NUNOTA , VlrNota ")
    assert projetadas == [{"nunota": 1, "vlrnota": 3.0}]
    assert ausentes == []


def test_projecao_denuncia_coluna_inexistente():
    linhas = [{"nunota": 1, "codparc": 2}]
    projetadas, ausentes, _ = select_columns(linhas, "NUNOTA,NAOEXISTE")
    # A coluna válida sai; a inválida não é silenciada.
    assert projetadas == [{"nunota": 1}]
    assert ausentes == ["NAOEXISTE"]


def test_projecao_sem_nenhuma_coluna_valida_volta_vazia():
    linhas = [{"nunota": 1, "codparc": 2}]
    projetadas, ausentes, _ = select_columns(linhas, "FOO, BAR")
    # Linhas vazias sinalizam ao chamador que não há tabela a exibir, só erro.
    assert projetadas == []
    assert ausentes == ["FOO", "BAR"]


def test_projecao_sem_linhas_nao_quebra():
    assert select_columns([], "NUNOTA") == ([], [], 0)


# --- Dublês de banco: fetch_rows sem Oracle -------------------------------


class _FakeCursor:
    def __init__(self, total):
        self._total = total
        self.description = [("N",)]
        self.executado = []
        self.fetchall_calls = 0
        self.fetchmany_args = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self.executado.append(sql)

    def fetchall(self):
        self.fetchall_calls += 1
        return [(i,) for i in range(self._total)]

    def fetchmany(self, n):
        self.fetchmany_args.append(n)
        return [(i,) for i in range(min(n, self._total))]


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def cursor(self):
        return self._cursor

    def rollback(self):
        pass


def _fetch_com_banco_falso(total, limit, db_type="oracle"):
    """Roda fetch_rows contra um cursor falso e devolve (linhas, truncado, cursor)."""
    cursor = _FakeCursor(total)

    @contextmanager
    def _connect_falso():
        yield _FakeConn(cursor)

    original_connect = server.connect
    server.connect = _connect_falso
    try:
        with _como(db_type=db_type):
            rows, truncated = server.fetch_rows("SELECT 1 FROM DUAL", limit=limit)
    finally:
        server.connect = original_connect
    return rows, truncated, cursor


def test_sem_teto_traz_a_tabela_inteira():
    # Uma tabela Oracle chega a 1000 colunas: describe_table nao pode cortar nenhuma.
    rows, truncated, cursor = _fetch_com_banco_falso(total=1000, limit=None)
    assert len(rows) == 1000
    assert truncated is False
    assert cursor.fetchall_calls == 1
    assert cursor.fetchmany_args == []


def test_com_teto_corta_e_sinaliza():
    rows, truncated, cursor = _fetch_com_banco_falso(total=1500, limit=DEFAULT_ROW_LIMIT)
    assert len(rows) == DEFAULT_ROW_LIMIT
    assert truncated is True
    # Busca limit+1 para detectar o corte sem um COUNT extra.
    assert cursor.fetchmany_args == [DEFAULT_ROW_LIMIT + 1]


def test_resultado_exato_no_teto_nao_e_truncado():
    rows, truncated, _ = _fetch_com_banco_falso(total=DEFAULT_ROW_LIMIT, limit=DEFAULT_ROW_LIMIT)
    assert len(rows) == DEFAULT_ROW_LIMIT
    assert truncated is False


def test_limite_zero_nao_esvazia_resultado():
    # limit=0 devolvia ([], truncado=True): query com linhas anunciada como vazia.
    rows, truncated, _ = _fetch_com_banco_falso(total=3, limit=0)
    assert len(rows) == 1
    assert truncated is True


def test_limite_negativo_nao_corta_o_fim():
    # rows[:-5] descartaria o fim do resultado em silêncio.
    rows, truncated, _ = _fetch_com_banco_falso(total=3, limit=-5)
    assert len(rows) == 1
    assert truncated is True


def test_aviso_de_truncamento_usa_o_limite_padrao():
    assert truncation_note(True) == truncation_note(True, DEFAULT_ROW_LIMIT)


def test_sessao_sempre_read_only():
    _, _, cursor = _fetch_com_banco_falso(total=5, limit=None)
    assert "SET TRANSACTION READ ONLY" in cursor.executado


def test_sessao_sqlserver_abre_transacao_explicita():
    # O SQL Server não tem READ ONLY de sessão: a transação explícita é o mais
    # próximo, e sem ela uma escrita que escapasse da validação ficaria gravada.
    _, _, cursor = _fetch_com_banco_falso(total=5, limit=None, db_type="sqlserver")
    assert "BEGIN TRANSACTION" in cursor.executado
    assert "SET TRANSACTION READ ONLY" not in cursor.executado


# --- Dialetos --------------------------------------------------------------


@contextmanager
def _dialeto(tipo):
    """Ativa um dialeto para o trecho, e devolve o anterior ao sair."""
    with _como(db_type=tipo):
        yield


def test_selecao_de_dialeto_por_env():
    # Ausente/vazio cai em oracle: instalação existente não pode passar a exigir
    # configuração nova para continuar funcionando.
    assert resolve_db_type(None) == "oracle"
    assert resolve_db_type("") == "oracle"
    assert resolve_db_type("   ") == "oracle"
    assert resolve_db_type("oracle") == "oracle"
    assert resolve_db_type("sqlserver") == "sqlserver"
    # Tolerante a maiúsculas e espaços: o valor vem digitado à mão no .env.
    assert resolve_db_type(" SqlServer ") == "sqlserver"


def test_dialeto_invalido_falha_alto():
    # Conectar no banco errado é pior que não subir.
    for valor in ["postgres", "mssql", "sql server"]:
        try:
            resolve_db_type(valor)
        except ValueError:
            continue
        raise AssertionError(f"deveria recusar: {valor!r}")


def test_dialetos_expoem_o_mesmo_conjunto_de_queries():
    # Query que exista só de um lado vira AttributeError em produção, no banco
    # que ninguém testou.
    assert set(QUERIES["oracle"]) == set(QUERIES["sqlserver"])


def test_cada_dialeto_usa_o_catalogo_do_seu_banco():
    assert "ALL_TAB_COLUMNS" in QUERIES["oracle"]["columns"]
    assert "INFORMATION_SCHEMA.COLUMNS" in QUERIES["sqlserver"]["columns"]
    assert "ALL_INDEXES" in QUERIES["oracle"]["indexes"]
    assert "sys.indexes" in QUERIES["sqlserver"]["indexes"]
    assert "ALL_CONSTRAINTS" in QUERIES["oracle"]["foreign_keys"]
    assert "sys.foreign_keys" in QUERIES["sqlserver"]["foreign_keys"]
    # TDDINS é tabela da aplicação: idêntica nos dois, não duplicada.
    for nome in ["resolve_table", "instances_by_table", "search_entities"]:
        assert "TDDINS" in QUERIES["oracle"][nome]
        assert QUERIES["oracle"][nome] == QUERIES["sqlserver"][nome]


def test_placeholder_de_bind_por_dialeto():
    with _dialeto("oracle"):
        assert ":1" in dialects.query("columns")
        assert "%s" not in dialects.query("columns")
    with _dialeto("sqlserver"):
        assert "%s" in dialects.query("columns")
        assert ":1" not in dialects.query("columns")


def test_placeholder_resolvido_tambem_no_filtro_opcional():
    # search_columns monta o filtro sem saber qual banco está ativo.
    with _dialeto("oracle"):
        sql = dialects.query("columns_search", filtro="AND c.TABLE_NAME LIKE {p2}")
        assert "LIKE :2" in sql
    with _dialeto("sqlserver"):
        sql = dialects.query("columns_search", filtro="AND c.TABLE_NAME LIKE {p2}")
        assert sql.count("%s") == 2


def test_amostra_usa_o_limitador_de_linhas_do_banco():
    with _dialeto("oracle"):
        assert dialects.query("table_sample", tabela="TGFCAB") == (
            "SELECT * FROM TGFCAB WHERE ROWNUM <= :1"
        )
    with _dialeto("sqlserver"):
        assert dialects.query("table_sample", tabela="TGFCAB") == (
            "SELECT TOP (%s) * FROM TGFCAB"
        )


def test_search_entities_tem_um_bind_por_ocorrencia():
    # `fetch_rows` manda dois parâmetros: o pymssql não reaproveita bind
    # posicional como o Oracle faz com `:1` repetido.
    with _dialeto("sqlserver"):
        assert dialects.query("search_entities", filtro="").count("%s") == 2
    with _dialeto("oracle"):
        sql = dialects.query("search_entities", filtro="")
        assert ":1" in sql and ":2" in sql


def test_begin_read_only_por_dialeto():
    assert BEGIN_READ_ONLY["oracle"] == "SET TRANSACTION READ ONLY"
    assert BEGIN_READ_ONLY["sqlserver"] == "BEGIN TRANSACTION"


# --- Agrupamento de módulos (list_modules) ---------------------------------


def test_agrupa_prefixo_de_tabela():
    # Contrato NOVO: prefixo de 3 caracteres, a convenção de nomenclatura do
    # Sankhya. O SQL antigo agrupava pela sequência de letras inicial, que em
    # `TGFCAB` casa o nome inteiro — então TGF, TSI e TCS, os módulos que a tool
    # existe para mostrar, caíam no HAVING e sumiam da saída.
    modulos = group_prefixes(["TGFCAB", "TGFITE", "TGFPAR", "TSIUSU", "TSIEMP"])
    assert modulos == [
        {"prefixo": "TGF", "qtd_tabelas": 3},
        {"prefixo": "TSI", "qtd_tabelas": 2},
    ]


def test_duas_tabelas_do_mesmo_prefixo_agrupam():
    assert group_prefixes(["TGFCAB", "TGFITE"]) == [{"prefixo": "TGF", "qtd_tabelas": 2}]


def test_agrupamento_descarta_prefixo_com_uma_tabela_so():
    # Mesma regra do HAVING COUNT(*) > 1 do SQL original: prefixo único é ruído.
    assert group_prefixes(["TGFCAB", "XYZUNICA"]) == []
    assert group_prefixes([]) == []


def test_agrupamento_com_nome_menor_que_o_prefixo_nao_quebra():
    # Nome curto demais não tem prefixo de módulo: fica de fora sem estourar.
    assert group_prefixes(["AB", "X", "", None, "TGFCAB", "TGFITE"]) == [
        {"prefixo": "TGF", "qtd_tabelas": 2},
    ]


def test_agrupamento_ignora_nome_sem_prefixo_alfabetico():
    assert group_prefixes(["123456", "1234", "TGFCAB", "TGFITE"]) == [
        {"prefixo": "TGF", "qtd_tabelas": 2},
    ]


def test_agrupamento_reconhece_tabela_customizada():
    # Customizadas do Sankhya são AD_*: o prefixo de 3 pega o underscore junto.
    assert group_prefixes(["AD_PEDIDO", "AD_CLIENTE"]) == [
        {"prefixo": "AD_", "qtd_tabelas": 2},
    ]


def test_agrupamento_desempata_por_ordem_alfabetica():
    # Empate na contagem precisa de ordem estável, senão a saída muda a cada run.
    assert group_prefixes(["ZZZA", "ZZZB", "AAAA", "AAAB"]) == [
        {"prefixo": "AAA", "qtd_tabelas": 2},
        {"prefixo": "ZZZ", "qtd_tabelas": 2},
    ]


def test_agrupamento_normaliza_caixa_do_prefixo():
    # No SQL Server um nome minúsculo é plausível e ficaria num grupo separado.
    assert group_prefixes(["tgfcab", "TGFITE"]) == [{"prefixo": "TGF", "qtd_tabelas": 2}]


# --- Allowlist: SELECT ... INTO e literais --------------------------------


def test_bloqueia_select_into():
    # `SELECT ... INTO nova FROM x` começa com SELECT e passaria a checagem de
    # prefixo, mas no SQL Server cria tabela — escrita, e lá não existe READ
    # ONLY de sessão para recusar. Bloqueado nos dois bancos.
    for sql in [
        "SELECT * INTO nova FROM TGFCAB",
        "select nunota into #tmp from TGFCAB",
        "WITH x AS (SELECT 1 c FROM T) SELECT * INTO t FROM x",
        "SELECT * /* disfarce */ INTO nova FROM TGFCAB",
    ]:
        assert assert_read_only_query(sql) is not None, sql


def test_literal_de_texto_nao_reprova_query_valida():
    # `;` e `INTO` dentro de aspas são dado, não comando. O caso do `;` era um
    # falso positivo que já existia antes do bloqueio de INTO.
    for sql in [
        "SELECT 'entrada into saida' AS obs FROM DUAL",
        "SELECT 'a;b' FROM DUAL",
        "SELECT 'aspa '' dobrada into x' FROM DUAL",
    ]:
        assert assert_read_only_query(sql) is None, sql


def test_bloqueio_de_into_nao_pega_palavra_composta():
    # Só a palavra inteira: coluna chamada INTOLERANCIA ou POINTO não é INTO.
    assert assert_read_only_query("SELECT INTOLERANCIA FROM T") is None
    assert assert_read_only_query("SELECT PONTO_INTOCADO FROM T") is None


# --- Schema das tabelas (SANKHYA_DB_SCHEMA) -------------------------------


def _com_schema(valor):
    """Roda schema_prefix() com SANKHYA_DB_SCHEMA temporariamente redefinido."""
    with _como(schema=valor):
        return schema_prefix()


def test_schema_ausente_nao_qualifica_nada():
    # Sem a variável, nome de tabela sai cru: comportamento de sempre.
    assert resolve_schema(None) is None
    assert resolve_schema("") is None
    assert resolve_schema("   ") is None
    assert _com_schema(None) == ""


def test_schema_normalizado_para_maiusculas():
    assert resolve_schema(" sankhya ") == "SANKHYA"
    assert _com_schema("SANKHYA") == "SANKHYA."


def test_schema_invalido_falha_alto():
    # O nome é interpolado no texto da query e do ALTER SESSION — nenhum dos
    # dois aceita bind para identificador —, então precisa ser recusado antes.
    for ruim in ["SANKHYA; DROP TABLE X", "SANKHYA TESTE", "SANKHYA--", "DONO.TABELA", "1SCHEMA"]:
        try:
            resolve_schema(ruim)
        except ValueError:
            continue
        raise AssertionError(f"schema inválido aceito: {ruim!r}")


def test_schema_qualifica_amostra_e_tddins_nos_dois_bancos():
    # As quatro referências não qualificadas: a amostra e as três do TDDINS.
    with _como(schema="SANKHYA"):
        with _dialeto("oracle"):
            assert dialects.query("table_sample", tabela="TGFCAB") == (
                "SELECT * FROM SANKHYA.TGFCAB WHERE ROWNUM <= :1"
            )
            for nome in ["resolve_table", "instances_by_table", "search_entities"]:
                assert "FROM SANKHYA.TDDINS" in dialects.query(nome, filtro="")
        with _dialeto("sqlserver"):
            assert dialects.query("table_sample", tabela="TGFCAB") == (
                "SELECT TOP (%s) * FROM SANKHYA.TGFCAB"
            )
            assert "FROM SANKHYA.TDDINS" in dialects.query("resolve_table")


def test_alter_session_so_no_oracle():
    # No SQL Server o schema padrão vem do mapeamento do login: persistente e
    # com privilégio, não é papel deste servidor mexer. Lá vale o qualificador.
    cursor = _FakeCursor(0)
    with _como(schema="SANKHYA"):
        dialects._set_current_schema(_FakeConn(cursor), None)
    assert cursor.executado == ["ALTER SESSION SET CURRENT_SCHEMA = SANKHYA"]


# --- Nome que o dicionário não reconheceu ---------------------------------


def test_nome_resolvido_nao_gera_aviso():
    # Resolveu via TDDINS: o vazio é real, cada tool usa a própria mensagem.
    assert server.unresolved_name_note("CabecalhoNota", [{"nometab": "TGFCAB"}]) is None


def test_nome_nao_resolvido_aponta_search_entities():
    # "Nenhum índice encontrado" se lê como "a tabela não tem índice"; o que
    # houve foi um EntityName que o dicionário não reconhece.
    aviso = server.unresolved_name_note("CabeçalhoNota", [])
    assert "search_entities(\"CabeçalhoNota\")" in aviso
    assert "TDDINS" in aviso


def test_exemplos_das_docstrings_usam_entityname_sem_acento():
    # O EntityName de TGFCAB é `CabecalhoNota`, sem cedilha — `CabeçalhoNota`
    # não existe no dicionário. Docstring é o que o cliente MCP mostra ao
    # modelo: exemplo errado vira chamada errada.
    for tool in (server.describe_table, server.get_indexes, server.search_entities):
        doc = tool.__doc__ or ""
        assert "CabeçalhoNota" not in doc, tool.__name__
        assert "CabecalhoNota" in doc, tool.__name__


def test_tools_avisam_nome_nao_resolvido_sem_quebrar():
    # get_indexes chamava unresolved_name_note com `entity_rows`, mas descartava
    # o retorno em `resolved, _`: NameError sempre que a tabela não tinha índice.
    originais = (server.resolve_table_name, server.execute_query)
    server.resolve_table_name = lambda nome: (nome.upper(), [])
    server.execute_query = lambda *a, **kw: []
    try:
        for tool in (server.describe_table, server.get_indexes, server.get_foreign_keys):
            assert "search_entities" in tool("NaoExiste"), tool.__name__
    finally:
        server.resolve_table_name, server.execute_query = originais


# --- Dicionário Sankhya: descrição e domínio -------------------------------

_COL_TIPMOV = {
    "column_name": "TIPMOV", "data_type": "VARCHAR2", "data_length": 1,
    "data_precision": None, "data_scale": None, "nullable": "Y",
    "comments": None, "owner": "SANKHYA",
}
_COL_VLRNOTA = {
    "column_name": "VLRNOTA", "data_type": "NUMBER", "data_length": 22,
    "data_precision": 15, "data_scale": 2, "nullable": "Y",
    "comments": None, "owner": "SANKHYA",
}


def test_tipo_compacto_colapsa_quatro_colunas_em_uma():
    assert server.compact_type(_COL_VLRNOTA) == "NUMBER(15,2)"
    assert server.compact_type(_COL_TIPMOV) == "VARCHAR2(1)"
    # DATE tem DATA_LENGTH 7 no Oracle e NUMBER sem precisão tem 22: número que
    # não diz nada não entra na célula.
    assert server.compact_type({"data_type": "DATE", "data_length": 7}) == "DATE"
    assert server.compact_type({"data_type": "NUMBER", "data_length": 22}) == "NUMBER"
    # Formas do SQL Server.
    assert server.compact_type(
        {"data_type": "varchar", "data_length": -1}
    ) == "varchar(max)"
    assert server.compact_type(
        {"data_type": "int", "data_precision": 10, "data_scale": 0}
    ) == "int"
    assert server.compact_type({"data_type": ""}) == ""


def test_nulo_normalizado_entre_os_dois_bancos():
    # Oracle devolve Y/N e o SQL Server YES/NO: sem normalizar, a mesma tabela
    # se descreve diferente conforme o banco.
    assert server.is_nullable("Y") == "S"
    assert server.is_nullable("YES") == "S"
    assert server.is_nullable("N") == "N"
    assert server.is_nullable("NO") == "N"
    assert server.is_nullable(None) == "N"


def test_merge_traz_as_opcoes_inline_e_nao_a_contagem():
    # O ponto da tool: quem lê precisa ver `P=Pedido de venda` junto de `V=Venda`
    # antes de escrever o WHERE. Só a contagem ("23 opções") deixa o consumidor
    # adivinhar o literal, e os dois valores são válidos — erro sem erro de SQL.
    linhas = server.merge_field_dict(
        [_COL_TIPMOV, _COL_VLRNOTA],
        [{"nomecampo": "TIPMOV", "descrcampo": "Tipo de Movimento"}],
        [
            {"nomecampo": "TIPMOV", "valor": "P", "opcao": "Pedido de venda"},
            {"nomecampo": "TIPMOV", "valor": "V", "opcao": "Venda"},
        ],
    )
    tipmov, vlrnota = linhas
    assert tipmov["opcoes"] == "P=Pedido de venda; V=Venda"
    assert tipmov["descricao"] == "Tipo de Movimento"
    assert tipmov["tipo"] == "VARCHAR2(1)"
    # Campo sem domínio não inventa opção.
    assert vlrnota["opcoes"] == ""
    assert list(tipmov) == ["campo", "tipo", "nulo", "descricao", "opcoes"]


def test_merge_prefere_dicionario_ao_comentario_do_catalogo():
    col = dict(_COL_TIPMOV, comments="comentário do catálogo")
    com_dic = server.merge_field_dict(
        [col], [{"nomecampo": "TIPMOV", "descrcampo": "Tipo de Movimento"}], []
    )
    assert com_dic[0]["descricao"] == "Tipo de Movimento"
    # Sem dicionário, o comentário do catálogo ainda serve: há bases fora da
    # Sankhya em que ele vem preenchido.
    sem_dic = server.merge_field_dict([col], [], [])
    assert sem_dic[0]["descricao"] == "comentário do catálogo"


def test_merge_sem_dicionario_nenhum_nao_quebra():
    linhas = server.merge_field_dict([_COL_TIPMOV], [], [])
    assert linhas[0]["descricao"] == ""
    assert linhas[0]["opcoes"] == ""
    assert linhas[0]["campo"] == "TIPMOV"


def test_dicionario_ausente_degrada_mas_erro_real_sobe():
    original = server.execute_query

    def nega(*_a, **_kw):
        raise Exception("ORA-00942: table or view does not exist"
                        if active_client().db_type == "oracle" else "Invalid object name 'TDDCAM'")

    server.execute_query = nega
    try:
        assert server.dictionary_rows("field_dict", "TGFCAB") == []
    finally:
        server.execute_query = original

    # Falha de permissão ou de rede não pode virar resposta incompleta silenciosa.
    def quebra(*_a, **_kw):
        raise Exception("ORA-01017: invalid username/password")

    server.execute_query = quebra
    try:
        erro = None
        try:
            server.dictionary_rows("field_dict", "TGFCAB")
        except Exception as exc:
            erro = exc
        assert erro is not None, "erro que não é 'objeto ausente' precisa subir"
    finally:
        server.execute_query = original


def test_queries_do_dicionario_valem_nos_dois_dialetos():
    # TDDCAM/TDDOPC/TDDLIG são tabelas da aplicação, idênticas em Oracle e SQL
    # Server: a mesma query serve aos dois, só o placeholder muda.
    for dialeto in ("oracle", "sqlserver"):
        for nome in ("field_dict", "field_options", "links"):
            assert nome in QUERIES[dialeto], f"{nome} ausente em {dialeto}"


def test_opcoes_tem_a_mesma_ordem_nos_dois_bancos():
    # ORDEM é nula na maioria das linhas de TDDOPC e os bancos discordam sobre
    # onde o nulo entra na ordenação: Oracle joga para o fim, SQL Server para o
    # começo. Sem o CASE, a mesma tabela sai em ordem diferente conforme o banco.
    for dialeto in ("oracle", "sqlserver"):
        sql = QUERIES[dialeto]["field_options"]
        assert "CASE WHEN o.ORDEM IS NULL" in sql, dialeto


def test_dicionario_qualifica_schema_e_bind_nos_dois_bancos():
    # Sem o prefixo de schema o banco resolve TDDCAM no schema do usuário
    # conectado, que num login somente-leitura não é o dono das tabelas.
    with _como(schema="SANKHYA"):
        for dialeto, bind in (("oracle", ":1"), ("sqlserver", "%s")):
            with _dialeto(dialeto):
                for nome in ("field_dict", "field_options", "links"):
                    sql = dialects.query(nome)
                    assert "{" not in sql, f"{nome}/{dialeto}: sobrou placeholder"
                    assert "SANKHYA.TDD" in sql, f"{nome}/{dialeto}"
                    # Um bind por placeholder: o pymssql não reaproveita `%s`.
                    assert sql.count(bind) == 1, f"{nome}/{dialeto}"


# --- Versão e aviso de atualização -----------------------------------------


def test_versao_bate_com_o_topo_do_changelog():
    # É o mesmo par que `tools/release.sh` valida antes de criar a tag: versão
    # dessincronizada faz o `check_updates` anunciar mudança ilegível.
    changelog = (Path(__file__).parent / "CHANGELOG.md").read_text(encoding="utf-8")
    topo = re.search(r"^##\s+\[?v?(\d+(?:\.\d+)*)\]?", changelog, re.MULTILINE)
    assert topo, "CHANGELOG.md sem nenhuma seção de versão"
    assert topo.group(1) == __version__


def test_versao_normaliza_para_tres_posicoes():
    # As tags antigas deste repositório têm duas posições (v1.1): sem completar
    # com zero, `1.1` e `1.1.0` comparariam diferente.
    assert parse_version("v1.1") == parse_version("1.1.0") == (1, 1, 0)
    assert parse_version("1.2.3") == (1, 2, 3)
    assert parse_version("1.2.0") > parse_version("1.1.9")
    assert parse_version("beta") is None
    assert parse_version("") is None


def test_maior_tag_ignora_ref_que_nao_e_versao():
    refs = [
        "aaa\trefs/tags/v1.0",
        "bbb\trefs/tags/v1.2.0",
        "ccc\trefs/tags/v1.2.0^{}",   # tag anotada: o git lista o objeto também
        "ddd\trefs/tags/beta",
        "eee\trefs/heads/main",
    ]
    assert pick_latest(refs) == (1, 2, 0)
    assert pick_latest([]) is None
    assert pick_latest(["aaa\trefs/tags/nightly"]) is None


def test_changelog_recorta_apenas_o_intervalo_pedido():
    with tempfile.TemporaryDirectory() as tmp:
        arquivo = Path(tmp) / "CHANGELOG.md"
        arquivo.write_text(
            "# Changelog\n\n"
            "## [1.3.0] - 2026-01-03\n- terceira\n\n"
            "## [1.2.0] - 2026-01-02\n- segunda\n\n"
            "## [1.1.0] - 2026-01-01\n- primeira\n",
            encoding="utf-8",
        )
        original = updates._CHANGELOG
        updates._CHANGELOG = arquivo
        try:
            trecho = updates.changelog_entries((1, 1, 0), (1, 3, 0))
            assert "terceira" in trecho and "segunda" in trecho
            assert "primeira" not in trecho, "o limite inferior é exclusivo"
            assert updates.changelog_entries((1, 3, 0), (1, 3, 0)) == ""
            # CHANGELOG ausente não pode derrubar o aviso.
            updates._CHANGELOG = Path(tmp) / "nao-existe.md"
            assert updates.changelog_entries((1, 0, 0), (9, 9, 9)) == ""
        finally:
            updates._CHANGELOG = original


_OPCOES_TGFCAB = [
    {"nomecampo": "STATUSNOTA", "valor": "L", "opcao": "Liberada"},
    {"nomecampo": "STATUSNOTA", "valor": "P", "opcao": "Pendente"},
    {"nomecampo": "TIPMOV", "valor": "V", "opcao": "Venda"},
    {"nomecampo": "ORIGEM", "valor": "1", "opcao": "Interna"},
]


def test_amostra_anexa_o_rotulo_ao_codigo():
    # Amostra de tabela Sankhya é uma parede de códigos de uma letra; sem o
    # rótulo não se lê sem consultar o domínio campo a campo.
    linhas = server.decorate_options(
        [{"nunota": 12, "statusnota": "L", "tipmov": "V"}], _OPCOES_TGFCAB
    )
    assert linhas[0]["statusnota"] == "L (Liberada)"
    assert linhas[0]["tipmov"] == "V (Venda)"
    # Coluna sem domínio não é tocada.
    assert linhas[0]["nunota"] == 12


def test_amostra_deixa_cru_o_valor_fora_do_dominio():
    # É assim que o valor não declarado se denuncia. Medido na base: TGFCAB
    # tem TIPMOV='Z' em 23 de 139 linhas, e `Z` não está em TDDOPC.
    linhas = server.decorate_options([{"tipmov": "Z"}], _OPCOES_TGFCAB)
    assert linhas[0]["tipmov"] == "Z", "valor fora do domínio não pode ganhar rótulo"


def test_amostra_preserva_valores_falsy_e_nulos():
    # Mesma regra do rows_to_markdown: 0, False e Decimal são valores reais e
    # não podem virar texto por causa de uma decoração.
    linhas = server.decorate_options(
        [{"vlrnota": Decimal("0.00"), "ativo": False, "obs": None, "statusnota": "L"}],
        _OPCOES_TGFCAB,
    )
    assert linhas[0]["vlrnota"] == Decimal("0.00")
    assert linhas[0]["ativo"] is False
    assert linhas[0]["obs"] is None
    assert linhas[0]["statusnota"] == "L (Liberada)"


def test_amostra_casa_coluna_minuscula_com_dicionario_maiusculo():
    # As chaves chegam minúsculas de `fetch_rows`, o dicionário guarda em
    # maiúsculas, e o valor pode ser numérico enquanto TDDOPC.VALOR é texto.
    linhas = server.decorate_options([{"origem": 1}], _OPCOES_TGFCAB)
    assert linhas[0]["origem"] == "1 (Interna)"


def test_amostra_sem_dicionario_devolve_as_linhas_intactas():
    linhas = [{"nunota": 1, "statusnota": "L"}]
    assert server.decorate_options(linhas, []) == linhas
    assert server.decorate_options([], _OPCOES_TGFCAB) == []


def test_rodape_nao_promete_dominio_fechado():
    # O dicionário declara o que a aplicação oferece, não esgota o que está
    # gravado. Prometer "domínio fechado" leva a `IN (...)` que perde linha em
    # silêncio — o oposto do que as opções vieram resolver.
    doc = server.describe_table.__doc__ or ""
    instrucoes = server.mcp.instructions
    assert "domínio fechado" not in instrucoes
    assert "NÃO garante esgotar" in instrucoes
    assert "table_sample" in instrucoes
    assert doc


def test_busca_de_tabela_troca_comentario_vazio_por_verbete():
    # `search_tables` é a primeira tool do fluxo e devolvia uma coluna
    # `comments` vazia — nenhuma tabela da base Sankhya tem comentário de
    # catálogo. O verbete é o que diz se a candidata é a certa.
    rows = [
        {"table_name": "TGFCAB", "num_rows": 95, "comments": None},
        {"table_name": "TGFCAB_BCK", "num_rows": 0, "comments": None},
    ]
    saida = server.merge_table_descriptions(
        rows, [{"nometab": "TGFCAB", "descrtab": "Entrada e Saída de Produto"}]
    )
    assert saida[0] == {
        "tabela": "TGFCAB",
        "linhas": 95,
        "descricao": "Entrada e Saída de Produto",
    }
    # Tabela fora do dicionário (backup, temporária) não some, fica sem verbete.
    assert saida[1]["tabela"] == "TGFCAB_BCK"
    assert saida[1]["descricao"] == ""


def test_busca_de_tabela_usa_comentario_do_catalogo_como_reserva():
    saida = server.merge_table_descriptions(
        [{"table_name": "OUTRA", "num_rows": 1, "comments": "verbete do catálogo"}], []
    )
    assert saida[0]["descricao"] == "verbete do catálogo"


def test_busca_de_campo_casa_por_tabela_e_campo():
    # A chave é o par: o mesmo nome de campo tem descrição diferente conforme a
    # tabela, do mesmo jeito que tem domínio diferente.
    rows = [
        {"table_name": "TGFCAB", "column_name": "CODPARC",
         "data_type": "NUMBER", "nullable": "N", "comments": None},
        {"table_name": "TGFACO", "column_name": "CODPARC",
         "data_type": "NUMBER", "nullable": "N", "comments": None},
    ]
    descricoes = [
        {"nometab": "TGFCAB", "nomecampo": "CODPARC", "descrcampo": "Parceiro"},
        {"nometab": "TGFACO", "nomecampo": "CODPARC", "descrcampo": "Cód. Parceiro"},
    ]
    saida = server.merge_column_descriptions(rows, descricoes)
    assert saida[0]["descricao"] == "Parceiro"
    assert saida[1]["descricao"] == "Cód. Parceiro"
    # Nulo normalizado aqui também: a mesma coluna não pode se descrever de um
    # jeito no describe_table e de outro no search_columns.
    assert saida[0]["nulo"] == "N"
    assert list(saida[0]) == ["tabela", "campo", "tipo", "nulo", "descricao"]


def test_busca_sem_dicionario_nao_perde_linha():
    # Dicionário inacessível degrada para a saída de antes, sem descrição, mas
    # nunca some com resultado do catálogo.
    rows = [{"table_name": "T", "column_name": "C",
             "data_type": "NUMBER", "nullable": "YES", "comments": None}]
    saida = server.merge_column_descriptions(rows, [])
    assert len(saida) == 1
    assert saida[0]["descricao"] == ""
    assert saida[0]["nulo"] == "S"
    assert server.merge_table_descriptions([], []) == []


def test_filtro_opcional_da_busca_de_campo_vale_nos_dois_bancos():
    # O dicionário não tem alias de tabela: o filtro usa a coluna crua, e o
    # placeholder precisa ser resolvido igual ao da consulta de catálogo.
    for dialeto, bind in (("oracle", ":2"), ("sqlserver", "%s")):
        with _dialeto(dialeto):
            com = dialects.query("column_descriptions", filtro="AND NOMETAB LIKE {p2}")
            assert "{" not in com, dialeto
            assert bind in com, dialeto
            sem = dialects.query("column_descriptions", filtro="")
            assert "{" not in sem, dialeto
            assert "NOMETAB LIKE" not in sem, dialeto


def test_ci_roda_os_testes_em_push_e_pr():
    ci = (Path(__file__).parent / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "pull_request:" in ci
    assert "python test_server.py" in ci


def test_secao_de_uma_versao_sai_sem_o_cabecalho():
    # É o corpo das notas do release no GitHub. O título do release já é a
    # tag, então repetir "## [1.2.0] - data" no corpo seria redundante.
    with tempfile.TemporaryDirectory() as tmp:
        arquivo = Path(tmp) / "CHANGELOG.md"
        arquivo.write_text(
            "# Changelog\n\n"
            "## [1.3.0] - 2026-01-03\n\n### Adicionado\n- coisa nova\n\n"
            "## [1.2.0] - 2026-01-02\n\n### Corrigido\n- bug antigo\n",
            encoding="utf-8",
        )
        original = updates._CHANGELOG
        updates._CHANGELOG = arquivo
        try:
            notas = updates.changelog_section((1, 3, 0))
            assert notas.startswith("### Adicionado")
            assert "coisa nova" in notas
            assert "## [1.3.0]" not in notas
            assert "bug antigo" not in notas, "não pode vazar a seção seguinte"
            # Versão sem seção é o que faz o validador recusar a publicação.
            assert updates.changelog_section((9, 9, 9)) == ""
        finally:
            updates._CHANGELOG = original


def test_secao_so_com_cabecalho_conta_como_vazia():
    # Cabeçalho sem corpo passaria no check de "topo bate com a versão" e
    # publicaria um release sem explicação nenhuma.
    with tempfile.TemporaryDirectory() as tmp:
        arquivo = Path(tmp) / "CHANGELOG.md"
        arquivo.write_text("# Changelog\n\n## [1.4.0] - 2026-01-04\n\n", encoding="utf-8")
        original = updates._CHANGELOG
        updates._CHANGELOG = arquivo
        try:
            assert updates.changelog_section((1, 4, 0)) == ""
        finally:
            updates._CHANGELOG = original


def test_release_notes_da_versao_atual_tem_conteudo():
    # Mesmo guard que `tools/release.sh` aplica antes de criar a tag e que o
    # workflow aplica antes de publicar: a versão em src/version.py precisa
    # ter notas escritas.
    assert updates.changelog_section(parse_version(__version__)), (
        f"CHANGELOG.md não descreve o que mudou na versão {__version__}"
    )


def test_workflow_de_release_dispara_em_tag_de_versao():
    # A garantia de que TODA release ganha nota está no workflow, não no
    # script local: tag empurrada à mão também precisa publicar as notas.
    workflow = (Path(__file__).parent / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    assert 'tags:' in workflow and '"v*"' in workflow
    assert "release_notes.py" in workflow
    assert "gh release create" in workflow
    # Tag reposicionada dispara o workflow de novo, e `create` falha quando o
    # release já existe. Sem o ramo de edição, o run fica vermelho à toa.
    assert "gh release edit" in workflow


def test_aviso_so_aparece_quando_ha_versao_maior_publicada():
    original = updates.latest_version
    try:
        local = parse_version(__version__)
        maior = (local[0], local[1], local[2] + 1)

        updates.latest_version = lambda use_cache=True: maior
        assert updates.update_notice() is not None

        updates.latest_version = lambda use_cache=True: local
        assert updates.update_notice() is None

        # Offline, git ausente ou remote inacessível: sem aviso, sem exceção.
        updates.latest_version = lambda use_cache=True: None
        assert updates.update_notice() is None

        def explode(use_cache=True):
            raise OSError("sem rede")

        updates.latest_version = explode
        assert updates.update_notice() is None, "falha de rede não pode derrubar o boot"
    finally:
        updates.latest_version = original


def test_instrucoes_exigem_consultar_dominio_antes_do_literal():
    # A regra é o que impede o modelo de deduzir `TIPMOV = 'V'` para "venda"
    # quando pedido de venda é 'P'. Se ela sair das instruções, o servidor volta
    # a depender de sorte.
    instrucoes = server.mcp.instructions
    assert "LITERAIS EM WHERE" in instrucoes
    assert "describe_table" in instrucoes
    assert "opcoes" in instrucoes


# --- Modo DBE: DB Explorer do Sankhya --------------------------------------


def test_modo_e_obrigatorio():
    # Sem padrão: um valor assumido abriria conexão com o banco justamente onde
    # só o DB Explorer é alcançável, e o erro viria como "conexão recusada".
    for ausente in (None, "", "   "):
        try:
            resolve_mode(ausente)
        except ValueError as exc:
            assert "obrigatório" in str(exc) and "SANKHYA_MODE" in str(exc)
        else:
            raise AssertionError(f"modo {ausente!r} deveria falhar")
    assert resolve_mode("direct") == "direct"
    assert resolve_mode(" DBE ") == "dbe"
    try:
        resolve_mode("dbx")
    except ValueError as exc:
        assert "SANKHYA_MODE" in str(exc) and "inválido" in str(exc)
    else:
        raise AssertionError("modo desconhecido deveria falhar alto")


def test_boot_sem_modo_falha_mesmo_com_cadastro():
    caminho = _arquivo(_JSON_DOIS_DIRECT)
    try:
        clients.load_clients({"SANKHYA_CLIENTS_FILE": str(caminho)})
    except ValueError as exc:
        assert "SANKHYA_MODE" in str(exc)
    else:
        raise AssertionError("sem SANKHYA_MODE o boot deveria falhar")


def test_cliente_do_ambiente_so_exige_dbe_no_modo_dbe():
    # Modo direto não precisa saber que o DB Explorer existe.
    direto = clients.client_from_env({"SANKHYA_MODE": "direct"})
    assert direto.mode == "direct" and direto.name == "default" and direto.default
    assert direto.db_type == "oracle" and direto.port == 1521 and direto.user == "SANKHYA"
    try:
        clients.client_from_env({"SANKHYA_MODE": "dbe"})
    except ValueError as exc:
        for nome in ("SANKHYA_DBE_URL", "SANKHYA_DBE_USER", "SANKHYA_DBE_PASSWORD"):
            assert nome in str(exc)
    else:
        raise AssertionError("modo dbe sem variáveis deveria falhar alto")
    dbe_ok = clients.client_from_env({
        "SANKHYA_MODE": "dbe",
        "SANKHYA_DBE_URL": "http://h:8380/mge/",
        "SANKHYA_DBE_USER": " vidya ",
        "SANKHYA_DBE_PASSWORD": "s",
    })
    assert (dbe_ok.dbe_url, dbe_ok.dbe_user, dbe_ok.dbe_password) == ("http://h:8380", "vidya", "s")


def test_cliente_do_ambiente_segue_o_banco_escolhido():
    direto = {"SANKHYA_MODE": "direct"}
    mssql = clients.client_from_env({**direto, "SANKHYA_DB_TYPE": "sqlserver", "SANKHYA_DB_SCHEMA": "sankhya"})
    assert mssql.db_type == "sqlserver" and mssql.port == 1433 and mssql.schema == "SANKHYA"
    assert clients.client_from_env({**direto, "SANKHYA_DB_PORT": "1522"}).port == 1522
    try:
        clients.client_from_env({**direto, "SANKHYA_DB_PORT": "abc"})
    except ValueError as exc:
        assert "SANKHYA_DB_PORT" in str(exc)
    else:
        raise AssertionError("porta inválida deveria falhar alto")


def test_tipos_de_banco_do_cliente_batem_com_as_queries():
    # `clients.py` não importa `dialects.py`: a lista de bancos é duplicada, e
    # este teste é o que impede as duas de divergirem.
    assert set(clients.DB_TYPES) == set(QUERIES)


def test_url_dbe_reduz_ao_endereco_base():
    for entrada in [
        "http://h:8380",
        "http://h:8380/",
        "http://h:8380/mge",
        "http://h:8380/mge/system.jsp?x=1",
        "h:8380",
    ]:
        assert dbe.normalize_url(entrada) == "http://h:8380", entrada
    assert dbe.normalize_url("https://h/mge/") == "https://h"
    # Credencial na URL iria parar em log e em `list_clients`.
    assert dbe.normalize_url("http://u:senha@h:8380/mge") == "http://h:8380"
    assert dbe.normalize_url("") == ""
    assert dbe.normalize_url(None) == ""


def test_literal_sql_escapa_aspas():
    # Sem bind, o escape é a única defesa contra injeção.
    assert dbe.sql_literal("O'Brien") == "'O''Brien'"
    assert dbe.sql_literal("x' OR '1'='1") == "'x'' OR ''1''=''1'"
    assert dbe.sql_literal(None) == "NULL"
    assert dbe.sql_literal(10) == "10"
    assert dbe.sql_literal(Decimal("1.50")) == "1.50"


def test_literal_sql_recusa_tipo_que_nao_sabe_escrever():
    for valor in (True, b"x", object(), "a\x00b"):
        try:
            dbe.sql_literal(valor)
        except (TypeError, ValueError):
            continue
        raise AssertionError(f"deveria recusar: {valor!r}")


def test_inline_params_oracle_posicional_e_reaproveitavel():
    sql = "SELECT 1 FROM T WHERE A = :1 AND B = :2 AND C = :1"
    assert dbe.inline_params(sql, ["x", 7], "oracle") == (
        "SELECT 1 FROM T WHERE A = 'x' AND B = 7 AND C = 'x'"
    )


def test_inline_params_sqlserver_na_ordem():
    sql = "SELECT 1 FROM T WHERE A = %s AND B = %s"
    assert dbe.inline_params(sql, ["x", 7], "sqlserver") == (
        "SELECT 1 FROM T WHERE A = 'x' AND B = 7"
    )


def test_inline_params_nao_mexe_em_literal_de_texto():
    # `:1` e `%s` dentro de aspas são dado, não placeholder.
    assert dbe.inline_params("SELECT ':1' FROM T WHERE A = :1", ["x"], "oracle") == (
        "SELECT ':1' FROM T WHERE A = 'x'"
    )
    assert dbe.inline_params("SELECT '%s' FROM T WHERE A = %s", ["x"], "sqlserver") == (
        "SELECT '%s' FROM T WHERE A = 'x'"
    )


def test_inline_params_sem_parametros_devolve_o_sql_intacto():
    # O `%s` de um LIKE escrito à mão em run_query não é placeholder.
    sql = "SELECT * FROM T WHERE N LIKE '%s%'"
    assert dbe.inline_params(sql, None, "sqlserver") == sql
    assert dbe.inline_params(sql, [], "sqlserver") == sql


def test_inline_params_com_parametro_faltando_falha():
    for sql, tipo in [("SELECT :1, :2 FROM T", "oracle"), ("SELECT %s, %s FROM T", "sqlserver")]:
        try:
            dbe.inline_params(sql, ["x"], tipo)
        except ValueError:
            continue
        raise AssertionError("placeholder sem parâmetro deveria falhar")


def test_valor_injetado_fica_dentro_do_literal():
    sql = dbe.inline_params("SELECT 1 FROM T WHERE A = :1", ["x'; DELETE FROM T; --"], "oracle")
    assert sql == "SELECT 1 FROM T WHERE A = 'x''; DELETE FROM T; --'"


def test_mensagem_em_base64_e_decodificada():
    import base64

    codificada = base64.b64encode("Usuário/Senha inválido.".encode()).decode()
    assert dbe.decode_message(codificada) == "Usuário/Senha inválido."
    # Texto comum, com espaço e pontuação, nunca é base64.
    assert dbe.decode_message("Não autorizado.") == "Não autorizado."
    assert dbe.decode_message("ORA-00936: expressão não encontrada") == (
        "ORA-00936: expressão não encontrada"
    )
    assert dbe.decode_message(None) == ""


class _SessaoFalsa:
    """Responde no lugar do DbeSession: devolve (colunas, linhas, tipos, burst)."""

    def __init__(self, colunas, linhas, tipos=None, burst=False):
        self.resposta = (colunas, linhas, tipos or ["S"] * len(colunas), burst)
        self.recebido = []

    def execute(self, sql):
        self.recebido.append(sql)
        return self.resposta


def test_dbe_devolve_chaves_minusculas_como_o_modo_direto():
    sessao = _SessaoFalsa(["NUNOTA", "TIPMOV"], [[1, "P"], [2, "V"]])
    rows, truncated = dbe.run(sessao, "SELECT 1", limit=10)
    assert rows == [{"nunota": 1, "tipmov": "P"}, {"nunota": 2, "tipmov": "V"}]
    assert truncated is False


def test_dbe_corta_no_limite_e_sinaliza():
    sessao = _SessaoFalsa(["N"], [[i] for i in range(10)])
    rows, truncated = dbe.run(sessao, "SELECT 1", limit=3)
    assert [r["n"] for r in rows] == [0, 1, 2]
    assert truncated is True
    rows, truncated = dbe.run(sessao, "SELECT 1", limit=10)
    assert len(rows) == 10 and truncated is False


def test_dbe_teto_do_db_explorer_sinaliza_truncamento_mesmo_sem_limite():
    # burstLimit: a aplicação cortou em 5000. Com limit=None o chamador pediu
    # tudo, então precisa saber que não veio tudo.
    sessao = _SessaoFalsa(["N"], [[1], [2]], burst=True)
    rows, truncated = dbe.run(sessao, "SELECT 1", limit=None)
    assert len(rows) == 2 and truncated is True
    # Com limite e o resultado cabendo nele, o corte do DB Explorer ainda vale.
    assert dbe.run(sessao, "SELECT 1", limit=10)[1] is True


def test_dbe_converte_data_so_em_coluna_de_data():
    sessao = _SessaoFalsa(
        ["DTNEG", "CEP", "OBS"],
        [["14102026 23:13:51", "01001000", "14102026"]],
        tipos=["H", "S", "S"],
    )
    linha = dbe.run(sessao, "SELECT 1", limit=1)[0][0]
    assert linha["dtneg"] == datetime(2026, 10, 14, 23, 13, 51)
    # Oito dígitos num VARCHAR (CEP) não pode virar data.
    assert linha["cep"] == "01001000"
    assert linha["obs"] == "14102026"


def test_dbe_data_so_com_dia_e_data_invalida():
    sessao = _SessaoFalsa(["D1", "D2"], [["01022003", "99999999"]], tipos=["D", "D"])
    linha = dbe.run(sessao, "SELECT 1", limit=1)[0][0]
    assert linha["d1"] == datetime(2003, 2, 1)
    assert linha["d2"] == "99999999"


class _HttpFalso:
    """Troca `DbeSession._post` por respostas roteirizadas, por serviço."""

    def __init__(self, respostas):
        self.respostas = {k: list(v) for k, v in respostas.items()}
        self.chamadas = []

    def __call__(self, servico, corpo, jsessionid=None):
        self.chamadas.append((servico, corpo, jsessionid))
        return self.respostas[servico].pop(0)


def _login_ok(sessao):
    return {"status": "1", "responseBody": {"jsessionid": {"$": sessao}}}


def _consulta_ok(valor):
    return {
        "status": "1",
        "responseBody": {
            "fieldsMetadata": [{"name": "X", "userType": "S"}],
            "rows": [[valor]],
            "burstLimit": False,
        },
    }


def _sessao_dbe(http):
    sessao = dbe.DbeSession("http://h:8380", "vidya", "segredo")
    sessao._post = http
    return sessao


def test_dbe_login_guarda_jsessionid_e_reaproveita():
    http = _HttpFalso({
        "MobileLoginSP.login": [_login_ok("S1")],
        "DbExplorerSP.executeQuery": [_consulta_ok("a"), _consulta_ok("b")],
    })
    sessao = _sessao_dbe(http)
    assert sessao.execute("SELECT 1 FROM DUAL")[1] == [["a"]]
    assert sessao.execute("SELECT 2 FROM DUAL")[1] == [["b"]]
    servicos = [c[0] for c in http.chamadas]
    # Um login só, mesmo com duas consultas.
    assert servicos.count("MobileLoginSP.login") == 1
    login = http.chamadas[0][1]
    assert login["NOMUSU"] == {"$": "vidya"} and login["INTERNO"] == {"$": "segredo"}
    assert all(c[2] == "S1" for c in http.chamadas[1:])


def test_dbe_refaz_login_quando_a_sessao_expira():
    http = _HttpFalso({
        "MobileLoginSP.login": [_login_ok("S2")],
        "DbExplorerSP.executeQuery": [
            {"status": "3", "statusMessage": "Não autorizado."},
            _consulta_ok("depois"),
        ],
    })
    sessao = _sessao_dbe(http)
    sessao._jsessionid = "S1"
    assert sessao.execute("SELECT 1 FROM DUAL")[1] == [["depois"]]
    assert sessao._jsessionid == "S2"
    assert http.chamadas[-1][2] == "S2"


def test_dbe_nao_entra_em_laco_de_login():
    # Recusa de novo após o relogin: sobe o erro, não tenta para sempre.
    http = _HttpFalso({
        "MobileLoginSP.login": [_login_ok("S1"), _login_ok("S2")],
        "DbExplorerSP.executeQuery": [
            {"status": "3", "statusMessage": "Não autorizado."},
            {"status": "3", "statusMessage": "Não autorizado."},
        ],
    })
    sessao = _sessao_dbe(http)
    try:
        sessao.execute("SELECT 1 FROM DUAL")
    except dbe.DbeError as exc:
        assert "Não autorizado" in str(exc)
    else:
        raise AssertionError("deveria subir o erro")


def test_dbe_login_recusado_nao_vaza_a_senha():
    http = _HttpFalso({
        "MobileLoginSP.login": [{"status": "3", "statusMessage": "Usuário/Senha inválido."}],
    })
    sessao = _sessao_dbe(http)
    try:
        sessao.execute("SELECT 1 FROM DUAL")
    except dbe.DbeError as exc:
        assert "Usuário/Senha inválido" in str(exc)
        assert "segredo" not in str(exc)
    else:
        raise AssertionError("login recusado deveria subir")


def test_dbe_erro_sql_sobe_com_a_mensagem_do_banco():
    http = _HttpFalso({
        "MobileLoginSP.login": [_login_ok("S1")],
        "DbExplorerSP.executeQuery": [
            {"status": "0", "statusMessage": "ORA-00942: a tabela ou view não existe"},
        ],
    })
    try:
        _sessao_dbe(http).execute("SELECT 1 FROM NADA")
    except dbe.DbeError as exc:
        # O `is_missing_object` do modo direto procura exatamente este código.
        assert server.is_missing_object(exc) or active_client().db_type != "oracle"
        assert "ORA-00942" in str(exc)
    else:
        raise AssertionError("erro SQL deveria subir")


def test_dbe_remove_ponto_e_virgula_final():
    http = _HttpFalso({
        "MobileLoginSP.login": [_login_ok("S1")],
        "DbExplorerSP.executeQuery": [_consulta_ok("a")],
    })
    _sessao_dbe(http).execute("  SELECT 1 FROM DUAL ;  ")
    assert http.chamadas[-1][1] == {"sql": "SELECT 1 FROM DUAL"}


def test_dbe_logout_encerra_e_esquece_a_sessao():
    http = _HttpFalso({
        "MobileLoginSP.login": [_login_ok("S1")],
        "MobileLoginSP.logout": [{"status": "1"}],
    })
    sessao = _sessao_dbe(http)
    sessao.login()
    sessao.logout()
    assert sessao._jsessionid is None
    assert http.chamadas[-1][0] == "MobileLoginSP.logout" and http.chamadas[-1][2] == "S1"
    sessao.logout()  # sem sessão: não chama nada
    assert len(http.chamadas) == 2


def test_dbe_get_session_exige_configuracao():
    try:
        dbe.get_session("", "", "")
    except dbe.DbeError as exc:
        assert "SANKHYA_DBE_URL" in str(exc)
    else:
        raise AssertionError("sem configuração deveria falhar")


def test_dbe_uma_sessao_por_credencial():
    # Cada cliente tem o seu login; a mesma credencial reaproveita o mesmo.
    antes = dict(dbe._sessions)
    try:
        a = dbe.get_session("http://a:8380", "vidya", "x")
        assert dbe.get_session("http://a:8380", "vidya", "x") is a
        assert dbe.get_session("http://b:8380", "vidya", "x") is not a
        assert dbe.get_session("http://a:8380", "outro", "x") is not a
        assert dbe.get_session("http://a:8380", "vidya", "y") is not a
    finally:
        dbe._sessions.clear()
        dbe._sessions.update(antes)


@contextmanager
def _modo_dbe(sessao, db_type="oracle"):
    """
    Faz do cliente único do processo um cliente DBE, com `sessao` no lugar da
    sessão real. É cadastro, e não só cliente ativo: toda tool resolve o cliente
    pelo cadastro ao entrar.
    """
    cliente = dataclasses.replace(
        active_client(), name="dbe", mode="dbe", db_type=db_type,
        dbe_url="http://h:8380", dbe_user="u", dbe_password="p",
    )
    guardado = server.get_session
    server.get_session = lambda *a: sessao
    try:
        with _cadastro({"dbe": cliente}):
            yield
    finally:
        server.get_session = guardado


def test_fetch_rows_dbe_nao_abre_conexao_e_escreve_os_parametros():
    sessao = _SessaoFalsa(["TABLE_NAME"], [["TGFCAB"]])
    original = server.connect

    def sem_conexao():
        raise AssertionError("o modo DBE não pode abrir conexão com o banco")

    server.connect = sem_conexao
    try:
        with _modo_dbe(sessao):
            rows, truncated = server.fetch_rows("SELECT 1 FROM T WHERE A = :1", ["TGF'X"])
    finally:
        server.connect = original
    assert rows == [{"table_name": "TGFCAB"}] and truncated is False
    assert sessao.recebido == ["SELECT 1 FROM T WHERE A = 'TGF''X'"]


def test_fetch_rows_dbe_respeita_o_dialeto_do_placeholder():
    sessao = _SessaoFalsa(["X"], [[1]])
    with _modo_dbe(sessao, db_type="sqlserver"):
        server.fetch_rows("SELECT 1 FROM T WHERE A = %s", ["v"])
    assert sessao.recebido == ["SELECT 1 FROM T WHERE A = 'v'"]


def test_execute_query_dbe_recusa_resultado_cortado_sem_limite():
    sessao = _SessaoFalsa(["N"], [[1], [2]], burst=True)
    with _modo_dbe(sessao):
        try:
            server.execute_query("SELECT 1", limit=None)
        except dbe.RowCapError as exc:
            assert len(exc.rows) == 2
            assert "5000" in str(exc)
        else:
            raise AssertionError("resultado cortado não pode passar por completo")
        # Com limite explícito o corte vira aviso, não erro.
        assert server.execute_query("SELECT 1", limit=10) == [{"n": 1}, {"n": 2}]


def test_busca_aceita_descricao_parcial_com_aviso():
    original = server.execute_query
    parcial = [{"nometab": "TGFCAB", "descrtab": "Cabeçalho"}]

    def corta(*a, **k):
        raise dbe.RowCapError(parcial)

    server.execute_query = corta
    try:
        rows, parcial_flag = server.partial_dictionary_rows("table_descriptions", "%A%")
    finally:
        server.execute_query = original
    assert rows == parcial and parcial_flag is True
    assert "incompletas" in server.partial_descriptions_note(True)
    assert server.partial_descriptions_note(False) == ""


def test_list_modules_dbe_agrega_no_banco():
    # O catálogo passa de 5000 tabelas e o DB Explorer corta ali: a contagem
    # precisa vir do banco, não de uma lista de nomes cortada.
    consultas = []

    def falso(sql, params=None, limit=server.DEFAULT_ROW_LIMIT):
        consultas.append(sql)
        return [{"prefixo": "TGF", "qtd": 1469}, {"prefixo": "XY", "qtd": 9}]

    original = server.execute_query
    server.execute_query = falso
    try:
        with _modo_dbe(None):
            saida = server.list_modules()
    finally:
        server.execute_query = original
    assert "GROUP BY" in consultas[0]
    assert "| TGF | 1469 |" in saida
    assert "XY" not in saida  # prefixo curto demais é ruído


def test_validate_query_dbe_executa_e_avisa_que_nao_ha_plano():
    sessao = _SessaoFalsa(["X"], [[1]])
    original = server.explain_plan

    def sem_plano(sql):
        raise AssertionError("o DB Explorer não tem EXPLAIN PLAN")

    server.explain_plan = sem_plano
    try:
        with _modo_dbe(sessao):
            ok = server.validate_query("SELECT 1 FROM DUAL")
    finally:
        server.explain_plan = original
    assert ok.startswith("✅") and "DBE" in ok
    assert sessao.recebido == ["SELECT 1 FROM DUAL"]


def test_validate_query_dbe_reporta_o_erro_do_banco():
    class Quebrada:
        def execute(self, sql):
            raise dbe.DbeError('ORA-00904: "X": identificador inválido')

    with _modo_dbe(Quebrada()):
        saida = server.validate_query("SELECT X FROM DUAL")
    assert saida.startswith("❌") and "ORA-00904" in saida


def test_validate_query_dbe_ainda_barra_escrita():
    sessao = _SessaoFalsa(["X"], [[1]])
    with _modo_dbe(sessao):
        assert server.validate_query("DELETE FROM TGFCAB").startswith("❌")
    assert sessao.recebido == []


def test_agrupamento_por_contagem_do_banco_equivale_ao_por_nome():
    nomes = ["TGFCAB", "TGFITE", "tgfpar", "TSIUSU", "TSIEMP", "XYZUNICA", "AB", "123456"]
    contagens = [
        {"prefixo": "TGF", "qtd": 3},
        {"prefixo": "TSI", "qtd": 2},
        {"prefixo": "XYZ", "qtd": 1},
        {"prefixo": "AB", "qtd": 1},
        {"prefixo": "123", "qtd": 1},
    ]
    assert group_prefix_counts(contagens) == group_prefixes(nomes)
    assert group_prefix_counts(None) == []


def test_queries_novas_existem_nos_dois_dialetos():
    for tipo in ("oracle", "sqlserver"):
        with _dialeto(tipo):
            triggers = dialects.query("triggers")
            assert triggers.count(dialects._BIND[tipo](1)) == 1
            prefixos = dialects.query("table_prefixes")
            assert "GROUP BY" in prefixos
            assert f"1, {dialects.PREFIXO_MODULO})" in prefixos


def test_triggers_fala_o_vocabulario_de_cada_banco():
    with _dialeto("oracle"):
        assert "ALL_TRIGGERS" in dialects.query("triggers")
    with _dialeto("sqlserver"):
        sql = dialects.query("triggers")
        assert "sys.triggers" in sql and "ExecIsInsertTrigger" in sql


def test_get_triggers_lista_e_trata_tabela_sem_trigger():
    original = (server.resolve_table_name, server.execute_query)
    server.resolve_table_name = lambda nome: (nome.upper(), [])
    server.execute_query = lambda sql, params=None, limit=None: [
        {"owner": "SANKHYA", "trigger_name": "TRG_X", "trigger_type": "BEFORE EACH ROW",
         "triggering_event": "INSERT OR UPDATE", "status": "ENABLED"},
    ]
    try:
        saida = server.get_triggers("tgfcab")
        server.execute_query = lambda sql, params=None, limit=None: []
        vazio = server.get_triggers("tgfcab")
    finally:
        server.resolve_table_name, server.execute_query = original
    assert "TRG_X" in saida and "INSERT OR UPDATE" in saida and "1 trigger(s)" in saida
    assert "Nenhuma trigger" in vazio or "Nada encontrado" in vazio


def test_instrucoes_so_falam_de_dbe_no_modo_dbe():
    # No modo direto o texto fica como sempre foi: nada de contexto a mais.
    assert not clients.is_multi() and active_client().mode == "direct"
    assert "MODO DBE" not in server.mcp.instructions
    assert "MULTI-CLIENTE" not in server.mcp.instructions
    assert server._client_instructions() == ""


# --- Vários clientes (clients.json, modo DBE) -------------------------------


def _entrada(**mudancas):
    """Uma entrada com os dois conjuntos de credencial: o modo escolhe qual vale."""
    base = {
        "dbe_url": "http://h:8380", "dbe_user": "vidya", "dbe_password": "segredo",
        "db_host": "10.0.0.5", "db_user": "RO", "db_password": "segredo-db",
    }
    return {**base, **mudancas}


def _tres_clientes(modo="dbe", **por_cliente):
    return clients.parse_registry({
        "acme": _entrada(descricao="Acme Ltda", **por_cliente.get("acme", {})),
        "beta": _entrada(dbe_url="http://beta:8380", db_type="sqlserver", db_schema="sankhya",
                         **por_cliente.get("beta", {})),
        "gama": _entrada(dbe_url="https://gama", **por_cliente.get("gama", {})),
    }, modo)


@contextmanager
def _cadastro(registrados):
    """Troca o cadastro de clientes do processo pelo informado, e devolve o original."""
    antes = dict(clients.CLIENTS)
    clients.CLIENTS.clear()
    clients.CLIENTS.update(registrados)
    try:
        yield
    finally:
        clients.CLIENTS.clear()
        clients.CLIENTS.update(antes)


def _recusa(fn, trecho):
    try:
        fn()
    except clients.ClientError as exc:
        assert trecho in str(exc), f"{trecho!r} não está em {str(exc)!r}"
    else:
        raise AssertionError(f"deveria recusar ({trecho})")


def _registro(dados, modo="dbe"):
    return clients.parse_registry(dados, modo)


def test_cadastro_le_os_clientes_do_modo_dbe():
    c = _tres_clientes("dbe")
    assert list(c) == ["acme", "beta", "gama"]
    assert all(x.mode == "dbe" for x in c.values())
    assert c["acme"].db_type == "oracle" and c["acme"].descricao == "Acme Ltda"
    assert c["beta"].db_type == "sqlserver" and c["beta"].schema == "SANKHYA"
    assert c["gama"].dbe_url == "https://gama" and c["acme"].dbe_user == "vidya"


def test_cadastro_le_os_clientes_do_modo_direct():
    c = _registro({
        "acme": _entrada(db_service="PROD", db_port=1522),
        "beta": _entrada(db_type="sqlserver", db_database="erp", db_service_name="X"),
    }, "direct")
    assert all(x.mode == "direct" for x in c.values())
    assert (c["acme"].host, c["acme"].port, c["acme"].sid, c["acme"].user) == ("10.0.0.5", 1522, "PROD", "RO")
    # Porta padrão segue o banco; o que não foi dito segue o padrão do .env.
    assert c["beta"].port == 1433 and c["beta"].database == "erp"
    assert c["beta"].service_name == "X" and c["beta"].password == "segredo-db"


def test_o_modo_escolhe_qual_credencial_vale():
    so_dbe = {"dbe_url": "http://h", "dbe_user": "u", "dbe_password": "p"}
    so_db = {"db_host": "h", "db_user": "u", "db_password": "p"}
    # Só exige o conjunto do modo em uso: o outro pode faltar.
    assert _registro({"a": so_dbe}, "dbe")["a"].dbe_url == "http://h"
    assert _registro({"a": so_db}, "direct")["a"].host == "h"
    _recusa(lambda: _registro({"a": so_dbe}, "direct"), "o modo direct exige db_host, db_user, db_password")
    _recusa(lambda: _registro({"a": so_db}, "dbe"), "o modo dbe exige dbe_url, dbe_user, dbe_password")


def test_cadastro_recusa_cliente_mal_formado():
    _recusa(lambda: _registro({}), "ao menos um cliente")
    _recusa(lambda: _registro([]), "ao menos um cliente")
    _recusa(lambda: _registro({"a": "texto"}), "objeto")
    _recusa(lambda: _registro({"a b": _entrada()}), "nome inválido")
    _recusa(lambda: _registro({"a": _entrada(db_type="mysql")}), "SANKHYA_DB_TYPE")
    _recusa(lambda: _registro({"a": _entrada(db_schema="X; DROP")}), "SANKHYA_DB_SCHEMA")
    _recusa(lambda: _registro({"a": _entrada(db_port="abc")}), "db_port")
    _recusa(lambda: _registro({"a": _entrada(default="sim")}), "true ou false")
    for faltando in ("dbe_url", "dbe_user", "dbe_password"):
        entrada = _entrada()
        del entrada[faltando]
        _recusa(lambda e=entrada: _registro({"a": e}), faltando)
    for faltando in ("db_host", "db_user", "db_password"):
        entrada = _entrada()
        del entrada[faltando]
        _recusa(lambda e=entrada: _registro({"a": e}, "direct"), faltando)


def test_cadastro_direct_nao_assume_localhost():
    # No .env o host padrão é localhost; no cadastro, esquecer o host apontaria o
    # cliente para a máquina local sem nenhum aviso.
    entrada = _entrada()
    del entrada["db_host"]
    _recusa(lambda: _registro({"a": entrada}, "direct"), "db_host")


def test_cadastro_recusa_chave_desconhecida_em_vez_de_ignorar():
    # `pasword` ignorado deixaria o cliente sem senha só na hora da consulta.
    _recusa(lambda: _registro({"a": _entrada(pasword="x")}), "pasword")
    _recusa(lambda: _registro({"a": _entrada(mode="direct")}), "mode")  # o modo é do processo
    _recusa(lambda: _registro({"a": _entrada(db_hots="x")}), "db_hots")


def test_cadastro_so_aceita_um_default():
    c = _tres_clientes(acme={"default": True})
    assert [n for n, x in c.items() if x.default] == ["acme"]
    _recusa(lambda: _tres_clientes(acme={"default": True}, beta={"default": True}), "acme, beta")


def test_cadastro_nome_e_case_insensitive():
    _recusa(lambda: _registro({"Acme": _entrada(), "acme": _entrada()}), "caixa")


def test_senha_do_cliente_nao_aparece_no_repr():
    texto = repr(_tres_clientes()["acme"])
    for segredo in ("segredo", "segredo-db", "developer"):
        assert segredo not in texto


def test_selecao_de_cliente():
    with _cadastro(_tres_clientes(beta={"default": True})):
        assert clients.select_client(None).name == "beta"
        assert clients.select_client("").name == "beta"
        assert clients.select_client("ACME").name == "acme"  # case-insensitive
        _recusa(lambda: clients.select_client("zeta"), "não cadastrado")
        _recusa(lambda: clients.select_client("zeta"), "`acme`, `beta`, `gama`")


def test_sem_default_e_com_varios_clientes_o_nome_e_obrigatorio():
    # Escolher sozinho devolveria dado do cliente errado sem erro de SQL.
    with _cadastro(_tres_clientes()):
        _recusa(lambda: clients.select_client(None), "Informe o cliente")
        assert clients.select_client("gama").name == "gama"


def test_um_cliente_so_dispensa_o_nome_e_o_default():
    with _cadastro({"unico": _tres_clientes()["acme"]}):
        assert clients.select_client(None).name == "acme"
        assert not clients.is_multi()


def test_cliente_ativo_sem_default_nao_e_escolhido_por_conta_propria():
    with _cadastro(_tres_clientes()):
        _recusa(active_client, "Informe o cliente")
        with use_client(clients.CLIENTS["beta"]):
            assert active_client().name == "beta"
        _recusa(active_client, "Informe o cliente")  # restaurado ao sair


def _arquivo(conteudo, nome="clients.json"):
    diretorio = tempfile.mkdtemp()
    caminho = Path(diretorio) / nome
    caminho.write_text(conteudo, encoding="utf-8")
    return caminho


_JSON_DOIS = (
    '{"acme": {"dbe_url": "http://a:8380", "dbe_user": "u", "dbe_password": "p"},'
    ' "beta": {"dbe_url": "http://b:8380", "dbe_user": "u", "dbe_password": "p", "default": true}}'
)
_JSON_DOIS_DIRECT = (
    '{"acme": {"db_host": "a", "db_user": "u", "db_password": "p"},'
    ' "beta": {"db_host": "b", "db_user": "u", "db_password": "p", "default": true}}'
)
_ENV_DBE = {
    "SANKHYA_MODE": "dbe",
    "SANKHYA_DBE_URL": "http://env:8380",
    "SANKHYA_DBE_USER": "e",
    "SANKHYA_DBE_PASSWORD": "e",
}
_ENV_DIRECT = {"SANKHYA_MODE": "direct", "SANKHYA_DB_HOST": "env-host"}


def test_cadastro_vale_nos_dois_modos():
    dbe_ = clients.load_clients({"SANKHYA_MODE": "dbe", "SANKHYA_CLIENTS_FILE": str(_arquivo(_JSON_DOIS))})
    assert list(dbe_) == ["acme", "beta"] and dbe_["acme"].mode == "dbe"
    assert dbe_["beta"].default and not dbe_["acme"].default
    direto = clients.load_clients(
        {"SANKHYA_MODE": "direct", "SANKHYA_CLIENTS_FILE": str(_arquivo(_JSON_DOIS_DIRECT))}
    )
    assert list(direto) == ["acme", "beta"] and direto["acme"].mode == "direct"
    assert direto["beta"].host == "b"


def test_cadastro_dispensa_as_variaveis_de_conexao_do_env():
    # Regressão: o servidor exigia SANKHYA_DBE_URL/USER/PASSWORD do .env mesmo com
    # clients.json, que traz as credenciais de cada cliente.
    caminho = _arquivo(_JSON_DOIS)
    resultado = clients.load_clients({"SANKHYA_MODE": "dbe", "SANKHYA_CLIENTS_FILE": str(caminho)})
    assert list(resultado) == ["acme", "beta"]


def test_arquivo_com_credencial_so_do_outro_modo_derruba_o_boot():
    # O modo vale para todos: um cliente sem as credenciais dele não ficaria de fora
    # em silêncio, e sim apareceria como erro de subida.
    caminho = _arquivo(_JSON_DOIS)
    _recusa(
        lambda: clients.load_clients({"SANKHYA_MODE": "direct", "SANKHYA_CLIENTS_FILE": str(caminho)}),
        "o modo direct exige db_host",
    )


def test_arquivo_indicado_que_nao_existe_derruba_o_boot():
    _recusa(
        lambda: clients.load_clients({**_ENV_DBE, "SANKHYA_CLIENTS_FILE": "/nao/existe.json"}),
        "não existe",
    )


def test_json_quebrado_diz_qual_arquivo():
    caminho = _arquivo("{ nao e json")
    _recusa(
        lambda: clients.load_clients({**_ENV_DBE, "SANKHYA_CLIENTS_FILE": str(caminho)}),
        str(caminho),
    )


def test_sem_arquivo_cai_no_cliente_do_env_nos_dois_modos():
    original = clients.DEFAULT_CLIENTS_FILE
    clients.DEFAULT_CLIENTS_FILE = Path(tempfile.mkdtemp()) / "clients.json"
    try:
        dbe_ = clients.load_clients(_ENV_DBE)
        direto = clients.load_clients(_ENV_DIRECT)
    finally:
        clients.DEFAULT_CLIENTS_FILE = original
    assert list(dbe_) == ["default"] and dbe_["default"].dbe_url == "http://env:8380"
    assert list(direto) == ["default"] and direto["default"].host == "env-host"


def test_clients_json_na_raiz_e_usado_sem_variavel():
    original = clients.DEFAULT_CLIENTS_FILE
    clients.DEFAULT_CLIENTS_FILE = _arquivo(_JSON_DOIS_DIRECT)
    try:
        assert list(clients.load_clients(_ENV_DIRECT)) == ["acme", "beta"]
    finally:
        clients.DEFAULT_CLIENTS_FILE = original


class _OracledbFalso:
    """Registra o que o servidor pede ao driver, sem conectar em nada."""

    def __init__(self):
        self.inits, self.pools = 0, []

    def init_oracle_client(self, lib_dir):
        self.inits += 1

    def makedsn(self, host, port, sid=None, service_name=None):
        return f"{host}:{port}/{service_name or sid}"

    def create_pool(self, **kw):
        self.pools.append(kw)
        return object()


def test_pool_oracle_e_por_cliente_e_o_instant_client_inicia_uma_vez():
    falso = _OracledbFalso()
    original = sys.modules.get("oracledb")
    sys.modules["oracledb"] = falso
    pools_antes, pronto_antes = dict(dialects._pools), dialects._instantclient_pronto
    dialects._pools.clear()
    dialects._instantclient_pronto = False
    try:
        c = _registro({
            "a": _entrada(db_host="ha", db_schema="sankhya"),
            "b": _entrada(db_host="hb", db_service_name="SVC"),
        }, "direct")
        with use_client(c["a"]):
            pool_a = dialects._oracle_pool()
            assert dialects._oracle_pool() is pool_a  # reaproveita
        with use_client(c["b"]):
            assert dialects._oracle_pool() is not pool_a
    finally:
        dialects._pools.clear()
        dialects._pools.update(pools_antes)
        dialects._instantclient_pronto = pronto_antes
        if original is None:
            del sys.modules["oracledb"]
        else:
            sys.modules["oracledb"] = original
    assert falso.inits == 1 and len(falso.pools) == 2
    a, b = falso.pools
    assert a["dsn"] == "ha:1521/XE" and b["dsn"] == "hb:1521/SVC"
    assert a["user"] == "RO" and a["password"] == "segredo-db"
    # O schema é de cada cliente: só o `a` aponta a sessão.
    assert a["session_callback"] is not None and b["session_callback"] is None
    cursor = _FakeCursor(0)
    a["session_callback"](_FakeConn(cursor), None)
    assert cursor.executado == ["ALTER SESSION SET CURRENT_SCHEMA = SANKHYA"]


def test_resposta_traz_o_cliente_so_quando_ha_mais_de_um():
    sessao = _SessaoFalsa(["X"], [[1]])
    original = server.get_session
    server.get_session = lambda *a: sessao
    try:
        with _cadastro(_tres_clientes(acme={"default": True})):
            multi = server.run_query("SELECT 1 FROM DUAL")
            assert multi.startswith("> **Cliente:** acme · Oracle · DBE\n\n")
            assert server.run_query("SELECT 1 FROM DUAL", client="beta").startswith(
                "> **Cliente:** beta · SQL Server · DBE"
            )
        with _cadastro({"unico": _tres_clientes()["acme"]}):
            assert "Cliente:" not in server.run_query("SELECT 1 FROM DUAL")
    finally:
        server.get_session = original


def test_tool_sem_cliente_e_sem_default_nao_executa_nada():
    sessao = _SessaoFalsa(["X"], [[1]])
    original = server.get_session
    server.get_session = lambda *a: sessao
    try:
        with _cadastro(_tres_clientes()):
            saida = server.run_query("SELECT 1 FROM DUAL")
            assert saida.startswith("❌") and "Informe o cliente" in saida
            assert server.run_query("SELECT 1", client="zeta").startswith("❌")
    finally:
        server.get_session = original
    assert sessao.recebido == []


def test_cada_cliente_consulta_o_seu_sankhya_com_o_seu_dialeto():
    # O ponto da feature: a mesma tool, clientes diferentes, SQL e destino de cada um.
    chamadas = []

    class Registra:
        def __init__(self, url):
            self.url = url

        def execute(self, sql):
            chamadas.append((self.url, sql))
            return (["TRIGGER_NAME"], [["TRG"]], ["S"], False)

    original = (server.get_session, server.resolve_table_name)
    server.get_session = lambda url, user, senha: Registra(url)
    server.resolve_table_name = lambda nome: (nome.upper(), [])
    try:
        with _cadastro(_tres_clientes()):
            server.get_triggers("TGFCAB", client="acme")
            server.get_triggers("TGFCAB", client="beta")
    finally:
        server.get_session, server.resolve_table_name = original
    (url_a, sql_a), (url_b, sql_b) = chamadas
    assert url_a == "http://h:8380" and "ALL_TRIGGERS" in sql_a and "'TGFCAB'" in sql_a
    assert url_b == "http://beta:8380" and "sys.triggers" in sql_b and "'TGFCAB'" in sql_b


def test_schema_do_cliente_qualifica_so_as_consultas_dele():
    with _cadastro(_tres_clientes()):
        with use_client(clients.CLIENTS["beta"]):
            assert "SANKHYA.TDDINS" in dialects.query("resolve_table")
        with use_client(clients.CLIENTS["acme"]):
            assert "SANKHYA." not in dialects.query("resolve_table")


def test_list_clients_marca_o_default_e_nao_vaza_credencial():
    with _cadastro(_tres_clientes(beta={"default": True})):
        saida = server.list_clients()
    assert "acme" in saida and "beta" in saida and "gama" in saida
    assert "Acme Ltda" in saida and "http://beta:8380" in saida
    assert "| beta | sim |" in saida and "| acme |  |" in saida
    for segredo in ("segredo", "vidya"):
        assert segredo not in saida


def test_instrucoes_multi_cliente_listam_os_clientes_e_mandam_perguntar():
    with _cadastro(_tres_clientes(acme={"default": True})):
        texto = server._client_instructions()
    assert "MODO DBE" in texto and "MULTI-CLIENTE" in texto
    assert "`acme`, `beta`, `gama`" in texto
    assert "O cliente padrão é `acme`" in texto
    assert "PERGUNTE" in texto
    assert "segredo" not in texto
    with _cadastro(_tres_clientes()):
        assert "`client` é obrigatório" in server._client_instructions()


def test_modo_direct_com_varios_clientes_conecta_em_cada_um():
    # O mesmo cadastro vale no modo direto: cada cliente abre a sua conexão.
    visto = []

    @contextmanager
    def conexao_falsa():
        cliente = active_client()
        visto.append((cliente.name, cliente.host, cliente.db_type))
        yield _FakeConn(_FakeCursor(1))

    c = _registro({
        "acme": _entrada(db_host="host-acme"),
        "beta": _entrada(db_host="host-beta", db_type="sqlserver"),
    }, "direct")
    original = server.connect
    server.connect = conexao_falsa
    try:
        with _cadastro(c):
            saida_a = server.run_query("SELECT 1 FROM DUAL", client="acme")
            saida_b = server.run_query("SELECT 1", client="beta")
            recusa = server.run_query("SELECT 1 FROM DUAL")
    finally:
        server.connect = original
    assert visto == [("acme", "host-acme", "oracle"), ("beta", "host-beta", "sqlserver")]
    assert saida_a.startswith("> **Cliente:** acme · Oracle · direto")
    assert saida_b.startswith("> **Cliente:** beta · SQL Server · direto")
    assert recusa.startswith("❌") and "Informe o cliente" in recusa


def test_instrucoes_do_modo_direct_com_varios_clientes_nao_falam_de_dbe():
    with _cadastro(_registro({"a": _entrada(), "b": _entrada(default=True)}, "direct")):
        texto = server._client_instructions()
    assert "MULTI-CLIENTE" in texto and "MODO DBE" not in texto
    assert "O cliente padrão é `b`" in texto


def test_toda_tool_com_banco_aceita_client():
    import inspect

    sem_banco = {"list_clients", "check_updates"}
    for tool in server.mcp._tool_manager.list_tools():
        params = inspect.signature(tool.fn).parameters
        assert ("client" in params) == (tool.name not in sem_banco), tool.name


if __name__ == "__main__":
    testes = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for teste in testes:
        teste()
        print(f"[OK] {teste.__name__}")
    print(f"\n{len(testes)} teste(s) passaram.")
