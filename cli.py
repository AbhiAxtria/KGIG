"""
CLI: parse a multi-file PL/SQL corpus with a real AST front end, build the
canonical knowledge graph, validate it, store it in Neo4j, and decide
convert-to-PySpark vs keep-in-Oracle per routine (logic-pattern driven).

  python -m plsqlmig.cli FILE [FILE ...] [options]

Options:
  --schema NAME        schema/owner name for the Schema root node
  --regex              use the legacy regex front end instead of the AST parser
  --graph-only         build + validate + knowledge graph, then stop (no PySpark)
  --neo4j-uri/user/pass   live Neo4j ingest
  --dry-run            emit Cypher instead of connecting
  --cypher-out PATH    write Cypher (constraints + MERGE) here
  --pyspark-out PATH   write generated PySpark here
  --no-columns         skip Column nodes (smaller graph)
"""
from __future__ import annotations

import argparse
import json
import sys

from . import (score_graph, generate_pyspark, ingest,
               enrich_knowledge_graph, graph_verdict, schema)
from .ast_builder import parse_corpus, build_graph_from_corpus


def _build_regex(paths, schema_name, include_columns):
    from .plsql_structure import parse_source
    from .graph_builder import build_graph
    objs = []
    for p in paths:
        objs.extend(parse_source(open(p).read()))
    return build_graph(objs, schema_name=schema_name, include_columns=include_columns)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="plsqlmig")
    p.add_argument("sql_files", nargs="+")
    p.add_argument("--schema", default=None)
    p.add_argument("--regex", action="store_true")
    p.add_argument("--graph-only", action="store_true")
    p.add_argument("--neo4j-uri", default="bolt://localhost:7687")
    p.add_argument("--neo4j-user", default="neo4j")
    p.add_argument("--neo4j-pass", default="neo4j")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--cypher-out", default=None)
    p.add_argument("--pyspark-out", default=None)
    p.add_argument("--no-columns", action="store_true")
    args = p.parse_args(argv)

    cols = not args.no_columns
    print(f"Corpus: {len(args.sql_files)} file(s)  front-end: "
          f"{'regex' if args.regex else 'AST'}")

    if args.regex:
        g = _build_regex(args.sql_files, args.schema, cols)
    else:
        corpus = parse_corpus(args.sql_files)
        for path, nodes in corpus:
            print(f"  {path}: {len(nodes)} top-level object(s)")
        g = build_graph_from_corpus(corpus, schema_name=args.schema,
                                    include_columns=cols)

    print(f"\nCanonical graph: {json.dumps(g.stats())}")
    violations = schema.validate_graph(g)
    print(f"Schema conformance: {'OK' if not violations else violations}")

    scores = score_graph(g)
    enrich_knowledge_graph(g, scores)
    print(f"Knowledge graph:  {json.dumps(g.stats())}")

    print("\nMigration verdict (logic-pattern driven, read back from the graph)")
    print("-" * 76)
    order = {"CONVERT_TO_PYSPARK": 0, "REWRITE_FOR_PYSPARK": 1,
             "ORCHESTRATION": 2, "KEEP_IN_ORACLE": 3}
    for v in sorted(graph_verdict(g), key=lambda x: order.get(x["decision"], 9)):
        owner = f"{v['owner']}." if v["owner"] else ""
        print(f"  {owner}{v['routine']}")
        print(f"     logic={v['logic_pattern']}  ->  {v['decision']}  "
              f"(confidence {v['confidence']})")
        if v["blocking"]:
            print(f"     blocked by: {v['blocking']}")

    summary = {}
    for v in graph_verdict(g):
        summary[v["decision"]] = summary.get(v["decision"], 0) + 1
    print(f"\nDecision summary: {json.dumps(summary)}")

    ingest(g, args.neo4j_uri, args.neo4j_user, args.neo4j_pass,
           dry_run=args.dry_run, out_path=args.cypher_out)

    if args.graph_only:
        print("\n--graph-only: stopped after building the knowledge graph.")
        return 0

    pyspark = generate_pyspark(g, scores)
    if args.pyspark_out:
        with open(args.pyspark_out, "w") as f:
            f.write(pyspark)
        print(f"\nWrote generated PySpark to {args.pyspark_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
