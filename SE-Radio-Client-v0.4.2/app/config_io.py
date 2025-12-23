import json
import time

def _now_ts():
    return int(time.time())

def load_user_config(path: str = "config_user.json"):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_user_config(data: dict, path: str = "config_user.json"):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass
