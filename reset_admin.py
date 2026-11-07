#!/usr/bin/env python3
"""Reset della password dell'utente amministratore."""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from app.database import SessionLocal, init_db
from app.models import User
from app.auth import hash_password

def main():
    new_password = sys.argv[1] if len(sys.argv) > 1 else "changeme"
    init_db()
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.username == "admin").first()
        if not admin:
            print("Utente admin non trovato. Crea prima un account con setup.")
            sys.exit(1)
        admin.hashed_password = hash_password(new_password)
        db.commit()
        print(f"Password admin resettata a: {new_password}")
        print("Accedi con username: admin")
    finally:
        db.close()

if __name__ == "__main__":
    main()
