class DaqError(Exception):
    def __init__(self, message, error_code=-200000, task_name=""):
        super().__init__(message)
        self.error_code = error_code


class DaqWarning(Warning):
    pass
