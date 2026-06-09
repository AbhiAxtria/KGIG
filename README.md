# plsqlmig — PL/SQL → AST graph → Neo4j → PySpark migration triage

A pipeline that parses Oracle PL/SQL (packages, procedures, functions, blocks),
builds an information graph from the structure, stores it in Neo4j, scores each
unit for PySpark migration suitability, and emits PySpark skeletons for the
convertible parts.

## Pipeline

```
.sql file
   │  plsql_structure.parse_source      (structural front end)
   ▼
PlsqlObject / PlsqlUnit  (+ procedural feature counts, raw SQL statements)
   │  sql_analyzer.analyze_statement    (sqlglot: reads/writes, set-based signals, Oracle→Spark)
   ▼
graph_builder.build_graph              (typed nodes + relationships)
   ├─► neo4j_store.ingest              (MERGE into Neo4j, or --dry-run Cypher)
   ├─► scorer.score_graph             (migration triage rubric)
   └─► pyspark_gen.generate_pyspark   (PySpark skeletons for convertible units)
```

## Run

```bash
pip install -r requirements.txt

# Dry run (no DB needed): prints triage + writes Cypher and PySpark
python -m plsqlmig.cli sample/sample_pkg.sql \
    --dry-run --cypher-out out.cypher --pyspark-out out_pyspark.py

# Live ingest into Neo4j
python -m plsqlmig.cli your_file.sql \
    --neo4j-uri bolt://localhost:7687 --neo4j-user neo4j --neo4j-pass <pw>
```

## Neo4j graph model

Node labels: `Package`, `Procedure`, `Function`, `Trigger`, `SqlStatement`,
`Table`, `Feature`.
Relationships: `CONTAINS`, `EXECUTES`, `READS`, `WRITES`, `HAS_FEATURE`.

Useful queries once loaded:

```cypher
// Procedures that write a table that other procedures read (migration ordering)
MATCH (p:Procedure)-[:EXECUTES]->(:SqlStatement)-[:WRITES]->(t:Table)
      <-[:READS]-(:SqlStatement)<-[:EXECUTES]-(q:Procedure)
RETURN p.name, t.name, q.name;

// Most Oracle-locked units
MATCH (p:Procedure)-[h:HAS_FEATURE]->(f:Feature)
RETURN p.name, collect(f.name + ' x' + h.count) AS oracle_features;
```

## Migration triage rubric (scorer.py)

Two scores per unit:
- **set_based_score** — joins, aggregates, window functions, GROUP BY, and bulk
  DML (INSERT…SELECT / MERGE). High = parallel data transformation.
- **oracle_lock_score** — cursor FOR loops, sequences (`.NEXTVAL`), autonomous
  transactions, savepoints, `DBMS_*`/`UTL_*` calls, `CONNECT BY`, etc. High =
  depends on Oracle procedural/transactional/OLTP behavior with no Spark analog.

Decision:
- `set_based < 2`                         → **KEEP_IN_ORACLE** (not a transform workload)
- `set_based ≥ 2` and `lock ≤ 3`          → **CONVERT_TO_PYSPARK**
- `set_based ≥ 2`, `lock > 3`, `set ≥ lock` → **REWRITE_FOR_PYSPARK**
- otherwise                               → **KEEP_IN_ORACLE**

Weights are explicit in `scorer.py` and meant to be tuned to your codebase.
Treat the output as triage for human review, not an automatic decision.

## Production upgrade: replace the regex front end with ANTLR

`plsql_structure.py` uses regex to carve objects and count features. It is fast
to stand up but not a real PL/SQL parser. For "very complex packages," swap in a
true parse:

1. Take the PL/SQL grammar from `antlr/grammars-v4` (`sql/plsql`:
   `PlSqlLexer.g4`, `PlSqlParser.g4`).
2. Generate the Python target:
   `antlr4 -Dlanguage=Python3 PlSqlLexer.g4 PlSqlParser.g4`
3. Write a `ParseTreeListener` that emits the same `PlsqlObject` / `PlsqlUnit`
   dataclasses this module already defines.

Because every downstream module (graph_builder, scorer, pyspark_gen, neo4j_store)
consumes those dataclasses, nothing downstream changes. You get a real AST for
control flow, exception handlers, nested blocks, and accurate call graphs, while
keeping sqlglot for per-statement SQL analysis and Oracle→Spark transpilation.

## Known limitations

- Regex front end can mis-bound deeply nested blocks; ANTLR fixes this.
- sqlglot transpiles SQL, not procedural PL/SQL — loops/cursors are flagged, not
  converted. `REWRITE_FOR_PYSPARK` output is a skeleton requiring developer work.
- `MERGE` → Spark requires Delta/Iceberg tables at runtime.
- OLTP single-row patterns are correctly steered to KEEP_IN_ORACLE: Spark is
  batch, not a transactional row store.

---

## Canonical graph model (uniform ontology)

`schema.py` is the single source of truth for the graph. It defines every node
label and every relationship type (with allowed source/target labels), so the
graph is uniform no matter which front end produced it (regex now, ANTLR later)
and whether or not a true AST was built.

Node labels: Schema, Package, Procedure, Function, Trigger, Parameter, Variable,
Constant, Cursor, Type, Block, Loop, Branch, ExceptionHandler, SqlStatement,
Table, View, Column, Sequence, External, Feature, plus the knowledge layer
(MigrationAssessment, OracleConstruct, SparkConstruct).

Relationships: DEFINES, CONTAINS, DECLARES, HAS_BLOCK, HANDLES, CALLS,
HAS_FEATURE, EXECUTES, READS, WRITES, USES_VIEW, REFERENCES, USES_CURSOR,
USES_SEQUENCE, BELONGS_TO, and the knowledge layer (ASSESSED_AS, USES_CONSTRUCT,
MAPS_TO).

