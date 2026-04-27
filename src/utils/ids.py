import hashlib


def safe_suffix_from_cal_id(calendar_id: str) -> str:
    return hashlib.sha1(calendar_id.encode("utf-8")).hexdigest()


def sync_secret_id_for(calendar_id: str) -> str:
    return f"cal-sync-{safe_suffix_from_cal_id(calendar_id)}"
