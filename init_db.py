"""Run this once manually if you ever want to (re)initialise the database outside
of just starting the app - e.g. right after cloning, to see the admin credentials
printed before you start the server.

    python init_db.py
"""
from app.database import init_schema
from app.seed import seed_if_needed

if __name__ == "__main__":
    init_schema()
    seed_if_needed()
    print("Database ready.")
