"""离线测试替身：jwt 的最小子集，令牌即账号 id 的字符串。"""


class InvalidTokenError(Exception):
    pass


def encode(payload, secret, algorithm=None):
    return str(payload.get("sub", ""))


def decode(token, secret, algorithms=None):
    try:
        return {"sub": int(token)}
    except ValueError as error:
        raise InvalidTokenError(str(error)) from error
