# AGENTS.md — guide for LLM agents working in this repository

## What this project is

`confdb` extracts a 1C:Enterprise 8 configuration (`.cf` / `.cfe` / `.epf`) into a
SQLite knowledge base — metadata objects, attributes with resolved types, tabular
sections, BSL modules/methods, SKD report queries — and serves it to LLM agents via
the MCP server **`1confdb-knw`** (stdio, read-only). The unpack algorithm is a port of
[v8unpack](https://github.com/saby-integration/v8unpack) (MIT; reference copy in
`_vendor/v8unpack`, see `NOTICE.md`). **Decode only** — never add encode/pack code.

Domain glossary: 1C = Russian business-automation platform; BSL = its built-in
(Russian-keyword) language; Catalog=справочник, Document=документ,
(Information/Accumulation)Register=регистр, Enum=перечисление, tabular
section=табличная часть (row table of an object).

## Hard constraints

- Python >= 3.9, **runtime stdlib only** (pytest is the only dev extra).
- Windows-oriented: `.bat` wrappers in repo root; venv in `.venv` (MS Store Python:
  `.venv\Scripts\python.exe` is a launcher, the real worker is a child process).
- **No git on this machine** — git commands are unavailable.
- Comments, docstrings and user-facing text are in **Russian**.
- The 885 MB test file `SmallBusinessKz_3_0_4_4_cf.cf` lives in `cf/` (or repo root);
  never commit it; a full extract takes ~2.5 min with `--workers 8` — keep it out of
  unit tests (tests use small synthetic fixtures).
- Keep the decoder equivalent to `_vendor/v8unpack`; comparison helpers in `_tmp/`.

## Layout

- `src/confdb/extract.py` — pipeline stages 0/1/3 (containers → inflate → decode).
- `src/confdb/__main__.py` — CLI: `extract`, `check`, `1confdb-knw`.
- `src/confdb/mcp_server.py` — MCP server `1confdb-knw <db>` (12 tools, read-only;
  self-describing: schema primer + glossary + workflow in `initialize.instructions`).
  Transports: stdio by default; `--port N` — HTTP (Streamable HTTP `POST /mcp`,
  legacy SSE `/sse`) for SSH-tunnel access (`ssh -L N:127.0.0.1:N`).
- `src/confdb/tui.py` — console UI (user chose console over GUI; do not suggest tkinter).
- `src/confdb/bsl_parser.py` — splits BSL modules into procedures/functions.
- `src/confdb/query_lang.py` — 1C query language lexer/parser/semantic validator.
- `src/confdb/db/writer.py` — SQLite schema + dump writer (batched inserts; BSL
  parsing parallelized via `workers`).
- `src/confdb/bench.py` — hardware benchmark: non-linear object sample (stride over
  the whole top-level list + one object of every type) timed through stage 3 + DB
  write at 1/2/4/8/CPU processes; best `workers` saved to `~/.confdb/config.json`
  (`src/confdb/config.py` — shared user config, also used by the TUI).
- `src/confdb/v8/` — ported unpack core.
- `tests/` — fast tests (`test.bat`); `_tmp/` — throwaway probes (gitignored).

## Commands

```bat
.venv\Scripts\python.exe -m pip install -e ".[dev]"   :: once
test.bat                                              :: pytest (54 tests)
confdb.bat extract <file.cf> --db out.db --workers 8
confdb.bat check out.db                               :: validate all SKD queries
confdb.bat bench <file.cf>                            :: tune workers to hardware
1confdb-knw.bat out.db                                :: MCP server (stdio)
1confdb-knw.bat out.db --port 8765                    :: MCP over HTTP (SSH tunnel)
.venv\Scripts\python.exe -m compileall -q src\confdb  :: static check
```

Deployed copies used by the user: `dist\confdb` and `D:\Projects\1confdb-knw-main`
(the latter also has a real copy in `.venv\Lib\site-packages\confdb` — sync it too)
(sync with `robocopy src\confdb <dest>\src\confdb /MIR /XD __pycache__` + README/bats
after changing `src`).

## Database schema (quick map)

- `meta_object(path, type, type_ru, name, uuid, parent_id, ord)` — path like
  `Catalog/Контрагенты` or nested `…/CatalogForm/ФормаЭлемента`.
- `meta_attribute(object_id, ord, name, type_str, tabular)` — fields; `tabular`
  names the tabular section a field belongs to. `type_str`: `Строка(50)`,
  `Ссылка: Catalog/Х`, `ОпределяемыйТип: DefinedType/Х (…)`, composites with ` | `.
- `meta_tabular(object_id, ord, name)`; `attribute_ref(attribute_id, uuid, object_id)`
  — field-type → object links (joins/impact analysis).
- `module(object_id, code_name, context, body)` — body = module text WITHOUT method
  bodies; `method(…, kind, name, signature, directives, description, body)` —
  body strictly `Процедура/Функция … Конец…`.
- `enum_value`, `predefined`, `common_target`, `skd_query(query)`,
  `subsystem_content`, `file`, `source`.

## Gotchas learned the hard way

- SQLite writes: keep the rollback journal; **never** `PRAGMA journal_mode=MEMORY`
  (an interrupted write otherwise leaves a "valid-looking" near-empty file).
- The brace-file parser returns numbers as **strings** — compare via `str(x)`.
- Reference uuids inside type descriptors are NOT object uuids: resolved via the
  root `.10` stream table, DefinedType headers (`header[0][1][1]`) and
  `attribute_ref`.
- Tabular section in a header = `[section record, '1', fields bag]` where the bag
  starts with canonical uuid `888744e1-b616-11d4-9436-004095e12fc7`.
- Acceptance criterion for the query validator: **375/375** SKD queries of the test
  configuration pass (`confdb check`); keep it green when touching
  `query_lang.py` / `writer.py`.
- MCP server is read-only by design (`?mode=ro`, `sql` tool rejects non-SELECT).
