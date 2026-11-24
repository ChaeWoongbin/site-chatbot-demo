from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.main import create_app


def test_sample_answer_without_api_key(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/health").json() == {"status": "ok"}
        result = client.post("/api/chat", json={"message": "How long is the warranty?"}).json()
        assert result["type"] == "excerpt"
        assert "two-year warranty" in result["message"]
        assert result["sources"][0]["title"] == "L120 Desk Lamp"
        ko = client.post("/api/chat", json={"message": "보증기간은 얼마인가요?", "language": "ko"}).json()
        assert ko["type"] == "excerpt"
        assert "보증기간은 2년" in ko["message"]
        assert ko["sources"][0]["title"] == "L120 책상 조명"
        missing = client.post("/api/chat", json={"message": "천체사진", "language": "ko"}).json()
        assert missing["message"] == "자료에서 관련 내용을 찾을 수 없습니다."
        assert client.post("/api/chat", json={"message": "Warranty", "language": "fr"}).status_code == 422
        assert client.post("/api/chat", json={"message": "    "}).status_code == 422
        assert client.post("/api/chat", json={"message": "astrophotography"}).json()["type"] == "no_match"


def test_upload_requires_a_configured_token_and_indexes_document(tmp_path):
    payload = {
        "data": {"title": "Example manual"},
        "files": {"file": ("manual.md", b"# Calibration\n\nThe spectrometer uses a quartz reference.")},
    }
    app = create_app(Settings(data_dir=tmp_path, upload_token="local-test-token"))
    with TestClient(app) as client:
        assert client.post("/api/documents", **payload).status_code == 401
        headers = {"X-Upload-Token": "local-test-token"}
        response = client.post("/api/documents", headers=headers, **payload)
        assert response.status_code == 201
        assert response.json()["chunks"] > 0
        result = client.post("/api/chat", json={"message": "spectrometer calibration"}).json()
        assert "quartz reference" in result["message"]
        assert result["sources"][0]["title"] == "Example manual"
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        assert client.post("/api/documents", **payload).status_code == 403
