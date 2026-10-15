# Changelog

Todas as mudanças relevantes deste projeto são registradas aqui.

O formato segue [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/) e o
versionamento segue [SemVer](https://semver.org/lang/pt-BR/).

A tool `check_updates` do próprio servidor compara a versão instalada com a
última publicada e mostra as entradas deste arquivo no intervalo, e as **notas
de cada release no GitHub** são geradas a partir da seção correspondente deste
arquivo. Uma seção vazia reprova a publicação.

## [2.1.0] - 2026-10-15

### Alterado

- **O modo é de cada cliente.** No `clients.json`, cada entrada declara o próprio
  `mode` (`direct` ou `dbe`, obrigatório) e traz as credenciais dele (`db_*` ou
  `dbe_*`). Um mesmo servidor mistura clientes diretos e DBE. Antes, o
  `SANKHYA_MODE` valia para o processo inteiro e todos os clientes eram de um modo
  só. Quem já tem `clients.json` precisa acrescentar `"mode"` a cada cliente; sem
  ele a subida falha e diz qual cliente está sem.
- `SANKHYA_MODE` só é lido (e continua obrigatório) quando não há `clients.json`:
  nesse caso o `.env` monta um cliente só, `default`. Com o arquivo, o `.env` não
  entra mais para modo nem para conexão.
- As instruções do servidor dizem a quais clientes o limite do DB Explorer vale
  quando os modos estão misturados.

## [2.0.0] - 2026-10-14

### Adicionado

- **Modo DBE** (`SANKHYA_MODE=dbe`): o servidor deixa de abrir conexão com o
  banco e passa a consultar pelo DB Explorer do Sankhya. Faz login com usuário e
  senha da aplicação (`SANKHYA_DBE_URL`, `SANKHYA_DBE_USER`,
  `SANKHYA_DBE_PASSWORD`) por `MobileLoginSP.login`, guarda o `jsessionid` na
  vida do processo, refaz o login se a aplicação o recusar e manda toda tool
  para `DbExplorerSP.executeQuery`. Serve a quem só alcança o Sankhya por HTTP.
  `SANKHYA_MODE=direct` mantém a conexão com o banco pelo driver.
- **Vários clientes**: `clients.json` cadastra clientes, cada um com o seu banco
  (`oracle` ou `sqlserver`) e, no modo DBE, a sua sessão no Sankhya. Vale nos dois
  modos: `SANKHYA_MODE` escolhe qual credencial de cada cliente é usada (`db_*` no
  `direct`, `dbe_*` no `dbe`), e no modo direto cada cliente Oracle ganha o seu
  pool. Toda tool de banco ganha o parâmetro `client`; `list_clients` mostra os
  cadastrados sem credenciais. No máximo um cliente é `"default"` (dois derrubam a
  subida); sem default e com mais de um cliente, a tool recusa em vez de escolher.
  Com mais de um cliente, a resposta abre com `Cliente: acme · Oracle · DBE`.
  `SANKHYA_CLIENTS_FILE` aponta para outro local. Chave desconhecida no arquivo é
  erro, e o aviso de permissão aparece se outros usuários podem lê-lo. Sem o
  arquivo, as variáveis `SANKHYA_*` valem como um cliente só.
- Tool `get_triggers`: triggers de uma tabela, com momento, evento e estado. A
  consulta é do dialeto — `ALL_TRIGGERS` no Oracle, `sys.triggers` no SQL
  Server.

### Alterado

- **Quebra de compatibilidade:** `SANKHYA_MODE` é obrigatório e não tem mais
  padrão. Antes, ausente valia `direct`; agora o servidor falha na subida e diz o
  que falta. Quem já usa conexão direta precisa acrescentar `SANKHYA_MODE=direct`
  ao `.env` (ou ao `.sankhya-mcp.env` do projeto). O motivo: um padrão tentaria
  conectar no banco justamente onde só o DB Explorer é alcançável, e o erro viria
  como "conexão recusada".
- No modo DBE, `validate_query` executa a consulta (somente leitura) e descarta o
  resultado, porque o DB Explorer não gera plano de execução; a resposta diz isso.
  `list_modules` conta os prefixos no banco em vez de listar os nomes: o DB
  Explorer corta em 5000 linhas e o catálogo passa disso.
- Tool que precisa do resultado inteiro (colunas, índices, FKs, triggers) falha
  com mensagem clara se o DB Explorer cortar o resultado, em vez de entregar parte
  como se fosse tudo. `search_tables` e `search_columns` aceitam descrição
  parcial e avisam.

## [1.4.1] - 2026-09-10

### Corrigido

- O workflow de release falhava quando a tag era reposicionada: `gh release
  create` recusa criar um release que já existe. Acontece de verdade — tag
  publicada e, em seguida, rebase por causa de commit novo na `main`. Agora o
  workflow atualiza as notas quando o release existe e só cria quando não
  existe.

## [1.4.0] - 2026-09-10

### Adicionado

- `table_sample` anexa o rótulo do dicionário ao valor das colunas enumeradas
  (`L` vira `L (Liberada)`). Amostra de tabela Sankhya era uma parede de
  códigos de uma letra, ilegível sem consultar o domínio campo a campo.

### Corrigido

- O rodapé do `describe_table` e as instruções do MCP diziam **domínio
  fechado** e mandavam usar exatamente os valores de `opcoes`. Não procede: o
  dicionário declara o que a aplicação oferece, não esgota o que está gravado.
  Na base medida, `TGFCAB.TIPMOV` tem `Z` em 23 de 139 linhas sem constar em
  `TDDOPC`. Quem seguisse aquele texto montaria `IN (...)` com a lista
  declarada e perderia linhas em silêncio — o oposto do que as opções vieram
  resolver. O texto agora pede confirmação com `table_sample` antes de filtro
  exaustivo.
- Valor gravado fora do domínio declarado aparece **sem rótulo** na amostra, em
  vez de passar despercebido como código válido.

## [1.3.0] - 2026-09-10

### Adicionado

- `search_tables` passa a mostrar a **descrição de cada tabela** (`TDDTAB`):
  `TGFCAB` deixa de ser só um nome e aparece como "Entrada e Saída de Produto".
  Cobre 69% do catálogo, com 100% dos verbetes preenchidos.
- `search_columns` passa a mostrar a **descrição de cada campo** por tabela
  (`TDDCAM`). A chave é o par tabela+campo, porque o mesmo nome significa
  coisas diferentes conforme a tabela: `CODPARC` é "Parceiro" na TGFAAXN e
  "Cód. Parceiro" na TGFACO.
- Integração contínua (`.github/workflows/ci.yml`): os testes rodam a cada
  push na `main` e a cada pull request. Antes só rodavam quando alguém lembrava
  ou dentro do `tools/release.sh` — tarde demais, a quebra aparecia só na hora
  de publicar.

### Alterado

- As duas tools de busca deixaram de emitir a coluna `comments`, sempre vazia
  na base Sankhya, no lugar da descrição do dicionário. O comentário do
  catálogo continua como reserva, para bases onde ele exista.
- `search_columns` normaliza o indicador de nulo para `S`/`N`, como o
  `describe_table` já fazia. A mesma coluna não pode se descrever de um jeito
  numa tool e de outro na vizinha.

## [1.2.1] - 2026-09-10

### Adicionado

- Release notes automáticas: o push de uma tag `v*` dispara o workflow
  `.github/workflows/release.yml`, que publica o release no GitHub com a seção
  correspondente deste CHANGELOG como corpo. Vale para qualquer tag, inclusive
  a empurrada à mão, porque o gatilho é a tag e não o script de publicação.
- `tools/release_notes.py`: imprime as notas de uma versão. Usado pelo workflow
  e pelos validadores locais.

### Alterado

- `tools/release.sh` e `tools/release.ps1` passaram a recusar a tag quando a
  seção da versão no CHANGELOG está vazia ou só tem o cabeçalho. Antes bastava
  a versão do topo bater com `src/version.py`, o que deixava passar uma release
  sem nenhuma explicação do que mudou.
- Parser do CHANGELOG refatorado em `changelog_sections()`, com
  `changelog_entries()` (intervalo, para o aviso de atualização) e
  `changelog_section()` (versão única, para as notas do release) filtrando o
  mesmo resultado. Um parser só para os dois consumidores.

## [1.2.0] - 2026-09-10

### Adicionado

- `describe_table` agora traz a **descrição em português** de cada campo,
  vinda do dicionário Sankhya (`TDDCAM`). O comentário de coluna do catálogo
  vem vazio na base Sankhya (0 de 69.782 colunas medidas), então até aqui a
  tool devolvia uma coluna sempre em branco.
- `describe_table` agora traz **todos os valores aceitos** dos campos
  enumerados, com o rótulo, vindos de `TDDOPC` — por exemplo `TIPMOV` com
  `P=Pedido de venda; V=Venda; C=Compra`. Sem isso, quem escrevia a query
  precisava adivinhar o literal do `WHERE`: `V` e `P` são ambos válidos, e usar
  um pelo outro devolve o documento errado sem levantar erro de SQL.
- `get_foreign_keys` ganhou a seção **Ligações do dicionário** (`TDDLIG`), o
  relacionamento que o JAPE enxerga. Não coincide com a FK física: há ligação
  sem FK e FK sem ligação.
- Nova tool `check_updates`: compara a versão instalada com a última publicada
  no repositório de origem e mostra o que mudou e como atualizar.
- Aviso de atualização no boot do servidor, injetado nas instruções do MCP.
  Consulta em cache de 24h com timeout de 2s; offline não impede o start.
- Versionamento do projeto: `src/version.py` como fonte única, este CHANGELOG e
  tags SemVer.
- `tools/release.sh` e `tools/release.ps1`: validam testes, sincronia entre
  `__version__` e CHANGELOG, working tree limpa e ausência da tag antes de
  publicar.
- Suporte a **SQL Server** além do Oracle, via `SANKHYA_DB_TYPE`. Todas as
  queries de catálogo, a conexão, a transação de leitura e o plano de execução
  passaram para `src/dialects.py`.
- `table_sample` ganhou o parâmetro `columns` para projetar colunas.
- Suporte a conexão Oracle por *service name* (`SANKHYA_DB_SERVICE_NAME`).
- Suporte a `SANKHYA_DB_SCHEMA` para quando o login do MCP não é dono das
  tabelas.
- Configuração por projeto via `.sankhya-mcp.env`, sobrescrevendo o `.env` geral.
- Menu de CLIs no instalador, com suporte ao Codex.

### Alterado

- Saída de `describe_table` reformatada: tipo, tamanho, precisão e escala
  colapsados numa coluna só (`NUMBER(15,2)`) e indicador de nulo normalizado
  entre Oracle e SQL Server. Abre espaço para descrição e domínio sem inchar a
  tabela.
- Instruções do MCP passaram a exigir `describe_table` antes de escrever
  qualquer filtro com valor literal.
- Inicialização do Oracle Instant Client passou a ser preguiçosa, para não
  travar o handshake MCP.

### Corrigido

- `table_sample` fazia `SELECT *` e devolvia tabelas de 600 colunas inteiras:
  TGFTOP com `limit=2` gerava 11.902 caracteres. Agora projeta e avisa o corte.
- Queries de metadados fixam um único *owner*, evitando colunas duplicadas
  quando a mesma tabela existe em mais de um schema visível.
- Sessão Oracle passa a apontar para `SANKHYA_DB_SCHEMA`, para que `run_query`
  funcione sem qualificar tabela à mão.
- EntityName não reconhecido devolve mensagem explicando o que houve, em vez de
  "nenhum índice encontrado", que se lia como "esta tabela não tem índice".
- Valores *falsy* (`0`, `False`, `Decimal("0.00")`) deixaram de virar célula
  vazia no Markdown, e `|` em comentário passou a ser escapado.
- `run_query` fechou brechas de escrita disfarçada de leitura: comentários,
  múltiplos comandos, `WITH FUNCTION/PROCEDURE` e `SELECT ... INTO`.
- `start.sh` deixou de usar `source` para ler o `.env`: um arquivo de
  repositório qualquer não deve executar comandos no host.
- `describe_table` degrada em vez de quebrar quando o dicionário Sankhya não
  existe no schema conectado.

## [1.1.0] - 2026-05-11

### Adicionado

- Instalação automatizada no Linux (`setup.sh`, `start.sh`) com download do
  Oracle Instant Client via GitHub Releases.
- `tools/repackage-instantclient-linux.sh` para reempacotar o Instant Client.

### Corrigido

- Detecção de `libaio1t64` no Ubuntu 24+ e criação do symlink `libaio.so.1`.
- Criação do `~/.claude.json` quando ausente.
- Versão do `oracledb` limitada abaixo da 3.0.0.

## [1.0.0] - 2026-05-02

### Adicionado

- Primeira versão do servidor MCP de exploração do schema Sankhya em Oracle,
  com as tools `describe_table`, `search_tables`, `search_columns`,
  `get_foreign_keys`, `get_indexes`, `run_query`, `validate_query`,
  `table_sample`, `search_entities` e `list_modules`.

[1.4.1]: https://github.com/frshaka/sankhya-schema-mcp/releases/tag/v1.4.1
[1.4.0]: https://github.com/frshaka/sankhya-schema-mcp/releases/tag/v1.4.0
[1.3.0]: https://github.com/frshaka/sankhya-schema-mcp/releases/tag/v1.3.0
[1.2.1]: https://github.com/frshaka/sankhya-schema-mcp/releases/tag/v1.2.1
[1.2.0]: https://github.com/frshaka/sankhya-schema-mcp/releases/tag/v1.2.0
[1.1.0]: https://github.com/frshaka/sankhya-schema-mcp/releases/tag/v1.1
[1.0.0]: https://github.com/frshaka/sankhya-schema-mcp/releases/tag/v1.0
