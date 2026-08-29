from datetime import datetime, timedelta
from typing import Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import ExpiredSignatureError, JWTError, jwt
from passlib.context import CryptContext
from passlib.hash import bcrypt_sha256, pbkdf2_sha256
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.database import get_db
from src.core.logging import get_logger
from src.api.errors import forbidden_error
from src.models import User

logger = get_logger(__name__)
"""
密碼雜湊設定：
- 首選 pbkdf2_sha256（無 72 bytes 限制，跨平台穩定）
- 仍保留 bcrypt_sha256 與 bcrypt 以相容既有使用者（deprecated=auto 會在重新雜湊時升級）
"""
pwd_context = CryptContext(schemes=['pbkdf2_sha256', 'bcrypt_sha256', 'bcrypt'], deprecated='auto')
security = HTTPBearer(auto_error=False)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """驗證密碼。
    - 優先嘗試以當前 context 驗證（支援 bcrypt_sha256 / bcrypt）
    - 若底層實作對長密碼拋出 ValueError（bcrypt > 72 bytes），改以截斷後再驗證以相容舊資料
    """
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except ValueError:
        # 舊版註冊流程曾截斷至 72 bytes 才雜湊，這裡做相容處理
        return pwd_context.verify(plain_password[:72], hashed_password)



def get_password_hash(password: str) -> str:
    """產生密碼雜湊。
    - 預設使用 bcrypt_sha256（無 72 bytes 限制）。
    - 不再主動截斷；由 bcrypt_sha256 預處理確保長密碼安全。
    - 舊資料（bcrypt）仍可由 verify_password 相容驗證。
    """
    try:
        # 新註冊一律使用 pbkdf2_sha256，避免 bcrypt 類型在部分環境的 72 bytes 限制與後端載入偵測問題
        return pbkdf2_sha256.hash(password)
    except Exception:
        # 後備路徑：若環境異常，退回 context 預設（將會在 schemes 中選擇可用者）
        return pwd_context.hash(password)


def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now() + expires_delta
    else:
        expire = datetime.now() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({'exp': expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


def decode_token(token: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except ExpiredSignatureError:
        logger.warning('token_decode_expired')
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Token has expired',
        )
    except JWTError as e:
        logger.error('token_decode_error', error=str(e))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Could not validate credentials',
        )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        # Behave as forbidden when no credentials provided (tests expect 403)
        raise forbidden_error('Authentication required')
    token = credentials.credentials
    payload = decode_token(token)
    user_id: str = payload.get('sub')
    if user_id is None:
        raise forbidden_error('Could not validate credentials')

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise forbidden_error('User not found')
    if not bool(getattr(user, 'enabled', True)):
        raise forbidden_error('User disabled')

    return user


async def get_current_active_user(current_user: User = Depends(get_current_user)) -> User:
    return current_user
