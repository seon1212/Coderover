"""A simple hello world function."""


def hello(name: str = "World") -> str:
    """Return a friendly greeting.

    Args:
        name: The name to greet. Defaults to "World".

    Returns:
        A greeting string.
    """
    return f"Hello, {name}!"
