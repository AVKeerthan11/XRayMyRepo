from app.users.service import UserService


def test_get_user_returns_none_when_missing():
    session = type("FakeSession", (), {"get": lambda self, model, key: None})()
    assert UserService(session).get(1) is None
