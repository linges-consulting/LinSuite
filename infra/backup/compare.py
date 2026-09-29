"""Compare the original and restored databases: every table's row count, and one document's
ciphertext + digest byte-for-byte (rehearse.sh, #89). Exits non-zero — with the mismatch
printed to stderr — on any difference. `asyncpg` is a main backend dependency already, so this
needs nothing extra installed.

Usage: uv run python compare.py <original DSN> <restored DSN> <document id>
DSNs are plain `postgresql://...` (asyncpg's own scheme, not SQLAlchemy's `+asyncpg`).
"""

import asyncio
import sys

import asyncpg


async def _table_counts(dsn: str) -> dict[str, int]:
    conn = await asyncpg.connect(dsn)
    try:
        tables = [
            row["tablename"]
            for row in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1"
            )
        ]
        return {t: await conn.fetchval(f'SELECT count(*) FROM "{t}"') for t in tables}
    finally:
        await conn.close()


async def _document_fingerprint(dsn: str, document_id: str) -> tuple[str, str] | None:
    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow(
            "SELECT encode(ciphertext, 'hex') AS ciphertext, encode(digest, 'hex') AS digest "
            "FROM documents WHERE id = $1::uuid",
            document_id,
        )
        return (row["ciphertext"], row["digest"]) if row else None
    finally:
        await conn.close()


async def main(original_dsn: str, restored_dsn: str, document_id: str) -> None:
    original_counts, restored_counts = await asyncio.gather(
        _table_counts(original_dsn), _table_counts(restored_dsn)
    )
    if original_counts != restored_counts:
        diff = {
            table: (original_counts.get(table), restored_counts.get(table))
            for table in sorted(set(original_counts) | set(restored_counts))
            if original_counts.get(table) != restored_counts.get(table)
        }
        print(f"row count mismatch: {diff}", file=sys.stderr)
        sys.exit(1)

    original_doc, restored_doc = await asyncio.gather(
        _document_fingerprint(original_dsn, document_id),
        _document_fingerprint(restored_dsn, document_id),
    )
    if original_doc is None or restored_doc is None or original_doc != restored_doc:
        print(
            f"document {document_id} ciphertext/digest mismatch: "
            f"original={original_doc} restored={restored_doc}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"OK: {len(original_counts)} tables match, document {document_id} digest matches")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3]))
