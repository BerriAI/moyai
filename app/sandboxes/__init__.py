"""Sandbox providers. The handle operations retain Modal's async SDK shape."""
from .modal import ModalProvider


class ProvisioningTerminated(Exception):
    """A named startup VM is confirmed terminated or absent, before handoff."""


def provider(settings, name=None, *, modal_clients=None):
    name = name or settings.sandbox_provider
    if name == 'modal':
        return ModalProvider(settings, modal_clients)
    if name == 'substrate':
        from .substrate import SubstrateProvider
        return SubstrateProvider(settings)
    if name == 'lambda':
        from .lambda_microvm import LambdaProvider
        return LambdaProvider(settings)
    raise ValueError('Unknown sandbox provider')


def provider_for_id(identity):
    if identity.startswith('lambda:'):
        return 'lambda'
    return 'substrate' if identity.startswith('substrate:') else 'modal'
