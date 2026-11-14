"""Migration guards plus optional PostgreSQL data-preservation regressions."""

import os
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

from app.scripts.add_price_validity import migrate
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError


class PriceValidityMigrationTests(unittest.TestCase):
    def engine(self, *, column_exists, constraint_exists):
        engine = MagicMock()
        engine.dialect.name = "postgresql"
        connection = engine.begin.return_value.__enter__.return_value
        connection.scalar.side_effect = [column_exists, constraint_exists]
        connection.execute.return_value.rowcount = 3
        return engine, connection

    def test_rejects_non_postgresql_before_opening_a_transaction(self):
        engine = MagicMock()
        engine.dialect.name = "sqlite"
        with self.assertRaisesRegex(RuntimeError, "requires PostgreSQL"):
            migrate(engine)
        engine.begin.assert_not_called()

    def test_first_addition_locks_before_detection_and_quarantines_legacy_gcp(self):
        engine, connection = self.engine(column_exists=False, constraint_exists=False)
        result = migrate(engine)
        self.assertEqual(result, {"column_added": True, "prices_quarantined": 3})
        calls = connection.method_calls
        self.assertEqual(calls[0][0], "execute")
        self.assertIn("ACCESS EXCLUSIVE", str(calls[0].args[0]))
        self.assertEqual(calls[1][0], "scalar")
        statements = [str(call.args[0]) for call in connection.execute.call_args_list]
        update = next(sql for sql in statements if "UPDATE" in sql)
        self.assertIn("machine.provider = 'GCP'", update)
        self.assertIn("price.price_type = 'SPOT'", update)
        self.assertIn("SET valid_until = price.recorded_at", update)
        self.assertTrue(any("valid_until >= recorded_at" in sql for sql in statements))

    def test_rerun_does_not_quarantine_verified_open_intervals(self):
        engine, connection = self.engine(column_exists=True, constraint_exists=True)
        self.assertEqual(
            migrate(engine), {"column_added": False, "prices_quarantined": 0}
        )
        statements = [str(call.args[0]) for call in connection.execute.call_args_list]
        self.assertEqual(len(statements), 1)
        self.assertIn("LOCK TABLE", statements[0])

    def test_existing_column_can_receive_missing_constraint_without_data_updates(self):
        engine, connection = self.engine(column_exists=True, constraint_exists=False)
        migrate(engine)
        statements = [str(call.args[0]) for call in connection.execute.call_args_list]
        self.assertFalse(any("UPDATE" in sql for sql in statements))
        self.assertTrue(any("ADD CONSTRAINT" in sql for sql in statements))


@unittest.skipUnless(
    os.getenv("TEST_POSTGRES_URL"),
    "Set TEST_POSTGRES_URL for PostgreSQL migration tests",
)
class PostgreSQLPriceValidityMigrationTests(unittest.TestCase):
    def setUp(self):
        self.schema = "price_validity_test_" + uuid4().hex
        self.admin = create_engine(os.environ["TEST_POSTGRES_URL"])
        with self.admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.engine = create_engine(
            os.environ["TEST_POSTGRES_URL"],
            connect_args={"options": f"-csearch_path={self.schema}"},
        )
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                CREATE TABLE machine_types (
                    id bigint PRIMARY KEY,
                    provider text NOT NULL
                )
            """)
            )
            connection.execute(
                text("""
                CREATE TABLE spot_prices (
                    id bigint PRIMARY KEY,
                    machine_type_id bigint NOT NULL REFERENCES machine_types(id),
                    price_type text NOT NULL,
                    price_per_hour numeric(12, 6) NOT NULL,
                    recorded_at timestamptz NOT NULL
                )
            """)
            )
            connection.execute(
                text("""
                INSERT INTO machine_types VALUES (1, 'GCP'), (2, 'AWS')
            """)
            )
            connection.execute(
                text("""
                INSERT INTO spot_prices VALUES
                    (1, 1, 'SPOT', .040956, '2026-09-07T00:00:00Z'),
                    (2, 2, 'SPOT', .1, '2026-09-07T00:00:00Z'),
                    (3, 1, 'ON_DEMAND', .04, '2026-09-07T00:00:00Z'),
                    (4, 2, 'ON_DEMAND', .2, '2026-09-07T00:00:00Z')
            """)
            )

    def tearDown(self):
        self.engine.dispose()
        with self.admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        self.admin.dispose()

    def rows(self):
        with self.engine.connect() as connection:
            return (
                connection.execute(text("SELECT * FROM spot_prices ORDER BY id"))
                .mappings()
                .all()
            )

    def test_preserves_rates_and_only_quarantines_legacy_gcp_spot(self):
        result = migrate(self.engine)
        self.assertEqual(result, {"column_added": True, "prices_quarantined": 1})
        rows = self.rows()
        self.assertEqual([row["id"] for row in rows], [1, 2, 3, 4])
        self.assertEqual(rows[0]["price_per_hour"], Decimal(".040956"))
        self.assertEqual(rows[0]["valid_until"], rows[0]["recorded_at"])
        self.assertTrue(all(row["valid_until"] is None for row in rows[1:]))

    def test_second_run_preserves_verified_open_ended_gcp_spot(self):
        migrate(self.engine)
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO spot_prices VALUES
                    (5, 1, 'SPOT', .03, '2026-11-14T00:00:00Z', NULL)
            """)
            )
        before = self.rows()
        self.assertEqual(
            migrate(self.engine), {"column_added": False, "prices_quarantined": 0}
        )
        self.assertEqual(self.rows(), before)
        self.assertIsNone(self.rows()[-1]["valid_until"])

    def test_rejects_end_before_start_and_rolls_back_failed_update(self):
        migrate(self.engine)
        with self.assertRaises(IntegrityError), self.engine.begin() as connection:
            connection.execute(
                text("""
                UPDATE spot_prices SET valid_until = '2026-09-06T00:00:00Z'
                WHERE id = 1
            """)
            )
        self.assertEqual(
            self.rows()[0]["valid_until"], datetime(2026, 9, 7, tzinfo=UTC)
        )

    def test_failed_legacy_update_rolls_back_added_column_and_all_rows(self):
        before = self.rows()
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                CREATE FUNCTION reject_price_update() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                    RAISE EXCEPTION 'migration update rejected';
                END;
                $$
            """)
            )
            connection.execute(
                text("""
                CREATE TRIGGER reject_price_update
                BEFORE UPDATE ON spot_prices
                FOR EACH ROW EXECUTE FUNCTION reject_price_update()
            """)
            )
        with self.assertRaises(DBAPIError):
            migrate(self.engine)
        with self.engine.connect() as connection:
            self.assertFalse(
                connection.scalar(
                    text("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_attribute
                    WHERE attrelid = 'spot_prices'::regclass
                      AND attname = 'valid_until'
                      AND NOT attisdropped
                )
            """)
                )
            )
        self.assertEqual(self.rows(), before)


if __name__ == "__main__":
    unittest.main()
