class ConnectorError(Exception):
    """Safe provider failure metadata; never include credentials or raw payloads."""
    def __init__(self, message, *, code='provider_error', dispatched=None,
                 retryable=False, status_code=None, retry_after=None, rejected=False):
        super().__init__(message)
        self.code, self.dispatched = code, dispatched
        self.retryable, self.status_code, self.retry_after = retryable, status_code, retry_after
        self.rejected = rejected

    def receipt(self, *, write=False):
        uncertain = bool(write and self.dispatched is not False and not self.rejected)
        return {'error': str(self), 'code': self.code, 'dispatched': self.dispatched,
                'outcome': 'unknown' if uncertain else 'rejected' if self.rejected or self.dispatched is False else 'failed',
                'outcome_uncertain': uncertain, 'retryable': bool(self.retryable and not uncertain),
                'status_code': self.status_code, 'retry_after_seconds': self.retry_after,
                'instruction': 'Verify the destination before retrying a write.' if uncertain else
                               'Correct the reported condition before retrying; no uncertain write needs replay.'}
