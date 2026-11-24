import uvicorn

from backend.app.config import load_settings


if __name__ == "__main__":
    uvicorn.run("backend.app.main:app", host="127.0.0.1", port=load_settings().port)
