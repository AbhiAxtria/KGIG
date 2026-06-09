"""
Build the canonical graph from the real AST, across a multi-file corpus.

- Handles nested procedures (Procedure CONTAINS Procedure).
- Resolves calls across all files in the corpus; unresolved targets become
  :External (e.g. a routine defined in a file you didn't include).
- Classifies each routine's *logic pattern* (the migration-relevant shape of the
  logic) from the AST: SET_BASED_TRANSFORM, ROW_BY_ROW_CURSOR, ORCHESTRATION,
  OLTP_BOOKKEEPING, or PROCEDURAL.
"""
from __future__ import annotations

import re
from typing import List, Dict, Tuple, Optional

from . import plsql_ast as ast
from .plsql_ast import (Routine, Package, Decl, Block, LoopStmt, IfStmt, CaseStmt,
                        SqlStmt, ExecImmediate, CallStmt, AssignStmt, SimpleStmt)
from .sql_analyzer import analyze_statement
from .graph_model import Graph, nid

_VIEW_HINT = re.compile(r"(?i)(^v_|^vw_|_v$|_vw$|_view$)")
_BUILTIN_CALLS = {"dbms_output", "dbms_stats", "raise_application_error"}


def parse_corpus(paths: List[str]) -> List[Tuple[str, list]]:
    out = []
    for p in paths:
        with open(p) as f:
            out.append((p, ast.parse_program(f.read())))
    return out


def _is_view(name: str, catalog: Optional[dict]) -> bool:
    if catalog and name.lower() in {v.lower() for v in catalog.get("views", [])}:
        return True
    return bool(_VIEW_HINT.search(name))


class _Builder:
    def __init__(self, catalog, include_columns):
        self.g = Graph()
        self.catalog = catalog
        self.include_columns = include_columns
        self.registry: Dict[str, str] = {}          # name(lower) -> routine node id
        self.pending_calls: List[Tuple[str, List[str]]] = []

    def relation(self, name: str):
        label = "View" if _is_view(name, self.catalog) else "Table"
        rid = nid(label.lower(), name)
        self.g.add_node(rid, label, name=name)
        return rid, label

    # ---- routine ----
    def routine(self, r: Routine, parent_id: Optional[str], qual: str, schema_id):
        if r.is_forward:
            return
        rid = nid("routine", qual)
        label = "Function" if r.kind == "FUNCTION" else "Procedure"
        owner = qual.rsplit(".", 1)[0] if "." in qual else ""
        self.g.add_node(rid, label, name=r.name, owner=owner, qualified=qual)
        self.registry.setdefault(r.name.lower(), rid)
        self.registry.setdefault(qual.lower(), rid)
        if parent_id:
            self.g.add_edge(parent_id, "CONTAINS", rid)
        elif schema_id:
            self.g.add_edge(schema_id, "DEFINES", rid)

        # declarations
        pragmas = []
        for p in r.params:
            sid = nid(rid, "param", p.name)
            self.g.add_node(sid, "Parameter", name=p.name, datatype=p.datatype, mode=p.mode)
            self.g.add_edge(rid, "DECLARES", sid)
        for d in r.decls:
            if isinstance(d, Routine):
                continue
            lbl = {"VARIABLE": "Variable", "CONSTANT": "Constant",
                   "CURSOR": "Cursor", "TYPE": "Type", "PRAGMA": None}[d.kind]
            if d.kind == "PRAGMA":
                pragmas.append(d.name.upper())
                continue
            sid = nid(rid, "decl", d.kind, d.name)
            self.g.add_node(sid, lbl, name=d.name, datatype=d.datatype)
            self.g.add_edge(rid, "DECLARES", sid)
            if d.kind == "CURSOR":
                self.g.add_edge(rid, "USES_CURSOR", sid)

        # body block
        block_id = nid(rid, "block")
        self.g.add_node(block_id, "Block", name=f"{r.name}_body")
        self.g.add_edge(rid, "HAS_BLOCK", block_id)

        feats: Dict[str, int] = {}
        calls: List[str] = []
        # cursors declared -> explicit_cursor feature
        n_cursors = sum(1 for d in r.decls if isinstance(d, Decl) and d.kind == "CURSOR")
        if n_cursors:
            feats["explicit_cursor"] = n_cursors
        if any(pr.startswith("AUTONOMOUS") for pr in pragmas):
            feats["autonomous_txn"] = 1
        if any(isinstance(d, Routine) for d in r.decls):
            feats["nested_procedure"] = sum(1 for d in r.decls if isinstance(d, Routine))

        self._walk(r.body, block_id, feats, calls, in_loop=False)
        if r.handlers:
            feats["exception_handler"] = len(r.handlers)
            eh_id = nid(rid, "exc")
            self.g.add_node(eh_id, "ExceptionHandler",
                            name="; ".join(h.when for h in r.handlers)[:60])
            self.g.add_edge(rid, "HANDLES", eh_id)
            for h in r.handlers:
                self._walk(h.body, block_id, feats, calls, in_loop=False)

        for feat, cnt in feats.items():
            fid = nid("feature", feat)
            self.g.add_node(fid, "Feature", name=feat)
            self.g.add_edge(rid, "HAS_FEATURE", fid, count=cnt)

        # logic pattern (the "understanding")
        self.g.nodes[rid].props["logic_pattern"] = self._classify(r, feats, calls)

        self.pending_calls.append((rid, calls))

        # nested routines
        for d in r.decls:
            if isinstance(d, Routine):
                self.routine(d, rid, f"{qual}.{d.name}", schema_id)

    # ---- statement walk ----
    def _walk(self, stmts, block_id, feats, calls, in_loop):
        for s in stmts:
            if isinstance(s, SqlStmt):
                self._sql(s, block_id, feats)
            elif isinstance(s, ExecImmediate):
                feats["execute_immediate"] = feats.get("execute_immediate", 0) + 1
                if s.dynamic_target:
                    tid, _ = self.relation(s.dynamic_target)
                    st_id = nid(block_id, "dyn", s.dynamic_target)
                    self.g.add_node(st_id, "SqlStatement", kind="TRUNCATE",
                                    dynamic=True, text=s.text[:200])
                    self.g.add_edge(block_id, "EXECUTES", st_id)
                    self.g.add_edge(st_id, "WRITES", tid)
            elif isinstance(s, CallStmt):
                base = s.callee.split(".")[0].lower()
                if base in _BUILTIN_CALLS or s.callee.lower().startswith("dbms_"):
                    feats["dbms_pkg_call"] = feats.get("dbms_pkg_call", 0) + 1
                elif s.callee.lower().startswith("utl_"):
                    feats["utl_pkg_call"] = feats.get("utl_pkg_call", 0) + 1
                else:
                    calls.append(s.callee)
            elif isinstance(s, AssignStmt):
                if ".nextval" in s.text.lower():
                    feats["sequence_nextval"] = feats.get("sequence_nextval", 0) + 1
            elif isinstance(s, SimpleStmt):
                kw = s.keyword.lower()
                if kw in ("commit", "rollback", "savepoint"):
                    feats[kw] = feats.get(kw, 0) + 1
                if kw == "raise" and "raise_application_error" in s.text.lower():
                    feats["raise_app_error"] = feats.get("raise_app_error", 0) + 1
            elif isinstance(s, LoopStmt):
                lk = {"FOR": "cursor_for_loop" if s.cursor_ref or "select" in s.iter_text.lower()
                      else "for_loop", "WHILE": "while_loop", "BASIC": "basic_loop"}[s.loop_kind]
                feats[lk] = feats.get(lk, 0) + 1
                loop_id = nid(block_id, "loop", lk, str(len(self.g.edges)))
                self.g.add_node(loop_id, "Loop", name=lk, iter=s.iter_text[:80])
                self.g.add_edge(block_id, "CONTAINS", loop_id)
                self._walk(s.body, block_id, feats, calls, in_loop=True)
            elif isinstance(s, IfStmt):
                feats["if_branch"] = feats.get("if_branch", 0) + 1
                br_id = nid(block_id, "branch", str(len(self.g.edges)))
                self.g.add_node(br_id, "Branch", name="IF")
                self.g.add_edge(block_id, "CONTAINS", br_id)
                for _cond, body in s.branches:
                    self._walk(body, block_id, feats, calls, in_loop)
            elif isinstance(s, CaseStmt):
                feats["if_branch"] = feats.get("if_branch", 0) + 1
            elif isinstance(s, Block):
                self._walk(s.body, block_id, feats, calls, in_loop)
                for h in s.handlers:
                    feats["exception_handler"] = feats.get("exception_handler", 0) + 1
                    self._walk(h.body, block_id, feats, calls, in_loop)

    def _sql(self, s: SqlStmt, block_id, feats):
        a = analyze_statement(s.text)
        st_id = nid(block_id, "stmt", str(len(self.g.edges)))
        self.g.add_node(
            st_id, "SqlStatement", kind=a.kind, joins=a.joins, aggregates=a.aggregates,
            window_funcs=a.window_funcs, has_group_by=a.has_group_by,
            oracle_specific=",".join(a.oracle_specific), parse_ok=a.parse_ok,
            spark_sql=a.spark_sql or "", text=s.text[:500],
        )
        self.g.add_edge(block_id, "EXECUTES", st_id)
        for t in a.reads:
            rid, lbl = self.relation(t)
            self.g.add_edge(st_id, "READS", rid)
            if lbl == "View":
                self.g.add_edge(st_id, "USES_VIEW", rid)
        for t in a.writes:
            tid, _ = self.relation(t)
            self.g.add_edge(st_id, "WRITES", tid)
        if self.include_columns:
            for col in a.columns:
                cid = nid(st_id, "col", col)
                self.g.add_node(cid, "Column", name=col)
                self.g.add_edge(st_id, "REFERENCES", cid)

    def _classify(self, r: Routine, feats: Dict[str, int], calls: List[str]) -> str:
        row_loop = any(k in feats for k in ("cursor_for_loop", "while_loop", "basic_loop"))
        # does a loop body contain DML or calls? (row-by-row)
        row_dml = row_loop and self._loop_has_work(r.body)
        set_based = self._has_set_based(r.body)
        own_dml = self._has_any_dml(r.body)
        if row_dml:
            return "ROW_BY_ROW_CURSOR"
        if set_based:
            return "SET_BASED_TRANSFORM"
        if len(calls) >= 2 and not own_dml:
            return "ORCHESTRATION"
        if own_dml and not row_loop and len(calls) == 0:
            return "OLTP_BOOKKEEPING"
        return "PROCEDURAL"

    def _loop_has_work(self, stmts) -> bool:
        for s in stmts:
            if isinstance(s, LoopStmt):
                if self._has_any_dml(s.body) or any(isinstance(x, CallStmt) for x in s.body):
                    return True
                if self._loop_has_work(s.body):
                    return True
            elif isinstance(s, IfStmt):
                for _c, b in s.branches:
                    if self._loop_has_work(b):
                        return True
        return False

    def _has_any_dml(self, stmts) -> bool:
        for s in stmts:
            if isinstance(s, (SqlStmt, ExecImmediate)):
                return True
            if isinstance(s, LoopStmt) and self._has_any_dml(s.body):
                return True
            if isinstance(s, IfStmt):
                for _c, b in s.branches:
                    if self._has_any_dml(b):
                        return True
            if isinstance(s, Block) and self._has_any_dml(s.body):
                return True
        return False

    def _has_set_based(self, stmts) -> bool:
        for s in stmts:
            if isinstance(s, SqlStmt):
                a = analyze_statement(s.text)
                if s.kind == "MERGE" or a.joins or a.has_group_by or \
                   (s.kind == "INSERT" and "select" in s.text.lower()):
                    return True
            if isinstance(s, Block) and self._has_set_based(s.body):
                return True
        return False

    def finish(self):
        for caller_id, names in self.pending_calls:
            for name in names:
                target = self.registry.get(name.lower())
                if target is None:
                    target = nid("external", name.lower())
                    self.g.add_node(target, "External", name=name)
                self.g.add_edge(caller_id, "CALLS", target)
        return self.g


