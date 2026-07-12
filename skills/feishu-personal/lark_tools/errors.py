"""
errors.py - Custom error types for lark_cli
"""


class LarkCliError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = details

    def to_json(self):
        result = {'error': self.code, 'message': str(self)}
        if self.details:
            result['details'] = self.details
        return result
