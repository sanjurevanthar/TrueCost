"""Domain exceptions. The API layer maps these to HTTP status codes in one place (main.py)."""


class DomainError(Exception):
    status_code = 400


class UnknownCodeError(DomainError):
    status_code = 404

    def __init__(self, codes):
        self.codes = list(codes)
        super().__init__(f"Unknown or unsupported code(s): {', '.join(self.codes)}")


class InvalidInsuranceError(DomainError):
    status_code = 422


class MissingInsuranceError(DomainError):
    status_code = 422

    def __init__(self):
        super().__init__("Insurance details are required when mode is 'insured'.")
