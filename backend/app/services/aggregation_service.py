"""小时值自动汇总: 按自然日把小时均值聚合为日均值.

规则 (依据 GB 3095-2012 对 24 小时平均的有效性要求):
- 按因子统计当日有效小时数, 同一小时内多条记录先取平均, 计 1 个有效小时;
- 有效小时数达到 DAILY_MIN_VALID_HOURS 视为数据完整, 不足则生成结果并标注
  ``is_complete=False`` (数据不完整);
- 当日某因子已无任何小时数据时, 撤销对应自动生成的日均值;
- 已存在手工/设备/导入来源的日均值时不覆盖, 标记为 skipped;
- 生成的日均值同样参与超标判定与统计查询.
"""
from datetime import datetime, time

from flask import current_app

from ..domain import exceedance_rules
from ..domain.constants import DATA_SOURCE_LABELS
from ..domain.standards import get_pollutant
from ..errors import NotFoundError
from ..extensions import db
from ..models import Measurement, Station
from .exceedance_service import sync_exceedance

AUTO_DATA_SOURCE = "auto"
AUTO_RECORDER = "系统自动"
AUTO_REMARK = "小时值自动汇总"

SUMMARY_KEYS = ("created", "updated", "removed", "skipped", "incomplete", "exceeded")


def _day_range(day):
    return datetime.combine(day, time.min), datetime.combine(day, time.max)


def _hourly_groups(station_id, day):
    """当天小时数据按 因子 -> 小时 分组."""
    start, end = _day_range(day)
    rows = (
        Measurement.query.filter(
            Measurement.station_id == station_id,
            Measurement.period == "hourly",
            Measurement.measured_at >= start,
            Measurement.measured_at <= end,
        )
        .order_by(Measurement.measured_at.asc())
        .all()
    )
    groups = {}
    for row in rows:
        groups.setdefault(row.pollutant, {}).setdefault(row.measured_at.hour, []).append(row.value)
    return groups


def _item(meta, status, valid_hours, required, record=None, message=None):
    item = {
        "pollutant": meta["code"],
        "pollutant_label": meta["label"],
        "unit": meta["unit"],
        "status": status,
        "valid_hours": valid_hours,
        "required_hours": required,
        "is_complete": None,
        "value": None,
        "is_exceeded": False,
        "exceed_ratio": None,
        "measurement_id": None,
        "message": message,
    }
    if record is not None:
        item["value"] = record.value
        item["is_exceeded"] = bool(record.is_exceeded)
        item["exceed_ratio"] = record.exceed_ratio
        item["measurement_id"] = record.id
        if record.data_source == AUTO_DATA_SOURCE:
            item["is_complete"] = record.is_complete
    return item


def aggregate_daily(station_id, day, commit=True):
    """把监测点某一天的小时均值汇总为日均值 (存在则刷新, 缺失则生成)."""
    station = db.session.get(Station, station_id)
    if station is None:
        raise NotFoundError("监测点不存在: id=%s" % station_id)

    required = current_app.config["DAILY_MIN_VALID_HOURS"]
    start, end = _day_range(day)
    groups = _hourly_groups(station.id, day)
    existing = {
        row.pollutant: row
        for row in Measurement.query.filter(
            Measurement.station_id == station.id,
            Measurement.period == "daily",
            Measurement.measured_at >= start,
            Measurement.measured_at <= end,
        ).all()
    }

    items = []
    counters = {key: 0 for key in SUMMARY_KEYS}
    for pollutant in sorted(set(groups) | set(existing)):
        meta = get_pollutant(pollutant)
        if meta is None:  # pragma: no cover - 历史脏数据兜底
            continue
        hours = groups.get(pollutant, {})
        valid_hours = len(hours)
        record = existing.get(pollutant)

        if valid_hours == 0:
            if record is not None and record.data_source == AUTO_DATA_SOURCE:
                db.session.delete(record)  # 关联超标记录随级联删除
                counters["removed"] += 1
                items.append(
                    _item(meta, "removed", 0, required, message="当日已无有效小时数据, 自动日均值已移除")
                )
            continue

        if record is not None and record.data_source != AUTO_DATA_SOURCE:
            counters["skipped"] += 1
            items.append(
                _item(
                    meta,
                    "skipped",
                    valid_hours,
                    required,
                    record=record,
                    message="已存在%s的日均值, 不覆盖"
                    % DATA_SOURCE_LABELS.get(record.data_source, record.data_source),
                )
            )
            continue

        # 同一小时内多条记录先平均, 再对各小时取算术平均
        value = round(
            sum(sum(values) / len(values) for values in hours.values()) / valid_hours,
            meta["precision"],
        )
        is_complete = valid_hours >= required
        evaluation = exceedance_rules.evaluate(pollutant, "daily", value)

        is_new = record is None
        if is_new:
            record = Measurement(
                station_id=station.id, pollutant=pollutant, period="daily", measured_at=start
            )
            db.session.add(record)
        record.value = value
        record.unit = meta["unit"]
        record.limit_value = evaluation["limit"]
        record.exceed_ratio = evaluation["ratio"]
        record.is_exceeded = evaluation["exceeded"]
        record.data_source = AUTO_DATA_SOURCE
        record.recorder = AUTO_RECORDER
        record.remark = AUTO_REMARK
        record.valid_hours = valid_hours
        record.is_complete = is_complete
        sync_exceedance(record, meta, evaluation)
        db.session.flush()

        counters["created" if is_new else "updated"] += 1
        if not is_complete:
            counters["incomplete"] += 1
        if evaluation["exceeded"]:
            counters["exceeded"] += 1
        items.append(
            _item(meta, "created" if is_new else "updated", valid_hours, required, record=record)
        )

    if commit:
        db.session.commit()
    return {
        "station": station.to_option(),
        "date": day.isoformat(),
        "required_hours": required,
        "items": items,
        "summary": counters,
    }


def aggregate_for_date(day, station_id=None):
    """按日批量汇总: 缺省处理当天所有有小时数据的监测点, 用于补算."""
    if station_id is not None:
        station_ids = [station_id]
    else:
        start, end = _day_range(day)
        station_ids = [
            row[0]
            for row in db.session.query(Measurement.station_id)
            .filter(
                Measurement.period == "hourly",
                Measurement.measured_at >= start,
                Measurement.measured_at <= end,
            )
            .distinct()
            .all()
        ]

    results = [aggregate_daily(sid, day, commit=False) for sid in station_ids]
    db.session.commit()

    summary = {"stations": len(results)}
    summary.update({key: 0 for key in SUMMARY_KEYS})
    for result in results:
        for key in SUMMARY_KEYS:
            summary[key] += result["summary"][key]
    return {
        "date": day.isoformat(),
        "required_hours": current_app.config["DAILY_MIN_VALID_HOURS"],
        "results": results,
        "summary": summary,
    }
