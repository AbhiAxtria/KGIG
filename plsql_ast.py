"""
A real (hand-written) PL/SQL parser producing a nested AST.

This replaces the regex front end. It is a recursive-descent parser over a
token stream, so it handles genuine nesting: procedures inside procedures,
blocks inside blocks, loops/ifs containing statements, exception sections.

Scope: the structural and control-flow subset that matters for migration
analysis — packages, (nested) procedures/functions, declarations (vars,
constants, cursors, types, pragmas), IF/CASE, FOR/WHILE/basic loops, nested
BEGIN..END blocks, EXCEPTION handlers, calls, assignments, EXECUTE IMMEDIATE,
and embedded SQL (captured verbatim and handed to sqlglot downstream).

It is NOT the complete Oracle grammar. For 100% grammar coverage, generate the
ANTLR4 PL/SQL parser (antlr/grammars-v4) and emit these same AST dataclasses
from a listener; everything downstream consumes the AST, not the parser.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# ---------------------------------------------------------------- tokenizer

_TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<lcomment>--[^\n]*)
  | (?P<bcomment>/\*.*?\*/)
  | (?P<qstring>q'(?P<qopen>[\[\(\{<]).*?(?P=qopen)?')   # q'[...]'-ish (best effort)
  | (?P<string>'(?:''|[^'])*')
  | (?P<number>\d+\.?\d*)
  | (?P<assign>:=)
  | (?P<concat>\|\|)
  | (?P<arrow>=>)
  | (?P<dotdot>\.\.)
  | (?P<word>"[^"]+"|[A-Za-z][A-Za-z0-9_$#]*)
  | (?P<sym>[(),;.*+\-/<>=:%@])
  | (?P<other>.)
""", re.VERBOSE | re.DOTALL)

KEYWORDS = {
    "CREATE", "OR", "REPLACE", "PACKAGE", "BODY", "PROCEDURE", "FUNCTION",
    "TRIGGER", "IS", "AS", "BEGIN", "END", "DECLARE", "EXCEPTION", "WHEN",
    "THEN", "IF", "ELSIF", "ELSE", "CASE", "LOOP", "FOR", "WHILE", "IN", "OUT",
    "NOCOPY", "RETURN", "CURSOR", "PRAGMA", "TYPE", "SUBTYPE", "CONSTANT",
    "EXECUTE", "IMMEDIATE", "OPEN", "FETCH", "CLOSE", "EXIT", "CONTINUE",
    "GOTO", "COMMIT", "ROLLBACK", "SAVEPOINT", "NULL", "RAISE", "INSERT",
    "UPDATE", "DELETE", "MERGE", "SELECT", "WITH", "DEFAULT",
}


@dataclass
class Tok:
    type: str       # WORD, KW, STRING, NUMBER, SYM, ASSIGN, ...
    value: str
    start: int
    end: int
    kw: str = ""    # uppercased keyword if it is one


def tokenize(text: str) -> List[Tok]:
    toks: List[Tok] = []
    for m in _TOKEN_RE.finditer(text):
        kind = m.lastgroup
        val = m.group()
        if kind in ("ws", "lcomment", "bcomment"):
            continue
        if kind == "word":
            up = val.upper().strip('"')
            if up in KEYWORDS:
                toks.append(Tok("KW", val, m.start(), m.end(), up))
            else:
                toks.append(Tok("WORD", val, m.start(), m.end()))
        elif kind in ("string", "qstring"):
            toks.append(Tok("STRING", val, m.start(), m.end()))
        elif kind == "number":
            toks.append(Tok("NUMBER", val, m.start(), m.end()))
        elif kind == "assign":
            toks.append(Tok("ASSIGN", val, m.start(), m.end()))
        elif kind in ("concat", "arrow", "dotdot"):
            toks.append(Tok("OP", val, m.start(), m.end()))
        elif kind in ("sym", "other"):
            toks.append(Tok("SYM", val, m.start(), m.end()))
    return toks


# ---------------------------------------------------------------- AST nodes

@dataclass
class Param:
    name: str
    mode: str = "IN"
    datatype: str = ""


@dataclass
class Decl:
    name: str
    kind: str            # VARIABLE | CONSTANT | CURSOR | TYPE | PRAGMA
    datatype: str = ""


@dataclass
class Stmt:
    pass


@dataclass
class SqlStmt(Stmt):
    kind: str            # SELECT/INSERT/UPDATE/DELETE/MERGE
    text: str


@dataclass
class ExecImmediate(Stmt):
    text: str
    dynamic_target: Optional[str] = None    # table for truncate/drop/create


@dataclass
class CallStmt(Stmt):
    callee: str
    text: str


@dataclass
class AssignStmt(Stmt):
    target: str
    text: str


@dataclass
class SimpleStmt(Stmt):
    keyword: str         # COMMIT/ROLLBACK/SAVEPOINT/RETURN/RAISE/NULL/EXIT/...
    text: str
    arg: str = ""        # e.g. cursor name for OPEN/FETCH/CLOSE


@dataclass
class IfStmt(Stmt):
    branches: List[Tuple[str, List[Stmt]]] = field(default_factory=list)  # (cond, stmts)


@dataclass
class LoopStmt(Stmt):
    loop_kind: str       # FOR | WHILE | BASIC
    iter_text: str = ""
    cursor_ref: Optional[str] = None
    body: List[Stmt] = field(default_factory=list)


@dataclass
class CaseStmt(Stmt):
    text: str
    body: List[Stmt] = field(default_factory=list)


@dataclass
class ExceptionHandler:
    when: str
    body: List[Stmt] = field(default_factory=list)


@dataclass
class Block(Stmt):
    decls: List = field(default_factory=list)
    body: List[Stmt] = field(default_factory=list)
    handlers: List[ExceptionHandler] = field(default_factory=list)


@dataclass
class Routine:
    kind: str            # PROCEDURE | FUNCTION
    name: str
    params: List[Param] = field(default_factory=list)
    decls: List = field(default_factory=list)        # Decl + nested Routine
    body: List[Stmt] = field(default_factory=list)
    handlers: List[ExceptionHandler] = field(default_factory=list)
    return_type: str = ""
    is_forward: bool = False


@dataclass
class Package:
    name: str
    kind: str            # PACKAGE | PACKAGE BODY
    members: List = field(default_factory=list)      # Routine + Decl


@dataclass
class Trigger:
    name: str
    body: List[Stmt] = field(default_factory=list)


# ---------------------------------------------------------------- parser

_DML_KW = {"INSERT", "UPDATE", "DELETE", "MERGE", "SELECT", "WITH"}
_STOP_AT = {"END", "EXCEPTION", "ELSIF", "ELSE", "WHEN"}


class Parser:
    def __init__(self, text: str):
        self.text = text
        self.toks = tokenize(text)
        self.i = 0

    # -- token helpers
    def peek(self, k=0) -> Optional[Tok]:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else None

    def at_kw(self, *kws) -> bool:
        t = self.peek()
        return bool(t and t.type == "KW" and t.kw in kws)

    def at_sym(self, s) -> bool:
        t = self.peek()
        return bool(t and t.type == "SYM" and t.value == s)

    def next(self) -> Optional[Tok]:
        t = self.peek()
        if t is not None:
            self.i += 1
        return t

    def eat_kw(self, *kws):
        if self.at_kw(*kws):
            return self.next()
        return None

    def slice(self, start_tok: Tok, end_tok: Tok) -> str:
        return self.text[start_tok.start:end_tok.end]

    def skip_to_semicolon(self) -> Optional[Tok]:
        """Advance to and consume the next top-level ';'. Returns the ';' token."""
        depth = 0
        last = None
        while self.i < len(self.toks):
            t = self.next()
            last = t
            if t.type == "SYM" and t.value == "(":
                depth += 1
            elif t.type == "SYM" and t.value == ")":
                depth = max(0, depth - 1)
            elif t.type == "SYM" and t.value == ";" and depth == 0:
                return t
        return last

    # -- top level
    def parse_program(self) -> List:
        out = []
        guard = 0
        while self.i < len(self.toks):
            guard += 1
            if guard > 10_000_000:
                break
            start_i = self.i
            node = self.parse_top()
            if node is not None:
                out.append(node)
            if self.i == start_i:        # safety: always advance
                self.i += 1
        return out

    def parse_top(self):
        # optional CREATE [OR REPLACE]
        if self.at_kw("CREATE"):
            self.next()
            if self.at_kw("OR"):
                self.next()
                self.eat_kw("REPLACE")
        if self.at_kw("PACKAGE"):
            self.next()
            kind = "PACKAGE"
            if self.at_kw("BODY"):
                self.next()
                kind = "PACKAGE BODY"
            return self.parse_package(kind)
        if self.at_kw("PROCEDURE", "FUNCTION"):
            return self.parse_routine()
        if self.at_kw("TRIGGER"):
            return self.parse_trigger()
        if self.at_kw("DECLARE", "BEGIN"):
            return self.parse_block()
        return None

    def parse_name(self) -> str:
        parts = []
        t = self.peek()
        while t and (t.type == "WORD" or (t.type == "KW" and t.kw not in
                     ("IS", "AS", "BEGIN", "RETURN"))):
            parts.append(t.value.strip('"'))
            self.next()
            if self.at_sym("."):
                self.next()
                t = self.peek()
                continue
            break
        return ".".join(parts) if parts else ""

    def parse_package(self, kind: str) -> Package:
        name = self.parse_name()
        self.eat_kw("IS", "AS")
        pkg = Package(name=name, kind=kind)
        guard = 0
        while self.i < len(self.toks) and not self.at_kw("END"):
            guard += 1
            if guard > 1_000_000:
                break
            before = self.i
            if self.at_kw("PROCEDURE", "FUNCTION"):
                pkg.members.append(self.parse_routine())
            elif self.at_kw("CURSOR", "TYPE", "SUBTYPE", "PRAGMA"):
                pkg.members.append(self.parse_decl())
            elif self.peek() and self.peek().type == "WORD":
                pkg.members.append(self.parse_decl())
            else:
                self.next()
            if self.i == before:
                self.next()
        self.eat_kw("END")
        self.skip_to_semicolon()
        return pkg

    def parse_routine(self) -> Routine:
        kind = self.next().kw            # PROCEDURE | FUNCTION
        name = self.parse_name()
        r = Routine(kind=kind, name=name)
        if self.at_sym("("):
            r.params = self.parse_params()
        if kind == "FUNCTION" and self.at_kw("RETURN"):
            self.next()
            rt = []
            while self.peek() and not self.at_kw("IS", "AS") and not self.at_sym(";"):
                rt.append(self.next().value)
            r.return_type = " ".join(rt)
        # forward declaration (no body)
        if self.at_sym(";"):
            self.next()
            r.is_forward = True
            return r
        self.eat_kw("IS", "AS")
        r.decls = self.parse_declare_section()
        self.eat_kw("BEGIN")
        r.body = self.parse_statements()
        if self.at_kw("EXCEPTION"):
            self.next()
            r.handlers = self.parse_handlers()
        self.eat_kw("END")
        # optional trailing name
        if self.peek() and self.peek().type in ("WORD", "KW") and not self.at_sym(";"):
            self.parse_name()
        self.skip_to_semicolon()
        return r

    def parse_params(self) -> List[Param]:
        self.next()  # (
        depth = 1
        start = self.i
        toks: List[Tok] = []
        while self.i < len(self.toks) and depth > 0:
            t = self.next()
            if t.type == "SYM" and t.value == "(":
                depth += 1
            elif t.type == "SYM" and t.value == ")":
                depth -= 1
                if depth == 0:
                    break
            toks.append(t)
        # split on top-level commas
        params: List[Param] = []
        cur: List[Tok] = []
        d = 0
        for t in toks:
            if t.type == "SYM" and t.value == "(":
                d += 1
            elif t.type == "SYM" and t.value == ")":
                d -= 1
            if t.type == "SYM" and t.value == "," and d == 0:
                params.append(self._mk_param(cur)); cur = []
            else:
                cur.append(t)
        if cur:
            params.append(self._mk_param(cur))
        return [p for p in params if p]

    def _mk_param(self, toks: List[Tok]) -> Optional[Param]:
        if not toks:
            return None
        name = toks[0].value.strip('"')
        mode = "IN"
        idx = 1
        if idx < len(toks) and toks[idx].type == "KW" and toks[idx].kw == "IN":
            mode = "IN"; idx += 1
            if idx < len(toks) and toks[idx].type == "KW" and toks[idx].kw == "OUT":
                mode = "IN OUT"; idx += 1
        elif idx < len(toks) and toks[idx].type == "KW" and toks[idx].kw == "OUT":
            mode = "OUT"; idx += 1
        dt = " ".join(t.value for t in toks[idx:])
        dt = dt.split(":=")[0].split("DEFAULT")[0].strip()
        return Param(name=name, mode=mode, datatype=dt)

    def parse_declare_section(self) -> List:
        decls: List = []
        guard = 0
        while self.i < len(self.toks) and not self.at_kw("BEGIN"):
            guard += 1
            if guard > 1_000_000:
                break
            before = self.i
            if self.at_kw("PROCEDURE", "FUNCTION"):
                decls.append(self.parse_routine())          # nested routine
            elif self.at_kw("CURSOR", "TYPE", "SUBTYPE", "PRAGMA"):
                decls.append(self.parse_decl())
            elif self.peek() and self.peek().type == "WORD":
                decls.append(self.parse_decl())
            else:
                self.next()
            if self.i == before:
                self.next()
        return [d for d in decls if d is not None]

    def parse_decl(self):
        t = self.peek()
        if t.type == "KW" and t.kw == "CURSOR":
            self.next()
            name = self.parse_name()
            self.skip_to_semicolon()
            return Decl(name=name, kind="CURSOR")
        if t.type == "KW" and t.kw in ("TYPE", "SUBTYPE"):
            self.next()
            name = self.parse_name()
            self.skip_to_semicolon()
            return Decl(name=name, kind="TYPE")
        if t.type == "KW" and t.kw == "PRAGMA":
            self.next()
            name = self.parse_name()
            self.skip_to_semicolon()
            return Decl(name=name or "PRAGMA", kind="PRAGMA")
        # variable or constant: name [CONSTANT] type [:= expr] ;
        name = self.next().value.strip('"')
        kind = "VARIABLE"
        if self.at_kw("CONSTANT"):
            self.next()
            kind = "CONSTANT"
        dt_toks = []
        while self.peek() and not self.at_sym(";") and self.peek().type != "ASSIGN":
            dt_toks.append(self.next().value)
        datatype = " ".join(dt_toks).strip()
        self.skip_to_semicolon()
        return Decl(name=name, kind=kind, datatype=datatype)

    def parse_statements(self) -> List[Stmt]:
        stmts: List[Stmt] = []
        guard = 0
        while self.i < len(self.toks):
            guard += 1
            if guard > 5_000_000:
                break
            if self.at_kw(*_STOP_AT):
                break
            before = self.i
            s = self.parse_statement()
            if s is not None:
                stmts.append(s)
            if self.i == before:
                self.next()
        return stmts

    def parse_statement(self) -> Optional[Stmt]:
        t = self.peek()
        if t is None:
            return None
        if t.type == "KW":
            kw = t.kw
            if kw == "IF":
                return self.parse_if()
            if kw == "FOR":
                return self.parse_for()
            if kw == "WHILE":
                return self.parse_while()
            if kw == "LOOP":
                return self.parse_basic_loop()
            if kw == "CASE":
                return self.parse_case()
            if kw in ("DECLARE", "BEGIN"):
                return self.parse_block()
            if kw == "EXECUTE":
                return self.parse_exec_immediate()
            if kw in ("OPEN", "FETCH", "CLOSE"):
                start = self.next()
                arg = self.parse_name()
                end = self.skip_to_semicolon() or start
                return SimpleStmt(keyword=kw, text=self.slice(start, end), arg=arg)
            if kw in ("COMMIT", "ROLLBACK", "SAVEPOINT", "RETURN", "RAISE",
                      "NULL", "EXIT", "CONTINUE", "GOTO"):
                start = t
                end = self.skip_to_semicolon() or start
                return SimpleStmt(keyword=kw, text=self.slice(start, end))
            if kw in _DML_KW:
                start = t
                end = self.skip_to_semicolon() or start
                return SqlStmt(kind=("SELECT" if kw == "WITH" else kw),
                               text=self.slice(start, end).rstrip(";").strip())
        # identifier: assignment or call
        if t.type == "WORD":
            start = t
            # look ahead to decide assignment vs call (stop at top-level ;)
            j = self.i
            depth = 0
            is_assign = False
            while j < len(self.toks):
                tj = self.toks[j]
                if tj.type == "SYM" and tj.value == "(":
                    depth += 1
                elif tj.type == "SYM" and tj.value == ")":
                    depth -= 1
                elif tj.type == "ASSIGN" and depth == 0:
                    is_assign = True; break
                elif tj.type == "SYM" and tj.value == ";" and depth == 0:
                    break
                j += 1
            callee = self.parse_name()
            end = self.skip_to_semicolon() or start
            text = self.slice(start, end)
            if is_assign:
                return AssignStmt(target=callee, text=text)
            return CallStmt(callee=callee, text=text)
        # anything else: skip a token
        self.next()
        return None

    def parse_if(self) -> IfStmt:
        node = IfStmt()
        self.next()  # IF
        cond = self._collect_until_kw("THEN")
        self.eat_kw("THEN")
        node.branches.append((cond, self.parse_statements()))
        while self.at_kw("ELSIF"):
            self.next()
            c = self._collect_until_kw("THEN")
            self.eat_kw("THEN")
            node.branches.append((c, self.parse_statements()))
        if self.at_kw("ELSE"):
            self.next()
            node.branches.append(("ELSE", self.parse_statements()))
        self.eat_kw("END")           # END IF
        self.skip_to_semicolon()
        return node

    def parse_for(self) -> LoopStmt:
        self.next()  # FOR
        loop = LoopStmt(loop_kind="FOR")
        self.parse_name()            # loop variable
        self.eat_kw("IN")
        iter_toks = []
        cur = None
        while self.peek() and not self.at_kw("LOOP"):
            tk = self.next()
            iter_toks.append(tk.value)
            if cur is None and tk.type == "WORD":
                cur = tk.value
        loop.iter_text = " ".join(iter_toks)
        loop.cursor_ref = cur
        self.eat_kw("LOOP")
        loop.body = self.parse_statements()
        self.eat_kw("END")           # END LOOP
        self.skip_to_semicolon()
        return loop

    def parse_while(self) -> LoopStmt:
        self.next()  # WHILE
        loop = LoopStmt(loop_kind="WHILE")
        loop.iter_text = self._collect_until_kw("LOOP")
        self.eat_kw("LOOP")
        loop.body = self.parse_statements()
        self.eat_kw("END")
        self.skip_to_semicolon()
        return loop

    def parse_basic_loop(self) -> LoopStmt:
        self.next()  # LOOP
        loop = LoopStmt(loop_kind="BASIC")
        loop.body = self.parse_statements()
        self.eat_kw("END")
        self.skip_to_semicolon()
        return loop

    def parse_case(self) -> CaseStmt:
        start = self.next()  # CASE
        # consume until matching END CASE ;
        depth = 1
        last = start
        while self.i < len(self.toks) and depth > 0:
            if self.at_kw("CASE"):
                depth += 1; self.next(); continue
            if self.at_kw("END"):
                self.next()
                if self.at_kw("CASE"):
                    self.next()
                depth -= 1
                last = self.skip_to_semicolon() or last
                break
            last = self.next()
        return CaseStmt(text=self.slice(start, last))

    def parse_block(self) -> Block:
        blk = Block()
        if self.at_kw("DECLARE"):
            self.next()
            blk.decls = self.parse_declare_section()
        self.eat_kw("BEGIN")
        blk.body = self.parse_statements()
        if self.at_kw("EXCEPTION"):
            self.next()
            blk.handlers = self.parse_handlers()
        self.eat_kw("END")
        self.skip_to_semicolon()
        return blk

    def parse_handlers(self) -> List[ExceptionHandler]:
        handlers: List[ExceptionHandler] = []
        while self.at_kw("WHEN"):
            self.next()
            when = self._collect_until_kw("THEN")
            self.eat_kw("THEN")
            handlers.append(ExceptionHandler(when=when, body=self.parse_statements()))
        return handlers

    def parse_exec_immediate(self) -> ExecImmediate:
        start = self.next()  # EXECUTE
        self.eat_kw("IMMEDIATE")
        end = self.skip_to_semicolon() or start
        text = self.slice(start, end)
        target = None
        m = re.search(r"(?is)'?\s*(?:truncate|drop|create)\s+table\s+([\w\.]+)", text)
        if m:
            target = m.group(1).lower()
        return ExecImmediate(text=text, dynamic_target=target)

    def _collect_until_kw(self, kw: str) -> str:
        parts = []
        while self.peek() and not self.at_kw(kw) and not self.at_sym(";"):
            parts.append(self.next().value)
        return " ".join(parts)


def parse_program(text: str) -> List:
    return Parser(text).parse_program()
