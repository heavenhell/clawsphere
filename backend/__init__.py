from dotenv import load_dotenv

load_dotenv()

__all__ = ["repo", "runtime_config"]


def __getattr__(name: str):
    """Keep convenience exports without loading Agent credentials eagerly."""
    if name == "repo":
        from backend.providers import repo

        return repo
    if name == "runtime_config":
        from backend.providers import runtime_config

        return runtime_config
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
