"""Definitive account-tool failures, safe to return without provider secrets."""

class ProviderRejected(ValueError):
    def __init__(self,status,code,provider='Google'):
        self.status,self.code=status,code
        super().__init__(f'{provider} rejected the operation ({status}: {code})')


class InvalidInput(ValueError):
    pass
