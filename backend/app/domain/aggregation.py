"""小时值 -> 日均值自动汇总规则 (GB 3095-2012 数据有效性要求)."""
from datetime import datetime, timedelta

# GB 3095-2012: 24 小时平均需每日至少 20 个小时平均浓度值, 否则当日日均数据无效
MIN_VALID_HOURLY_POINTS = 20
HOURS_PER_DAY = 24


def day_bounds(moment):
    """Return the [start, end) datetimes of the natural day containing ``moment``."""
    start = datetime(moment.year, moment.month, moment.day)
    return start, start + timedelta(days=1)


def aggregate_daily_mean(values, min_hours=MIN_VALID_HOURLY_POINTS):
    """Aggregate one day of hourly readings into a daily mean.

    Returns ``{"valid_hours", "required_hours", "complete", "value"}``.
    ``value`` is the arithmetic mean of the valid hourly readings and is also
    computed for incomplete days so callers can show it as a reference;
    ``complete`` tells whether the day satisfies the minimum valid-hours
    requirement, i.e. whether the mean may be persisted as an official
    daily value and take part in exceedance evaluation.
    """
    valid = [float(item) for item in values if item is not None]
    count = len(valid)
    mean = round(sum(valid) / count, 3) if count else None
    return {
        "valid_hours": count,
        "required_hours": min_hours,
        "complete": count >= min_hours,
        "value": mean,
    }
