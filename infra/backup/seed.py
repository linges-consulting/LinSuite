"""Seed one customer with one sealed document, through the app's own code — never raw SQL for
the encrypted part, so the rehearsal proves the same crypto path production uses (rehearse.sh,
#89). Run with `uv run python seed.py` from `app/backend`, with the Settings env vars already
exported (rehearse.sh does this). Prints "<customer_id> <document_id>" on success.
"""

import asyncio
import uuid

from sqlalchemy import text

from core.db import session_scope
from core.documents import store_document
from customers.keys import data_key


async def main() -> None:
    async with session_scope() as db:
        # The single business row (core/models.py: id is pinned to 1 by a CHECK).
        await db.execute(
            text(
                "INSERT INTO businesses (name, timezone) VALUES ('Rehearsal Co', 'America/Toronto')"
            )
        )
        customer_id = await db.scalar(
            text(
                "INSERT INTO customers (first_name, last_name) VALUES ('Rehearsal', 'Client') "
                "RETURNING id"
            )
        )
        key = await data_key(db, customer_id)
        document_id = await store_document(
            db,
            key=key,
            key_owner="customer",
            customer_id=customer_id,
            kind="rehearsal_probe",
            source_id=uuid.uuid4(),
            content=b"restore rehearsal probe document \x00\x01\x02 not a real PDF",
            content_type="application/octet-stream",
        )
        await db.commit()
    print(f"{customer_id} {document_id}")


if __name__ == "__main__":
    asyncio.run(main())
