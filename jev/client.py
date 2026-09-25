"""TypeSafe SDK client initialization and invocation wrapper for Jev.
Provides a lazy-loaded TypeSafeClient singleton and a fail-open call wrapper
that raises JevUnavailable on any error (missing credentials, network failure,
timeout, API error, import failure). Never crashes into the hot path.
"""

import logging

logger = logging.getLogger(__name__)

# Module-level singleton; initialized on first get_client() call.
_client = None


class JevUnavailable(Exception):
    """Raised when the Jev/TypeSafe system is unavailable or cannot be used.
    Signals that the system should fall through to the current pipeline silently
    (logged at WARN by the caller). Never propagates up; always caught and handled.
    """


def get_client():
    """Return a lazily-initialized TypeSafeClient singleton.
    Reads TYPESAFE_API_KEY and TYPESAFE_MODEL from config; performs no network
    call at import time. Caches the client in the module-level _client global on
    first success and emits a logger.debug line. Raises JevUnavailable if
    credentials are missing, the SDK is not installed, or initialization fails.
    Returns:
        TypeSafeClient configured with the API key and model from config.
    Raises:
        JevUnavailable: If credentials are missing, the typesafe_sdk import
                       fails, or client initialization fails.
    """
    global _client

    if _client is not None:
        return _client

    try:
        from typesafe_sdk import RetryPolicy, TypeSafeClient

        import config

        api_key = config.TYPESAFE_API_KEY
        if not api_key or api_key == "":
            raise JevUnavailable(
                "TYPESAFE_API_KEY is empty; Jev cannot initialize. Set TYPESAFE_API_KEY in .env"
            )

        model = config.TYPESAFE_MODEL
        _client = TypeSafeClient(
            api_key=api_key,
            model=model,
            timeout=config.JEV_TIMEOUT_SECONDS,
            retry=RetryPolicy(timeout=config.JEV_TIMEOUT_SECONDS),
        )
        logger.debug(f"Jev client initialized with model={model}")
        return _client

    except JevUnavailable:
        raise
    except ImportError as e:
        raise JevUnavailable(f"TypeSafe SDK import failed: {e}")
    except Exception as e:
        raise JevUnavailable(f"Jev client initialization failed: {e}")


def call(state: dict, questions: dict) -> dict:
    """Invoke the TypeSafe system_one API synchronously over the network.
    Calls the Jev client's system_one method with the given state and questions.
    On any error (client unavailable, API error, timeout, credentials failure,
    rate limiting), raises JevUnavailable. Never crashes into the hot path.
    Args:
        state: The context dict passed to Jev (project brief, query, passages, etc.)
        questions: A dict mapping question names to TypeSafe question types
                  (Noul, Choice, Score, etc.)
    Returns:
        dict: The raw system_one response namespace, taken from response.__dict__
              (or dict(response) if not dict-like). Each answer is a typed object
              (NoulResponse, ChoiceResponse, ScoreResponse, etc.) with
              score/noul/choice attributes.
    Raises:
        JevUnavailable: If the client is unavailable, the API call fails,
                       times out, rate-limits, or any other error occurs.
    """
    try:

        client = get_client()

        # Invoke system_one with the configured model (can be overridden at call time).
        response = client.system_one(state=state, questions=questions)

        # The response is a SystemOneResponse or dict-like object.
        # Extract the raw answers dict keyed by question name.
        if hasattr(response, '__dict__'):
            return response.__dict__
        return dict(response)

    except JevUnavailable:
        raise
    except TimeoutError as e:
        raise JevUnavailable(f"Jev call timed out: {e}")
    except Exception as e:
        raise JevUnavailable(f"Jev system_one call failed: {e}")
