class ConnectorError(Exception):
    pass


class DefiniteConnectorError(ConnectorError):
    """The provider confirmed the request failed; nothing was created."""
    definite = True
