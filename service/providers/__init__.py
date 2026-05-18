from service.providers.base import Provider, ProviderCapabilities

# Registry: name → Provider class. Add new providers here.
_REGISTRY: dict[str, type[Provider]] = {}


def register(cls: type[Provider]) -> type[Provider]:
    _REGISTRY[cls.name] = cls
    return cls


def get(name: str) -> type[Provider]:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"Unknown provider: {name!r}. Registered: {list(_REGISTRY)}")


def all_providers() -> list[type[Provider]]:
    return list(_REGISTRY.values())


__all__ = ["Provider", "ProviderCapabilities", "register", "get", "all_providers"]