Decomposition produced per routine:
`Procedure ─DECLARES→ Parameter|Variable|Constant|Cursor`,
`Procedure ─HAS_BLOCK→ Block ─EXECUTES→ SqlStatement ─READS/WRITES→ Table|View`,
`SqlStatement ─REFERENCES→ Column`, `Block ─CONTAINS→ Loop|Branch`,
`Procedure ─HANDLES→ ExceptionHandler`, `Procedure ─CALLS→ Procedure|External`.

Two guarantees:
- `schema.validate_graph(g)` returns `[]` only if the graph uses only defined
  labels/relationships with valid endpoints. The CLI runs this every time.
- `schema.cypher_constraints()` emits a `REQUIRE n.id IS UNIQUE` per label;
  these are prepended to the Cypher output so MERGE stays idempotent.

## Knowledge graph (regeneration + validation)

`knowledge.py` enriches the structural graph so the graph alone carries
everything needed to (a) regenerate a PySpark equivalent and (b) justify the
verdict. It materializes a reusable Oracle→Spark mapping knowledge base as
`(:OracleConstruct)-[:MAPS_TO]->(:SparkConstruct)` with a convertibility tag of
DIRECT, REWRITE, or UNSUPPORTED on each. Routines and statements link to the
constructs they use via `USES_CONSTRUCT`, and each routine gets a
`(:MigrationAssessment)` linked by `ASSESSED_AS`.

The verdict combines the structural scorer (set-based vs Oracle-lock) with the
KB: a routine with any UNSUPPORTED construct (autonomous transaction, COMMIT/
ROLLBACK semantics, savepoints) cannot be a clean conversion regardless of how
set-based it looks. `graph_verdict(g)` reads the decision back out using only
nodes and edges, demonstrating self-containment.

Example queries on the loaded graph:

```cypher
// What must change to move a routine to PySpark
MATCH (p:Procedure {name:'post_invoices'})-[:USES_CONSTRUCT]->(o:OracleConstruct)-[:MAPS_TO]->(s:SparkConstruct)
RETURN o.name, o.convertibility, s.name, o.note;

// Routines blocked from conversion by unsupported constructs
MATCH (p)-[:ASSESSED_AS]->(a:MigrationAssessment)
WHERE a.n_unsupported > 0
RETURN p.name, a.decision, a.blocking;

// Full lineage a regenerator walks: routine -> statement -> spark_sql + tables
MATCH (p:Procedure)-[:HAS_BLOCK]->(:Block)-[:EXECUTES]->(s:SqlStatement)
OPTIONAL MATCH (s)-[:WRITES]->(t:Table)
RETURN p.name, s.kind, s.spark_sql, collect(t.name) AS writes;
```

## Updated CLI

```bash
python -m plsqlmig.cli your_file.sql --schema MYSCHEMA --dry-run \
    --cypher-out out.cypher --pyspark-out out.py
# --no-columns  to skip Column nodes on very large files
```

---

## v2: real AST front end, multi-file corpus, logic-pattern decisions

The default front end is now a hand-written recursive-descent PL/SQL parser
(`plsql_ast.py`) producing a true nested AST, not regex. It handles complex
PL/SQL: packages, **nested procedures (procedure inside procedure)**,
declarations, IF/CASE, FOR/WHILE/basic loops, nested blocks, EXCEPTION
sections, calls, assignments, EXECUTE IMMEDIATE, and embedded SQL.

Run over many files at once; calls resolve across the whole corpus:

```bash
python -m plsqlmig.cli file1.sql file2.sql file3.sql --schema MYSCHEMA \
    --dry-run --cypher-out out.cypher --pyspark-out out.py
# --graph-only   build + validate + knowledge graph, then stop (no PySpark)
# --regex        fall back to the legacy regex front end
```

Cross-file resolution: a call to a routine defined in another file in the
corpus resolves to that routine node; only routines defined nowhere in the
corpus stay `:External`. Add more files and the graph stitches together.

### "Understanding the logic": logic-pattern classification

For each routine the builder walks the AST and classifies the migration-relevant
*shape* of the logic, stored as `logic_pattern` on the node and in the
assessment:

- `SET_BASED_TRANSFORM` — bulk INSERT…SELECT / MERGE / joins+group → CONVERT_TO_PYSPARK
- `ROW_BY_ROW_CURSOR` — cursor loop with per-row DML/calls → REWRITE_FOR_PYSPARK (vectorize)
- `ORCHESTRATION` — mostly calls other routines → becomes the Spark driver / DAG
- `OLTP_BOOKKEEPING` — single-row transactional writes → KEEP_IN_ORACLE
- `PROCEDURAL` — falls back to the structural set-based vs Oracle-lock scores

The decision (`knowledge._decide`) is driven first by the logic pattern, then
overridden to KEEP_IN_ORACLE by *true* blockers only — `autonomous_txn` and
`savepoint`. A plain COMMIT/ROLLBACK is a REWRITE (the Spark write is the
commit), not a blocker.

### Front-end swap remains free

`plsql_ast.py` and the legacy regex parser both feed `ast_builder` /
`graph_builder`, which emit the same `schema.py` node/edge shapes. To reach 100%
Oracle grammar coverage, generate the ANTLR4 PL/SQL parser in an environment
with antlr.org/Maven access and emit the same AST dataclasses from a listener —
the graph model, knowledge layer, and decision logic do not change.

### Honest scope

The hand-written parser covers the common structural/control-flow grammar and
degrades gracefully on constructs it doesn't model (it advances rather than
crashing). It does structural + pattern understanding, not semantic proof: the
verdict is triage, and generated PySpark is a reviewed starting point, not a
guaranteed-equivalent translation.
