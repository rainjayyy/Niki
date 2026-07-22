"""End-to-end API tests for the auth endpoints."""

from fastapi.testclient import TestClient


def test_signup_start(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/auth/signup/start",
        json={
            "name": "张三",
            "id_number": "110101199003078811",
            "phone": "13800138000",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["account"]["status"] == "pending"
    assert data["account"]["is_verified"] is False
    assert data["account"]["id_number_masked"] == "110101****8811"
    assert data["account"]["phone_masked"] == "138****8000"
    assert len(data["progress"]["steps"]) == 3


def test_signup_start_invalid_id(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/auth/signup/start",
        json={"name": "张三", "id_number": "123", "phone": "13800138000"},
    )
    assert resp.status_code == 400


def test_signup_start_invalid_phone(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/auth/signup/start",
        json={"name": "张三", "id_number": "110101199003078811", "phone": "abc"},
    )
    assert resp.status_code == 400


def test_full_signup_flow(client: TestClient) -> None:
    """Complete the full 3-step signup."""
    # Step 1: start
    resp = client.post(
        "/api/v1/auth/signup/start",
        json={
            "name": "张三",
            "id_number": "110101199003078811",
            "phone": "13800138000",
        },
    )
    parent_id = resp.json()["account"]["id"]

    # Step 2: real-name
    resp = client.post(
        "/api/v1/auth/signup/real-name",
        json={
            "parent_id": parent_id,
            "name": "张三",
            "id_number": "110101199003078811",
        },
    )
    assert resp.status_code == 200

    # Step 3: face
    resp = client.post(
        "/api/v1/auth/signup/face",
        json={
            "parent_id": parent_id,
            "live_image_url": "https://example.com/face.jpg",
        },
    )
    assert resp.status_code == 200

    # Step 4: consent
    resp = client.post(
        "/api/v1/auth/signup/consent",
        json={
            "parent_id": parent_id,
            "typed_signature": "张三",
            "ip_address": "1.2.3.4",
            "device_fingerprint": "abc123",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["account"]["status"] == "verified"
    assert data["account"]["is_verified"] is True


def test_get_account(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/auth/signup/start",
        json={
            "name": "李四",
            "id_number": "110101199003078811",
            "phone": "13800138000",
        },
    )
    parent_id = resp.json()["account"]["id"]
    resp = client.get(f"/api/v1/auth/me/{parent_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["account"]["id"] == parent_id


def test_get_nonexistent_account(client: TestClient) -> None:
    resp = client.get("/api/v1/auth/me/nonexistent")
    assert resp.status_code == 404


def test_deletion_request(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/auth/signup/start",
        json={
            "name": "王五",
            "id_number": "110101199003078811",
            "phone": "13800138000",
        },
    )
    parent_id = resp.json()["account"]["id"]

    resp = client.post(
        "/api/v1/auth/deletion/request",
        json={"parent_id": parent_id},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["account"]["status"] == "deletion_pending"
    assert data["deletion_window_remaining_days"] >= 0
    assert data["deletion_window_remaining_days"] <= 30


def test_deletion_complete(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/auth/signup/start",
        json={
            "name": "赵六",
            "id_number": "110101199003078811",
            "phone": "13800138000",
        },
    )
    parent_id = resp.json()["account"]["id"]
    client.post(
        "/api/v1/auth/deletion/request",
        json={"parent_id": parent_id},
    )

    resp = client.post(
        "/api/v1/auth/deletion/complete",
        json={"parent_id": parent_id},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["account"]["status"] == "deleted"


def test_get_parent_consent_document(client: TestClient) -> None:
    resp = client.get("/api/v1/auth/consent/document/parent")
    assert resp.status_code == 200
    data = resp.json()
    assert data["type"] == "parent"
    assert "Niki" in data["text"]


def test_get_child_consent_document(client: TestClient) -> None:
    resp = client.get("/api/v1/auth/consent/document/child")
    assert resp.status_code == 200
    data = resp.json()
    assert data["type"] == "child"
    assert "Niki" in data["text"]


def test_get_invalid_consent_type(client: TestClient) -> None:
    resp = client.get("/api/v1/auth/consent/document/invalid")
    assert resp.status_code == 400


def test_deletion_complete_without_request_raises(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/auth/signup/start",
        json={
            "name": "孙七",
            "id_number": "110101199003078811",
            "phone": "13800138000",
        },
    )
    parent_id = resp.json()["account"]["id"]
    resp = client.post(
        "/api/v1/auth/deletion/complete",
        json={"parent_id": parent_id},
    )
    assert resp.status_code == 400


def test_real_name_verification_failure(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/auth/signup/start",
        json={
            "name": "周八",
            "id_number": "110101199003078811",
            "phone": "13800138000",
        },
    )
    parent_id = resp.json()["account"]["id"]
    resp = client.post(
        "/api/v1/auth/signup/real-name",
        json={
            "parent_id": parent_id,
            "name": "周八",
            "id_number": "bad",  # invalid format
        },
    )
    assert resp.status_code == 400


def test_face_verification_failure(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/auth/signup/start",
        json={
            "name": "吴九",
            "id_number": "110101199003078811",
            "phone": "13800138000",
        },
    )
    parent_id = resp.json()["account"]["id"]
    resp = client.post(
        "/api/v1/auth/signup/face",
        json={
            "parent_id": parent_id,
            "live_image_url": "not-a-url",
        },
    )
    assert resp.status_code == 400
