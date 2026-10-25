class AppError(Exception):
    def __init__(self, code, message, status=422, details=None, retryable=False, *,
                 retry_after_s=None, upstream_status=None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.details, self.retryable = details or {}, retryable
        self.retry_after_s, self.upstream_status = retry_after_s, upstream_status

    def body(self):
        result = dict(code=self.code, message=self.message, details=self.details, retryable=self.retryable)
        for key in ('retry_after_s', 'upstream_status'):
            if getattr(self, key) is not None:
                result[key] = getattr(self, key)
        return result
