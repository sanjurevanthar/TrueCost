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


class UnsupportedImageError(DomainError):
    status_code = 415

    def __init__(self, content_type, allowed):
        super().__init__(f"Unsupported file type '{content_type}'. Upload one of: {', '.join(sorted(allowed))}.")


class ImageTooLargeError(DomainError):
    status_code = 413

    def __init__(self, max_mb):
        super().__init__(f"File is too large (limit {max_mb} MB).")


class EmptyUploadError(DomainError):
    status_code = 422

    def __init__(self):
        super().__init__("The uploaded file is empty.")


class TranscriptionUnavailableError(DomainError):
    status_code = 503

    def __init__(self):
        super().__init__("Reading images needs a Gemini key: set GEMINI_API_KEY. You can still paste the note text.")


class TranscriptionFailedError(DomainError):
    status_code = 502

    def __init__(self, detail="could not read the image"):
        super().__init__(f"Could not read the note from the image ({detail}). Try a clearer photo or paste the text.")


class AIBudgetExceededError(DomainError):
    status_code = 503

    def __init__(self):
        super().__init__("The daily AI budget has been reached, so reading images is paused. Paste the note text instead.")
