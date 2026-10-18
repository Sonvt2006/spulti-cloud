from app.database import engine
from sqlalchemy import inspect, text

print("Username:", engine.url.username)
print("Database:", engine.url.database)

with engine.connect() as conn:
    print("Connected to:", conn.execute(text("SELECT current_database()")).scalar_one())

print("Tables:", inspect(engine).get_table_names())
