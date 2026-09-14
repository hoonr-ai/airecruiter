from zoneinfo import ZoneInfo
from datetime import datetime

ny_tz = ZoneInfo("America/New_York")
utc_tz = ZoneInfo("UTC")

start_date = "2026-09-08"
dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=ny_tz)
utc_start = dt.astimezone(utc_tz).strftime("%Y-%m-%d %H:%M:%S+00")
print(f"Start: {utc_start}")

end_date = "2026-09-10"
dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=ny_tz)
utc_end = dt.astimezone(utc_tz).strftime("%Y-%m-%d %H:%M:%S+00")
print(f"End: {utc_end}")
