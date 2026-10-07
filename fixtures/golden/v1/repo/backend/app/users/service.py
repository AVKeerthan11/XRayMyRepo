from .models import User


class UserService:
    def __init__(self, session):
        self.session = session

    def get(self, user_id: int) -> User | None:
        return self.session.get(User, user_id)
