import asyncio
import hashlib
import secrets
from pathlib import Path
from uuid import UUID, uuid4
from datetime import datetime, timedelta, timezone

import uvicorn
from fastapi import APIRouter, HTTPException, status, Depends, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, EmailStr, field_validator, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from jose import jwt
from passlib.context import CryptContext

from backend.config import get_settings
from backend.dependencies import get_db,get_current_user
from backend.core.logger import get_logger
from backend.core.auth_throttle import limit_auth_attempts

# ── 兼容性补丁：passlib 1.7.4 要读 bcrypt.__about__.__version__，而 bcrypt>=4 删了它 ──
import bcrypt as _bcrypt_mod, types as _types
if not hasattr(_bcrypt_mod, "__about__"):
    _about = _types.SimpleNamespace(__version__=getattr(_bcrypt_mod, "__version__", "4.x"))
    _bcrypt_mod.__about__ = _about   # 注入假的 __about__，让 passlib 能探测到版本

router = APIRouter()                                          # 本模块的路由集合
logger = get_logger(__name__)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")  # 密码哈希上下文
AUTH_FRONTEND = Path(__file__).resolve().parents[3] / "frontend/auth"


@router.get("/ui", include_in_schema=False)
async def registration_page():
    return FileResponse(AUTH_FRONTEND / "index.html", media_type="text/html", headers={"Cache-Control": "no-store"})


@router.get("/ui/admin", include_in_schema=False)
async def invitations_page():
    return FileResponse(AUTH_FRONTEND / "admin.html", media_type="text/html", headers={"Cache-Control": "no-store"})


@router.get("/ui/assets/{name}", include_in_schema=False)
async def auth_asset(name: str):
    types = {"auth.js": "text/javascript", "auth.css": "text/css"}
    if name not in types:
        raise HTTPException(404, "页面资源不存在")
    return FileResponse(AUTH_FRONTEND / name, media_type=types[name], headers={"Cache-Control": "no-cache"})


class LoginRequest(BaseModel):
    """登录请求体。"""
    username: str = Field(..., min_length=1, max_length=128, description="用户名或邮箱")
    password: str = Field(..., min_length=1, max_length=256, description="密码")


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: str
    user_id: str


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]+$")
    email: EmailStr = Field(max_length=128)
    password: str = Field(min_length=12, max_length=72)
    invite_code: str = Field(default="", max_length=128)

    @field_validator("password")
    @classmethod
    def password_length(cls, value):
        if len(value.encode("utf-8")) > 72:
            raise ValueError("密码的 UTF-8 长度不能超过 72 字节")
        return value


class InviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(default="", max_length=100)
    max_uses: int = Field(default=1, ge=1, le=100)
    expires_days: int = Field(default=7, ge=1, le=30)


def _token_for_user(row):
    settings = get_settings()
    return TokenResponse(access_token=_create_access_token(
        {"sub": str(row["id"]), "role": row["role"], "tenant_id": row["tenant_id"]},
        settings.jwt_access_token_expire_minutes),
        expires_in=settings.jwt_access_token_expire_minutes * 60,
        role=row["role"], user_id=str(row["id"]))


async def require_admin(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "admin":
        raise HTTPException(403, "此操作需要管理员权限")
    return current_user


@router.get("/registration")
async def registration_options():
    return {"mode": get_settings().registration_mode, "password_min_length": 12}


@router.post("/register", status_code=201, response_model=TokenResponse)
async def register(req: RegisterRequest, request: Request, db: AsyncSession = Depends(get_db)):
    limit_auth_attempts(request, "register")
    settings = get_settings()
    if settings.registration_mode == "closed":
        raise HTTPException(403, "当前未开放注册，请联系管理员")
    # Serialize registrations across workers and tenants so username/email login
    # remains unambiguous, including username-to-email collisions.
    await db.execute(text("SELECT pg_advisory_xact_lock(710120261012)"))
    values = {"username": req.username.lower(), "email": str(req.email).lower()}
    duplicate = await db.execute(text("""
        SELECT id FROM users WHERE lower(username) IN (:username, :email)
        OR lower(email) IN (:username, :email) LIMIT 1
    """), values)
    if duplicate.first():
        raise HTTPException(409, "用户名或邮箱已被使用")
    tenant_id = settings.default_tenant_id
    if settings.registration_mode == "invite":
        result = await db.execute(text("""
            UPDATE registration_invites SET used_count = used_count + 1
            WHERE token_hash = :token_hash AND revoked_at IS NULL
              AND expires_at > NOW() AND used_count < max_uses
            RETURNING tenant_id
        """), {"token_hash": hashlib.sha256(req.invite_code.strip().encode()).hexdigest()})
        invite = result.mappings().first()
        if not invite:
            raise HTTPException(400, "邀请码无效、已过期或名额已用完，请联系管理员")
        tenant_id = invite["tenant_id"]
    password_hash = await asyncio.to_thread(pwd_context.hash, req.password)
    user = {"id": str(uuid4()), "tenant_id": tenant_id, "role": "user"}
    try:
        await db.execute(text("""
            INSERT INTO users (id, tenant_id, username, email, password_hash, role)
            VALUES (:id, :tenant_id, :username, :email, :password_hash, :role)
        """), {**user, "username": req.username, "email": values["email"], "password_hash": password_hash})
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "用户名或邮箱已被使用") from None
    logger.info("auth.register_success", user_id=user["id"], tenant_id=tenant_id)
    return _token_for_user(user)


