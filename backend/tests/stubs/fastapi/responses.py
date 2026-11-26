"""离线测试替身：fastapi.responses 的最小子集。"""


class Response:
    def __init__(self, content="", media_type=None, headers=None):
        self.body = content
        self.media_type = media_type
        self.headers = headers or {}


class JSONResponse(Response):
    def __init__(self, content, status_code=200, headers=None):
        super().__init__(content, media_type="application/json", headers=headers)
        self.status_code = status_code
