"""离线测试替身：fastapi 的最小子集，提供异常类型与可注册路由的 APIRouter。"""


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def Depends(callable_or_value):
    return callable_or_value


class Request:
    def __init__(self, scope=None):
        self.scope = scope or {}


def Query(default=None, **kwargs):
    return default


class _Route:
    def __init__(self, path, endpoint, methods):
        self.path = path
        self.endpoint = endpoint
        self.methods = set(methods)


class APIRouter:
    def __init__(self, prefix=""):
        self.prefix = prefix
        self.routes = []

    def _register(self, path, methods):
        def decorator(endpoint):
            self.routes.append(_Route(self.prefix + path, endpoint, methods))
            return endpoint

        return decorator

    def get(self, path, **kwargs):
        return self._register(path, ["GET"])

    def post(self, path, **kwargs):
        return self._register(path, ["POST"])

    def put(self, path, **kwargs):
        return self._register(path, ["PUT"])

    def delete(self, path, **kwargs):
        return self._register(path, ["DELETE"])

    def include_router(self, router):
        for route in router.routes:
            self.routes.append(_Route(self.prefix + route.path, route.endpoint, route.methods))


class FastAPI(APIRouter):
    def __init__(self, title="", lifespan=None, **kwargs):
        super().__init__()
        self.title = title
        self.lifespan = lifespan
        self.user_middleware = []
        self.exception_handlers = {}

    def add_middleware(self, middleware, **kwargs):
        self.user_middleware.append(middleware)

    def middleware(self, kind):
        def decorator(endpoint):
            self.user_middleware.append((kind, endpoint))
            return endpoint

        return decorator

    def add_exception_handler(self, exception, handler):
        self.exception_handlers[exception] = handler
