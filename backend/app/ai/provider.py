"""The AI layer: ONE swappable function, ``extract(text, document_type) -> dict``.

There is deliberately no agent, no tools, no memory and no planning here. A
provider receives text, returns JSON, and nothing else. Everything a provider
returns is treated as untrusted input and forced through the Pydantic schema.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import Settings


class AIProviderError(RuntimeError):
    """Raised when a provider cannot produce output (network, timeout, bad JSON)."""


class AIProvider(ABC):
    """Minimal interface: text in, dict out."""

    #: short identifier stored on documents/runs (e.g. "mock")
    name: str = "provider"

    @property
    def model_name(self) -> str:
        """Human-readable model identifier (mock providers report a fixed name)."""
        return self.name

    @abstractmethod
    def extract(
        self,
        text: str,
        document_type: str,
        validation_error: str | None = None,
    ) -> dict:
        """Extract structured fields from ``text``.

        ``validation_error`` carries the schema error from a previous attempt so
        the provider can repair its output. Providers that cannot use it may
        ignore it - the pipeline allows at most one retry.
        """


def get_provider(settings: Settings) -> AIProvider:
    """Build the configured provider. Defaults to the free, deterministic mock."""
    if settings.ai_provider == "openai_compatible":
        from .openai_provider import OpenAICompatibleProvider

        return OpenAICompatibleProvider(settings)
    from .mock_provider import MockAIProvider

    return MockAIProvider()
