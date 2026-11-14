"""Add price validity and quarantine unverified legacy GCP Spot history.

Stop API/workers first, then run: python -m app.scripts.add_price_validity
The first migration preserves every row and price, but marks old GCP Spot
intervals empty until their actual intervals can be restored from Google.
"""

from sqlalchemy import text
from sqlalchemy.engine import Engine


def migrate(engine: Engine) -> dict[str, int | bool]:
    if engine.dialect.name != "postgresql":
        raise RuntimeError("This migration requires PostgreSQL")
    with engine.begin() as connection:
        # Serialize migrations and collectors before deciding whether these are
        # legacy rows. Re-running must not expire verified open-ended prices.
        connection.execute(text("LOCK TABLE spot_prices IN ACCESS EXCLUSIVE MODE"))
        column_exists = connection.scalar(
            text("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_attribute
                    WHERE attrelid = 'spot_prices'::regclass
                      AND attname = 'valid_until'
                      AND NOT attisdropped
                )
            """)
        )
        quarantined = 0
        if not column_exists:
            connection.execute(
                text("ALTER TABLE spot_prices ADD COLUMN valid_until timestamptz")
            )
            result = connection.execute(
                text("""
                    UPDATE spot_prices AS price
                    SET valid_until = price.recorded_at
                    FROM machine_types AS machine
                    WHERE machine.id = price.machine_type_id
                      AND machine.provider = 'GCP'
                      AND price.price_type = 'SPOT'
                """)
            )
            quarantined = result.rowcount
        constraint_exists = connection.scalar(
            text("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'spot_prices'::regclass
                      AND conname = 'spot_prices_valid_until_check'
                )
            """)
        )
        if not constraint_exists:
            connection.execute(
                text("""
                    ALTER TABLE spot_prices
                    ADD CONSTRAINT spot_prices_valid_until_check
                    CHECK (valid_until IS NULL OR valid_until >= recorded_at)
                """)
            )
    return {
        "column_added": not column_exists,
        "prices_quarantined": quarantined,
    }


def main() -> None:
    from app.database import engine

    result = migrate(engine)
    print(f"Price validity migration complete: {result}")
    print("Restore legacy GCP Spot validity from the Google API before relying on it.")


if __name__ == "__main__":
    main()
