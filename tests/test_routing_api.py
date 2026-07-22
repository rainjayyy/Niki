"""End-to-end API tests for the routing endpoints."""

from fastapi.testclient import TestClient


def test_classify_tutoring_input(client: TestClient) -> None:
    resp = client.post("/api/v1/routing/classify", json={"text": "数学题不会做"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["primary"] is not None
    assert data["primary"]["category"] == "tutoring"


def test_classify_emotional_input(client: TestClient) -> None:
    resp = client.post("/api/v1/routing/classify", json={"text": "我今天很难过"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["primary"]["category"] == "emotional"


def test_classify_encyclopedia_input(client: TestClient) -> None:
    resp = client.post("/api/v1/routing/classify", json={"text": "为什么天是蓝的"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["primary"]["category"] == "encyclopedia"


def test_classify_empty_text(client: TestClient) -> None:
    resp = client.post("/api/v1/routing/classify", json={"text": ""})
    assert resp.status_code == 200
    data = resp.json()
    assert data["primary"] is None


def test_route_to_tutoring(client: TestClient) -> None:
    resp = client.post("/api/v1/routing/route", json={"text": "数学题不会做"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["primary_agent"] == "tutoring_agent"
    assert data["is_composite"] is False


def test_route_composite_empathy_first(client: TestClient) -> None:
    """Emotional + tutoring → emotional first per PRD."""
    # Use text with strong emotional + tutoring signals
    resp = client.post(
        "/api/v1/routing/route",
        json={"text": "我难过伤心哭，数学题也不会做"},
    )
    assert resp.status_code == 200
    data = resp.json()
    # Emotional empathy first
    assert data["primary_agent"] == "emotional_agent"
    assert data["is_composite"] is True
    assert "tutoring_agent" in data["parallel_agents"]


def test_route_empty_defaults_to_emotional(client: TestClient) -> None:
    resp = client.post("/api/v1/routing/route", json={"text": ""})
    assert resp.status_code == 200
    data = resp.json()
    assert data["primary_agent"] == "emotional_agent"


def test_list_personas(client: TestClient) -> None:
    resp = client.get("/api/v1/routing/personas")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["personas"]) == 3
    persona_names = {p["name"] for p in data["personas"]}
    assert persona_names == {"lively", "gentle", "rigorous"}
    # Each persona has TTS params
    for p in data["personas"]:
        assert "tts" in p
        assert "speech_rate" in p["tts"]


def test_inject_persona_gentle(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/routing/persona/inject",
        json={"base_prompt": "You are a tutor.", "persona": "gentle"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["persona"] == "gentle"
    assert "You are a tutor." in data["injected_prompt"]
    # Should include persona fragment
    assert "温柔" in data["injected_prompt"] or "Niki" in data["injected_prompt"]


def test_inject_persona_lively(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/routing/persona/inject",
        json={"base_prompt": "You are an encyclopedia.", "persona": "lively"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["persona"] == "lively"
    # Lively should be more enthusiastic
    assert "热情" in data["injected_prompt"] or "哇塞" in data["injected_prompt"]


def test_inject_persona_invalid(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/routing/persona/inject",
        json={"base_prompt": "...", "persona": "unknown_persona"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data


def test_inject_persona_default(client: TestClient) -> None:
    """No persona specified → use default (gentle)."""
    resp = client.post(
        "/api/v1/routing/persona/inject",
        json={"base_prompt": "base"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["persona"] == "gentle"


def test_fuse_agents(client: TestClient) -> None:
    """Fuse mocked agent outputs."""
    resp = client.post(
        "/api/v1/routing/fuse",
        json={
            "decision": {
                "primary_agent": "emotional_agent",
                "parallel_agents": ["tutoring_agent"],
            },
            "outputs": {
                "emotional_agent": {"text": "我在呢", "emotion": "caring"},
                "tutoring_agent": {"text": "一起看~"},
            },
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["strategy"] == "concatenate"
    assert "我在呢" in data["text"]
    assert "一起看" in data["text"]
