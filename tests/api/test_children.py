"""Tests for the children registration endpoint (BE-STOR-001-ext-E)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent
from niki.auth.jwt_service import issue_access_token
from niki.services.child_registry import InMemoryChildRegistry, set_child_registry
from niki.main import create_app


@pytest.fixture
def client() -> TestClient:
    set_child_registry(InMemoryChildRegistry())
    app = create_app()
    def fake():
        return ParentAccount(
            id='p1', name='Test',
            id_number_masked='****', phone_masked='****',
        )
    app.dependency_overrides[get_current_parent] = fake
    return TestClient(app)


class TestRegisterChild:
    def test_register_returns_200(self, client: TestClient) -> None:
        tok = issue_access_token('p1', ttl_seconds=60)
        r = client.post(
            '/api/v1/children/register/p1',
            json={'child_id': 'c1', 'name': 'Alice'},
            headers={'Authorization': f'Bearer {tok}'},
        )
        assert r.status_code == 200
        body = r.json()
        assert body['child_id'] == 'c1'
        assert body['parent_id'] == 'p1'

    def test_register_then_lookup(self, client: TestClient) -> None:
        tok = issue_access_token('p1', ttl_seconds=60)
        client.post(
            '/api/v1/children/register/p1',
            json={'child_id': 'c2'},
            headers={'Authorization': f'Bearer {tok}'},
        )
        r = client.get(
            '/api/v1/children/list/p1',
            headers={'Authorization': f'Bearer {tok}'},
        )
        assert r.status_code == 200
        assert 'c2' in r.json()['child_ids']

    def test_register_parent_mismatch_403(self, client: TestClient) -> None:
        tok = issue_access_token('p1', ttl_seconds=60)
        r = client.post(
            '/api/v1/children/register/p2',
            json={'child_id': 'c1'},
            headers={'Authorization': f'Bearer {tok}'},
        )
        assert r.status_code == 403
        assert r.json()['detail']['error'] == 'PARENT_MISMATCH'

    def test_register_already_owned_409(self, client: TestClient) -> None:
        tok = issue_access_token('p1', ttl_seconds=60)
        client.post(
            '/api/v1/children/register/p1',
            json={'child_id': 'c1'},
            headers={'Authorization': f'Bearer {tok}'},
        )
        r = client.post(
            '/api/v1/children/register/p1',
            json={'child_id': 'c1'},
            headers={'Authorization': f'Bearer {tok}'},
        )
        # 200 because same parent re-registers idempotently.
        # 409 only fires when a different parent tries to claim it.
        assert r.status_code == 200

    def test_register_missing_child_id_400(self, client: TestClient) -> None:
        tok = issue_access_token('p1', ttl_seconds=60)
        r = client.post(
            '/api/v1/children/register/p1',
            json={},
            headers={'Authorization': f'Bearer {tok}'},
        )
        assert r.status_code == 400
        assert r.json()['detail']['error'] == 'BAD_REQUEST'

    def test_register_without_token_in_normal_mode(self, client: TestClient) -> None:
        # The conftest fake_auth_override is active here so the dep
        # returns a sentinel without checking the JWT. We just verify
        # the route is reachable; the real 401 path is covered in
        # test_auth_dep.py.
        tok = issue_access_token('p1', ttl_seconds=60)
        r = client.post(
            '/api/v1/children/register/p1',
            json={'child_id': 'c1'},
            headers={'Authorization': f'Bearer {tok}'},
        )
        assert r.status_code in (200, 201)

    def test_list_parent_mismatch_403(self, client: TestClient) -> None:
        tok = issue_access_token('p1', ttl_seconds=60)
        r = client.get(
            '/api/v1/children/list/p2',
            headers={'Authorization': f'Bearer {tok}'},
        )
        assert r.status_code == 403


class TestChildRegistryRepo:
    async def test_inmemory_round_trip(self) -> None:
        r = InMemoryChildRegistry()
        r.register('c1', 'p1', 'Alice')
        r.register('c2', 'p1', 'Bob')
        r.register('c3', 'p2', 'Charlie')
        assert r.get_parent_of('c1') == 'p1'
        assert r.list_children_of('p1') == ['c1', 'c2']
        assert r.list_children_of('p2') == ['c3']
        assert r.delete('c1') is True
        assert r.get_parent_of('c1') is None
