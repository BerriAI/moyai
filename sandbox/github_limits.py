"""Publication budgets shared by the sandbox packager and server validator."""
MAX_FILE = 10 * 1024 * 1024
MAX_TOTAL = 20 * 1024 * 1024
# JSON can expand a UTF-8 byte into a six-character escape, plus metadata.
MAX_PUBLICATION_BODY = 6 * MAX_TOTAL + 1024 * 1024
