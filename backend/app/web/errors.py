"""Transport errors detached from application persistence."""
class WebError(Exception):
    def __init__(self, code: str, message: str, scope: str = 'page', *, retryable: bool = False,
                 retry_after_s: int | None = None, upstream_status: int | None = None,
                 details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.scope = code, message, scope
        self.retryable, self.retry_after_s = retryable, retry_after_s
        self.upstream_status, self.details = upstream_status, details or {}

    def to_dict(self) -> dict:
        return {key: getattr(self, key) for key in ('code', 'message', 'scope', 'retryable',
                'retry_after_s', 'upstream_status', 'details')}
