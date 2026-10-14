import os
import uvicorn
from dotenv import load_dotenv

load_dotenv()
if __name__ == "__main__":
    host, port = os.getenv("HOST", "127.0.0.1"), int(os.getenv("PORT", 8000))
    print(f"\n  Dictionary is running → http://{host}:{port}\n")
    uvicorn.run("server.main:app", host=host, port=port)
