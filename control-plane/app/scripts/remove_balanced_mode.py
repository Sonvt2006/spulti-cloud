"""Migrate existing PostgreSQL machines and saved requests to two modes.

Stop API/workers first, then run: python -m app.scripts.remove_balanced_mode
"""

from app.database import engine
from sqlalchemy import text


def main() -> None:
    if engine.dialect.name != "postgresql":
        raise RuntimeError("This migration requires PostgreSQL")
    with engine.begin() as connection:
        connection.execute(
            text("LOCK TABLE machines, machine_lifecycles IN ACCESS EXCLUSIVE MODE")
        )
        connection.execute(
            text("""
            UPDATE machines SET optimization_mode = 'SAVING'
            WHERE optimization_mode = 'BALANCED'
        """)
        )
        connection.execute(
            text("""
            UPDATE machine_lifecycles
            SET request_payload = jsonb_set(
                request_payload, '{optimization_mode}', '"SAVING"'::jsonb
            )
            WHERE request_payload->>'optimization_mode' = 'BALANCED'
        """)
        )
        connection.execute(
            text("""
            ALTER TABLE machines
            DROP CONSTRAINT IF EXISTS machines_optimization_mode_check
        """)
        )
        connection.execute(
            text("""
            ALTER TABLE machines
            ADD CONSTRAINT machines_optimization_mode_check
            CHECK (optimization_mode IN ('SAVING', 'STABLE'))
        """)
        )
    print("Migration complete: only SAVING and STABLE are supported.")


if __name__ == "__main__":
    main()
