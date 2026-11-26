"""离线测试替身：redis 的最小子集，队列调用静默成功。"""


class RedisError(Exception):
    pass


class _Redis:
    def __init__(self, *args, **kwargs):
        self.items = []

    @classmethod
    def from_url(cls, url, *args, **kwargs):
        return cls(url)

    def ping(self):
        return True

    def pipeline(self):
        return _Pipeline(self)

    def brpop(self, key, timeout=0):
        return None

    def lpush(self, key, value):
        self.items.append(value)

    def ltrim(self, key, start, end):
        del self.items[: max(0, start)]


class _Pipeline:
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def lpush(self, key, value):
        self.client.lpush(key, value)
        return self

    def ltrim(self, key, start, end):
        self.client.ltrim(key, start, end)
        return self

    def execute(self):
        return []


Redis = _Redis
