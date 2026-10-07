from fastapi import APIRouter, Depends

from .schemas import UserOut
from .service import UserService

router = APIRouter(prefix="/users")


@router.get("/{user_id}", response_model=UserOut)
def get_user(user_id: int, service: UserService = Depends()) -> UserOut:
    return service.get(user_id)
