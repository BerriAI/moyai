"""Sandbox providers. The handle operations retain Modal's async SDK shape."""
from .modal import ModalProvider


def provider(settings, name=None, *, modal_clients=None):
    name = name or settings.sandbox_provider
    if name == 'modal':
        return ModalProvider(settings, modal_clients)
    if name == 'substrate':
        from .substrate import SubstrateProvider
        return SubstrateProvider(settings)
    raise ValueError('Unknown sandbox provider')


def provider_for_id(identity):
    return 'substrate' if identity.startswith('substrate:') else 'modal'
