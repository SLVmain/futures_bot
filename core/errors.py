class BitunixError(Exception):
    """Base error for the Bitunix integration."""


class BitunixTransportError(BitunixError):
    """The request could not be completed at the HTTP transport layer."""


class BitunixHTTPError(BitunixError):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Bitunix HTTP {status_code}: {message}")


class BitunixResponseError(BitunixError):
    """Bitunix returned an invalid or unexpected response."""


class BitunixAPIError(BitunixError):
    def __init__(self, code, message: str, data=None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"Bitunix API error {code}: {message}")
