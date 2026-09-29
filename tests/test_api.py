from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["model_loaded"]


def test_predict_ok():
    r = client.post("/predict", json={"category": "R03", "horizon": 7})
    assert r.status_code == 200
    body = r.json()
    assert len(body["points"]) == 7 and body["total_forecast"] > 0


def test_predict_validation():
    assert client.post("/predict", json={"category": "R03", "horizon": 0}).status_code == 422
    assert client.post("/predict", json={"category": "NOPE", "horizon": 5}).status_code == 422
