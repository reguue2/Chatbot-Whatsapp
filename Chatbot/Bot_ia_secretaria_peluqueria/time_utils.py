# time_utils.py
from datetime import datetime, time, date, timedelta
from zoneinfo import ZoneInfo
from settings import settings

_FALLBACK_TZ = getattr(settings, "CAL_TZ", "Europe/Madrid")

def tz_of(pelu) -> str:
    return (getattr(pelu, "tz", None) or _FALLBACK_TZ) or "Europe/Madrid"

def now_local(pelu) -> datetime:
    return datetime.now(ZoneInfo(tz_of(pelu)))

def today_local(pelu) -> date:
    return now_local(pelu).date()

def local_dt_from_parts(pelu, d: date, t: time) -> datetime:
    return datetime(d.year, d.month, d.day, t.hour, t.minute, t.second, tzinfo=ZoneInfo(tz_of(pelu)))
