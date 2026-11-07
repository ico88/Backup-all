import uvicorn
import os

if __name__ == "__main__":
    # Carica .secret_key se esiste
    if os.path.exists(".secret_key"):
        with open(".secret_key") as f:
            os.environ.setdefault("BACKUP_SECRET_KEY", f.read().strip())

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=os.getenv("DEV", "false").lower() == "true",
    )
