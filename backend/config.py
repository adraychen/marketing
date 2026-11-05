import os

from dotenv import load_dotenv

load_dotenv()

MODE = os.getenv("DASHBOARD_MODE", "demo")
if MODE not in {"demo", "live"}:
    raise RuntimeError("DASHBOARD_MODE must be demo or live.")

def required(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Configure {name} on the backend.")
    return value
