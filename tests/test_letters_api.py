"""Parent-to-child letter endpoints (BE integration)."""


def test_send_and_list_letter(client) -> None:
    resp = client.post(
        "/api/v1/parents/test_parent/letters",
        json={
            "child_id": "test-child-1",
            "content": "宝贝，今天开心吗？",
        },
    )
    assert resp.status_code == 200, resp.text
    letter = resp.json()["letter"]
    assert letter["child_id"] == "test-child-1"
    assert letter["from"] == "parent"
    assert letter["content"] == "宝贝，今天开心吗？"
    assert letter["read"] is False
    assert letter["id"]

    listed = client.get("/api/v1/children/test-child-1/letters")
    assert listed.status_code == 200, listed.text
    letters = listed.json()["letters"]
    assert len(letters) == 1
    assert letters[0]["id"] == letter["id"]
    assert letters[0]["read"] is False


def test_accepts_text_alias(client) -> None:
    resp = client.post(
        "/api/v1/parents/test_parent/letters",
        json={"child_id": "test-child-1", "text": "记得喝水"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["letter"]["content"] == "记得喝水"


def test_mark_letter_read(client) -> None:
    sent = client.post(
        "/api/v1/parents/test_parent/letters",
        json={"child_id": "test-child-1", "content": "晚安"},
    ).json()["letter"]

    read = client.post(f"/api/v1/children/letters/{sent['id']}/read")
    assert read.status_code == 200, read.text
    assert read.json()["read"] is True

    listed = client.get("/api/v1/children/test-child-1/letters").json()["letters"]
    assert listed[0]["read"] is True


def test_empty_content_rejected(client) -> None:
    resp = client.post(
        "/api/v1/parents/test_parent/letters",
        json={"child_id": "test-child-1", "content": "   "},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "LETTER_EMPTY"


def test_unknown_child_rejected(client) -> None:
    resp = client.post(
        "/api/v1/parents/test_parent/letters",
        json={"child_id": "no-such-child", "content": "hi"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CHILD_NOT_FOUND"


def test_foreign_child_rejected(client) -> None:
    from niki.services.child_registry import get_child_registry

    registry = get_child_registry()
    registry.register("foreign-child", "parent-other", name="foreign")

    resp = client.post(
        "/api/v1/parents/test_parent/letters",
        json={"child_id": "foreign-child", "content": "hi"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "PARENT_CHILD_MISMATCH"


def test_missing_letter_read_rejected(client) -> None:
    resp = client.post("/api/v1/children/letters/no-such-letter/read")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "LETTER_NOT_FOUND"
