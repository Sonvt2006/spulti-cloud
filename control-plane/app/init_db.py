# Create missing tables: python -m app.init_db
# Existing tables are not changed.
from app import models  # noqa: F401  (registers the models)
from app.database import Base, engine


def main() -> None:
    Base.metadata.create_all(engine)
    print("Tables:", ", ".join(Base.metadata.tables))


if __name__ == "__main__":
    main()
