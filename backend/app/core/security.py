"""密码哈希与 JWT 签发/校验。

这个文件回答两个问题：
    密码怎么安全地存？  → hash_password / verify_password
    登录后怎么证明身份？ → create_access_token / decode_access_token
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.core.config import settings
from app.core.exceptions import UnauthorizedError

# bcrypt 只处理前 72 字节，超出的部分【静默忽略】。
# 不校验的话，"同一个密码的后 20 位"会被当成同一个密码。
BCRYPT_MAX_BYTES = 72


# ══════════════════════════════════════════════════════════════
# 密码
# ══════════════════════════════════════════════════════════════


def validate_password_length(password: str) -> None:
    """检查密码字节长度是否超出 bcrypt 上限。

    注意算的是【字节】不是【字符】：一个中文字符占 3 字节，
    所以 25 个汉字就会超过 72。
    """
    if len(password.encode("utf-8")) > BCRYPT_MAX_BYTES:
        raise ValueError(f"密码过长（最多 {BCRYPT_MAX_BYTES} 字节，中文约 24 个字）")


def hash_password(plain_password: str) -> str:
    """把明文密码变成哈希串。

    bcrypt 每次都会生成一个随机"盐"（salt）混进结果，所以：
        同一个密码，两次 hash 出来的字符串【不一样】
    这是对的 —— 攻击者无法通过"比对哈希是否相同"来批量猜密码。

    返回的是 60 字符的字符串，形如：
        $2b$12$N9qo8uLOickgx2ZMRZoMyeIjZAgcfl7p92ldGxad68LJZdL17lhWy
         └┬┘└┬┘└──────────┬──────────┘└────────────┬────────────┘
        算法 代价      盐(22字符)             哈希值(31字符)
    """
    validate_password_length(plain_password)
    return bcrypt.hashpw(
        plain_password.encode("utf-8"),
        # 代价因子 12：算一次约 0.1~0.3 秒。
        # 对正常登录无所谓，但让暴力破解变得不可行。
        bcrypt.gensalt(rounds=12),
    ).decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    """校验明文密码是否匹配哈希串。

    bcrypt 会从 password_hash 里把当初用的盐读出来，
    用同样的方式再算一遍，然后比对。
    """
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"),
            password_hash.encode("utf-8"),
        )
    except ValueError:
        # 数据库里的哈希串格式不对（比如被手工改过）。
        # 这种情况视为"密码不匹配"，而不是抛异常 ——
        # 否则一个脏数据就能让登录接口变成 500。
        return False


# ══════════════════════════════════════════════════════════════
# JWT
# ══════════════════════════════════════════════════════════════

# JWT 结构（用 . 分成三段）：
#   header.payload.signature
#
#   header    {"alg":"HS256","typ":"JWT"}          —— 用什么算法签的
#   payload   {"sub":"3","role":"admin","exp":...} —— 真正装的东西
#   signature 用 SECRET_KEY 算出来的签名          —— 防篡改
#
# 关键认知：payload 只是 Base64 编码，【不是加密】，任何人都能解开看。
# 所以绝不能往里面放密码。它的作用是"防篡改"而不是"保密"。


def create_access_token(user_id: int, role: str) -> tuple[str, int]:
    """签发 access token。

    Returns:
        (token 字符串, 有效期秒数)
    """
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    payload = {
        # sub (subject) 是 JWT 标准字段，放"这个 token 属于谁"
        # 注意 JWT 规范要求 sub 是字符串，所以这里转成 str
        "sub": str(user_id),
        "role": role,
        # ★ jti (JWT ID) —— token 的唯一标识
        #
        # 为什么需要它？因为 JWT 是无状态的，服务端收不回已签发的 token。
        # 用户登出后，如果有人之前复制了这个 token，在过期前依然能用。
        #
        # 有了 jti，就可以在登出时把这一串写进 Redis 黑名单，
        # 每次鉴权时查一下 —— 这样"登出"才真正生效。
        # 详见 app/core/redis.py 的说明。
        "jti": uuid.uuid4().hex,
        # iat (issued at) 签发时间
        "iat": now,
        # exp (expiration) 过期时间 —— PyJWT 会在解码时自动校验
        "exp": expire,
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return token, settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60


def get_token_remaining_seconds(payload: dict) -> int:
    """算出这个 token 还有多少秒过期。

    登出时写黑名单要用这个值当 TTL —— 设成"剩余有效期"的好处是
    黑名单会自动清理：token 一旦自然过期，黑名单里那条也就没意义了，
    Redis 会自动删掉，不需要额外的清理任务。
    """
    exp = payload.get("exp")
    if exp is None:
        return 0
    remaining = int(exp - datetime.now(timezone.utc).timestamp())
    return max(0, remaining)


def decode_access_token(token: str) -> dict:
    """解码并校验 token。

    校验三件事：签名对不对、有没有过期、格式是否正确。

    Raises:
        UnauthorizedError: 任何一种校验失败。
        注意返回给前端的 message 都是模糊的 —— 不告诉攻击者
        "是签名错了"还是"过期了"，那等于帮他调试。
    """
    try:
        return jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("登录已过期，请重新登录") from exc
    except jwt.InvalidTokenError as exc:
        # 签名不对 / 格式不对 / 算法不对，都归为这一类
        raise UnauthorizedError("登录凭证无效") from exc
