"""离线测试替身：fastapi.security 的最小子集。"""


class HTTPAuthorizationCredentials:
    def __init__(self, token):
        self.credentials = token


class HTTPBearer:
    def __init__(self, auto_error=True):
        self.auto_error = auto_error


def Depends(callable_or_value):
    return callable_or_value
