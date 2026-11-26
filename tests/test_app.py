from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.cache import ai_response_cache
from app.database import Base, get_db
from app.main import app
from app.models import Message, User


@pytest.fixture()
def test_session_factory(tmp_path: Path):
    db_file = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    try:
        yield TestingSessionLocal
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def client(test_session_factory) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        db = test_session_factory()
        try:
            yield db
        finally:
            db.close()

    ai_response_cache.clear()
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def register_and_login(client: TestClient, email: str, password: str) -> str:
    reg_res = client.post("/auth/register", json={"email": email, "password": password})
    assert reg_res.status_code == 201
    login_res = client.post("/auth/login", json={"email": email, "password": password})
    assert login_res.status_code == 200
    return login_res.json()["access_token"]


def test_register_hashes_password_and_rejects_duplicate(client: TestClient, test_session_factory) -> None:
    raw_password = "StrongPassword123!"
    res = client.post("/auth/register", json={"email": "Alice@example.com", "password": raw_password})
    assert res.status_code == 201
    data = res.json()
    assert data["email"] == "alice@example.com"
    assert "password" not in data
    assert "password_hash" not in data

    with test_session_factory() as db:
        user = db.scalar(select(User).where(User.email == "alice@example.com"))
        assert user is not None
        assert user.password_hash != raw_password
        assert user.password_hash.startswith("$2")

    dup = client.post("/auth/register", json={"email": "alice@example.com", "password": raw_password})
    assert dup.status_code == 409


def test_login_and_current_user_dependency(client: TestClient) -> None:
    token = register_and_login(client, "bob@example.com", "SecretPass123!")

    bad_login = client.post("/auth/login", json={"email": "bob@example.com", "password": "wrong-password"})
    assert bad_login.status_code == 401

    me_res = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_res.status_code == 200
    assert me_res.json()["email"] == "bob@example.com"

    unauth_me = client.get("/auth/me")
    assert unauth_me.status_code == 401

    bad_token_me = client.get("/auth/me", headers={"Authorization": "Bearer invalid.jwt.token"})
    assert bad_token_me.status_code == 401


def test_conversation_endpoints_require_authentication(client: TestClient) -> None:
    assert client.post("/conversations", json={"title": "Unauth"}).status_code == 401
    assert client.get("/conversations").status_code == 401
    assert client.get("/conversations/1").status_code == 401
    assert client.post("/conversations/1/messages", json={"content": "hello"}).status_code == 401


def test_start_conversation_and_save_user_and_assistant_messages(
    client: TestClient, test_session_factory
) -> None:
    token = register_and_login(client, "carol@example.com", "CarolPass123!")
    headers = {"Authorization": f"Bearer {token}"}

    conv_res = client.post("/conversations", json={"title": "Architecture Chat"}, headers=headers)
    assert conv_res.status_code == 201
    conv_id = conv_res.json()["id"]

    msg_res = client.post(
        f"/conversations/{conv_id}/messages",
        json={"content": "Explain SQLAlchemy persistence"},
        headers=headers,
    )
    assert msg_res.status_code == 201
    turn = msg_res.json()
    assert turn["user_message"]["role"] == "user"
    assert turn["user_message"]["content"] == "Explain SQLAlchemy persistence"
    assert turn["assistant_message"]["role"] == "assistant"
    assert "Explain SQLAlchemy persistence" in turn["assistant_message"]["content"]

    detail_res = client.get(f"/conversations/{conv_id}", headers=headers)
    assert detail_res.status_code == 200
    messages = detail_res.json()["messages"]
    assert len(messages) == 2
    assert [m["role"] for m in messages] == ["user", "assistant"]

    with test_session_factory() as db:
        db_messages = list(db.scalars(select(Message).where(Message.conversation_id == conv_id)).all())
        assert len(db_messages) == 2


def test_private_data_protection_ownership_check(client: TestClient) -> None:
    token_a = register_and_login(client, "usera@example.com", "PasswordA123!")
    token_b = register_and_login(client, "userb@example.com", "PasswordB123!")

    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    create_b = client.post(
        "/conversations",
        json={"title": "User B Private Notes", "initial_message": "My private secret"},
        headers=headers_b,
    )
    assert create_b.status_code == 201
    conv_b_id = create_b.json()["id"]

    list_a = client.get("/conversations", headers=headers_a)
    assert list_a.status_code == 200
    assert list_a.json() == []

    read_b_as_a = client.get(f"/conversations/{conv_b_id}", headers=headers_a)
    assert read_b_as_a.status_code == 403

    modify_b_as_a = client.post(
        f"/conversations/{conv_b_id}/messages",
        json={"content": "Injected message"},
        headers=headers_a,
    )
    assert modify_b_as_a.status_code == 403

    verify_b = client.get(f"/conversations/{conv_b_id}", headers=headers_b)
    assert verify_b.status_code == 200
    assert len(verify_b.json()["messages"]) == 2
    assert verify_b.json()["messages"][0]["content"] == "My private secret"


def test_non_sensitive_ai_cache_and_database_permanence(client: TestClient) -> None:
    token = register_and_login(client, "dave@example.com", "DavePass123!")
    headers = {"Authorization": f"Bearer {token}"}

    conv = client.post("/conversations", json={"title": "FAQ"}, headers=headers).json()
    conv_id = conv["id"]

    first = client.post(
        f"/conversations/{conv_id}/messages",
        json={"content": "What is FastAPI?"},
        headers=headers,
    )
    assert first.status_code == 201
    assert first.json()["cached_ai_result"] is False

    second = client.post(
        f"/conversations/{conv_id}/messages",
        json={"content": "what is fastapi"},
        headers=headers,
    )
    assert second.status_code == 201
    assert second.json()["cached_ai_result"] is True

    ai_response_cache.clear()
    detail = client.get(f"/conversations/{conv_id}", headers=headers)
    assert detail.status_code == 200
    assert len(detail.json()["messages"]) == 4
