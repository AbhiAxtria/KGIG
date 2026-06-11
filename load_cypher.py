"""Load a plsqlmig-generated .cypher file into Neo4j."""
import argparse, sys
from neo4j import GraphDatabase


def split_statements(text):
    stmts, buf, in_str, i, n = [], [], False, 0, len(text)
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\" and i + 1 < n:
                buf.append(ch); buf.append(text[i + 1]); i += 2; continue
            if ch == "'":
                in_str = False
            buf.append(ch)
        else:
            if ch == "'":
                in_str = True; buf.append(ch)
            elif ch == ";":
                s = "".join(buf).strip()
                if s: stmts.append(s)
                buf = []
            else:
                buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail: stmts.append(tail)
    return stmts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cypher_file")
    ap.add_argument("--uri", default="bolt://localhost:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="neo4j")
    ap.add_argument("--database", default=None)
    ap.add_argument("--wipe", action="store_true")
    args = ap.parse_args()

    text = open(args.cypher_file, encoding="utf-8").read()
    statements = split_statements(text)
    constraints = [s for s in statements if s.upper().startswith("CREATE CONSTRAINT")]
    data = [s for s in statements if not s.upper().startswith("CREATE CONSTRAINT")]
    print(f"{len(constraints)} constraint(s), {len(data)} data statement(s)")

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    try:
        driver.verify_connectivity()
    except Exception as e:
        print(f"ERROR connecting to {args.uri}: {e}")
        driver.close(); sys.exit(2)

    kw = {"database": args.database} if args.database else {}
    with driver.session(**kw) as session:
        if args.wipe:
            session.run("MATCH (n) DETACH DELETE n")
        for c in constraints:
            try: session.run(c)
            except Exception as e: print(f"  constraint skipped: {e.__class__.__name__}")
        for i in range(0, len(data), 500):
            with session.begin_transaction() as tx:
                for s in data[i:i + 500]:
                    tx.run(s)
                tx.commit()
            print(f"  loaded {min(i + 500, len(data))}/{len(data))}")
    driver.close()
    print("Done.")


if __name__ == "__main__":
    main()