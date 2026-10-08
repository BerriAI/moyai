"""Credential-free payload service double shared by consumer contract tests."""
import hashlib

from fastapi import HTTPException


class MemoryObjects:
    enabled = True

    def __init__(self):
        self.values = {}
        self.fail = False
        self.reads = 0

    def put(self, raw):
        if self.fail:
            raise HTTPException(503, 'Test object service unavailable')
        reference = 'test:' + hashlib.sha256(raw).hexdigest()
        self.values[reference] = raw
        return reference

    def read(self, reference, limit):
        self.reads += 1
        if self.fail or reference not in self.values:
            raise HTTPException(503, 'Test object service unavailable')
        raw = self.values[reference]
        assert len(raw) <= limit
        return raw

    def put_file(self, path):
        return self.put(path.read_bytes())

    def verify(self, reference, expected_size):
        raw = self.read(reference, expected_size)
        assert len(raw) == expected_size
        assert reference == 'test:' + hashlib.sha256(raw).hexdigest()

    def close(self):
        pass