@router.post("/invites", status_code=201)
async def create_invite(req: InviteRequest, current_user=Depends(require_admin), db: AsyncSession = Depends(get_db)):
    code = secrets.token_urlsafe(24)
    record = {"id": str(uuid4()), "tenant_id": current_user["tenant_id"],
              "created_by": current_user["user_id"], "label": req.label,
              "max_uses": req.max_uses,
              "expires_at": datetime.now(timezone.utc) + timedelta(days=req.expires_days)}
    await db.execute(text("""
        INSERT INTO registration_invites
          (id, token_hash, token_hint, tenant_id, created_by, label, max_uses, expires_at)
        VALUES (:id, :token_hash, :token_hint, :tenant_id, :created_by, :label, :max_uses, :expires_at)
    """), {**record, "token_hash": hashlib.sha256(code.encode()).hexdigest(), "token_hint": code[-6:]})
    await db.commit()
    return {"id": record["id"], "code": code, "expires_at": record["expires_at"], "max_uses": req.max_uses}


@router.get("/invites")
async def list_invites(current_user=Depends(require_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(text("""
        SELECT id, token_hint, label, max_uses, used_count, expires_at, revoked_at, created_at
        FROM registration_invites WHERE tenant_id = :tenant_id
        ORDER BY created_at DESC LIMIT 100
    """), {"tenant_id": current_user["tenant_id"]})
    return {"items": [dict(row) for row in result.mappings().all()]}


@router.delete("/invites/{invite_id}")
async def revoke_invite(invite_id: UUID, current_user=Depends(require_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(text("""
        UPDATE registration_invites SET revoked_at = COALESCE(revoked_at, NOW())
        WHERE id = :id AND tenant_id = :tenant_id RETURNING id
    """), {"id": str(invite_id), "tenant_id": current_user["tenant_id"]})
    if not result.first():
        raise HTTPException(404, "邀请码不存在")
    await db.commit()
    return {"revoked": True}


def _create_access_token(data: dict, expires_minutes: int) -> str:
    """把身份信息 + 过期时间打包，用密钥签名成 JWT 字符串。"""
    settings = get_settings()
    payload = data.copy()                                            # 拷一份，避免改到原字典
    expire = datetime.now(timezone.utc) + timedelta(minutes=expires_minutes)
    payload["exp"] = expire                                          # exp 是 JWT 标准的过期字段
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)

@router.post("/login", response_model=TokenResponse)
async def login(
    req: LoginRequest,                            # 请求体自动解析为 LoginRequest
    request: Request,
    db: AsyncSession = Depends(get_db),           # 注入数据库会话
):
    # print(f'req-->{req}')
    # print(f'req-->{req.model_dump()}')
    """用户登录，返回 JWT Access Token（支持用户名或邮箱登录）。"""
    limit_auth_attempts(request, "login")
    settings = get_settings()

    # 查用户：用户名或邮箱都行（:val 同时匹配两列），参数化查询防注入（回顾 2.6）
    result = await db.execute(
        text(
            "SELECT id, password_hash, role, tenant_id, is_active "
            "FROM users WHERE lower(username) = :val OR lower(email) = :val LIMIT 2"
        ),
        {"val": req.username.strip().lower()},
    )
    rows = result.fetchall()
    row = rows[0] if len(rows) == 1 else None
    # print(f'row-->{row}')
    if not row:                                   # 用户不存在
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")

    if len(req.password.encode("utf-8")) > 72:
        raise HTTPException(401, "用户名或密码错误")
    if not row.is_active:                         # 账号被禁用
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已被禁用，请联系管理员")
    # 密码校验是 CPU 密集型（~100ms），用线程池避免阻塞事件循环（回顾 2.1.4）
    loop = asyncio.get_running_loop()
    password_ok = await loop.run_in_executor(
        None, pwd_context.verify, req.password, row.password_hash
    )
    if not password_ok:                           # 密码不对
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")

    # 校验通过，签发 Token（把用户身份装进去）
    token = _create_access_token(
        data={"sub": str(row.id), "role": row.role, "tenant_id": row.tenant_id},
        expires_minutes=settings.jwt_access_token_expire_minutes,
    )
    logger.info("auth.login_success", user_id=str(row.id), role=row.role)

    return TokenResponse(
        access_token=token,
        expires_in=settings.jwt_access_token_expire_minutes * 60,   # 分钟转秒
        role=row.role,
        user_id=str(row.id),
    )

@router.get("/me")
async def get_me(
    current_user: dict = Depends(get_current_user),   # 注入当前用户（顺带完成鉴权）
):
    """获取当前登录用户信息（用于验证 Token 是否有效）。"""
    return current_user
if __name__ == '__main__':
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    uvicorn.run(app, host="0.0.0.0", port=8000)