def build_graph_from_corpus(corpus, schema_name=None, catalog=None,
                            include_columns=True) -> Graph:
    b = _Builder(catalog, include_columns)
    schema_id = None
    if schema_name:
        schema_id = nid("schema", schema_name)
        b.g.add_node(schema_id, "Schema", name=schema_name)

    for _path, nodes in corpus:
        for node in nodes:
            if isinstance(node, Package):
                pkg_id = nid("pkg", node.name)
                b.g.add_node(pkg_id, "Package", name=node.name, plsql_kind=node.kind)
                if schema_id:
                    b.g.add_edge(schema_id, "DEFINES", pkg_id)
                for m in node.members:
                    if isinstance(m, Routine):
                        b.routine(m, pkg_id, f"{node.name}.{m.name}", schema_id)
                    elif isinstance(m, Decl):
                        sid = nid(pkg_id, "decl", m.kind, m.name)
                        lbl = {"VARIABLE": "Variable", "CONSTANT": "Constant",
                               "CURSOR": "Cursor", "TYPE": "Type"}.get(m.kind)
                        if lbl:
                            b.g.add_node(sid, lbl, name=m.name, datatype=m.datatype)
                            b.g.add_edge(pkg_id, "DECLARES", sid)
            elif isinstance(node, Routine):
                b.routine(node, None, node.name, schema_id)
            elif isinstance(node, ast.Trigger):
                tid = nid("trigger", node.name)
                b.g.add_node(tid, "Trigger", name=node.name)
                if schema_id:
                    b.g.add_edge(schema_id, "DEFINES", tid)

    return b.finish()
