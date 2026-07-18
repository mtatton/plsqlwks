#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import struct
import textwrap
from collections import Counter
from pathlib import Path

TOKEN_RE = re.compile(r"[^\W\d]\w*", re.UNICODE)


def main() -> None:
    parser = argparse.ArgumentParser(description="Search a local vector database.")
    parser.add_argument("query", help="Query text.")
    parser.add_argument("--db", default="vdb/python_chunks.sqlite", help="SQLite vector database path.")
    parser.add_argument(
        "--collection",
        choices=("chunks", "symbols", "documentation"),
        help="Search only one chunk collection within the selected database.",
    )
    parser.add_argument("--limit", type=int, default=5, help="Number of results.")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"Database does not exist: {db_path}")

    uri = f"{db_path.resolve().as_uri()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        dims = int(metadata(conn).get("dimensions", "384"))
        query_vector = embed_text(args.query, dims)
        sql = """
            select c.id, c.file_path, c.kind, c.symbol, c.start_line, c.end_line, c.text, v.embedding
            from chunks c
            join vectors v on v.chunk_id = c.id
            """
        parameters: tuple[str, ...] = ()
        if args.collection is not None:
            sql += " where c.collection = ?"
            parameters = (args.collection,)
        rows = conn.execute(sql, parameters).fetchall()
    finally:
        conn.close()

    scored = []
    for row in rows:
        vector = unpack_vector(row[7], dims)
        score = dot(query_vector, vector)
        scored.append((score, row))
    scored.sort(reverse=True, key=lambda item: item[0])

    for score, row in scored[: args.limit]:
        _, file_path, kind, symbol, start_line, end_line, text, _ = row
        print(f"{score:.3f}  {file_path}:{start_line}-{end_line}  {kind}  {symbol}")
        print(indent(snippet(text)))
        print()


def metadata(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute("select key, value from metadata").fetchall())


def embed_text(text: str, dims: int) -> list[float]:
    tokens: list[str] = []
    for match in TOKEN_RE.finditer(text):
        tokens.extend(split_identifier(match.group(0)))
    return embed_tokens([token for token in tokens if len(token) > 1], dims)


def split_identifier(value: str) -> list[str]:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    value = value.replace("__", "_")
    return [part.lower() for part in re.split(r"_+|[^\w]+", value, flags=re.UNICODE) if part]


def embed_tokens(tokens: list[str], dims: int) -> list[float]:
    counts = Counter(tokens)
    vector = [0.0] * dims
    for token, count in counts.items():
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "little", signed=False)
        index = value % dims
        sign = -1.0 if value & (1 << 63) else 1.0
        vector[index] += sign * (1.0 + count.bit_length())
    norm = sum(item * item for item in vector) ** 0.5
    if norm:
        vector = [item / norm for item in vector]
    return vector


def unpack_vector(blob: bytes, dims: int) -> tuple[float, ...]:
    return struct.unpack(f"<{dims}f", blob)


def dot(left: list[float], right: tuple[float, ...]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def snippet(text: str) -> str:
    compact = " ".join(line.strip() for line in text.splitlines() if line.strip())
    return textwrap.shorten(compact, width=220, placeholder="...")


def indent(text: str) -> str:
    return textwrap.indent(text, "  ")


if __name__ == "__main__":
    main()
