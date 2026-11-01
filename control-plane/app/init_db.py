# Tạo các bảng còn thiếu: python -m app.init_db
# Bảng đã có thì giữ nguyên, không sửa.
from app import models  # noqa: F401  (import để đăng ký các model)
from app.database import Base, engine


def main() -> None:
    Base.metadata.create_all(engine)
    print("Tables:", ", ".join(Base.metadata.tables))


if __name__ == "__main__":
    main()
