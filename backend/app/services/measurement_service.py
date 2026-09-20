"""监测数据录入业务逻辑 (含超标自动判定与小时值日均自动汇总)."""
from flask import current_app

from ..domain import aggregation, exceedance_rules
from ..domain.standards import get_pollutant
from ..errors import ConflictError, NotFoundError, ValidationError
from ..extensions import db
from ..models import Exceedance, Measurement, Station


def get_measurement(measurement_id):
    measurement = db.session.get(Measurement, measurement_id)
    if measurement is None:
        raise NotFoundError("监测数据不存在: id=%s" % measurement_id)
    return measurement


def preview_entries(period, entries):
    """Dry-run evaluation for the entry form (no database writes)."""
    results = []
    for entry in entries:
        pollutant = str(entry.get("pollutant", "")).upper()
        meta = get_pollutant(pollutant)
        if meta is None:
            raise ValidationError("未知监测因子: %s" % entry.get("pollutant"), fields={"pollutant": "unknown"})
        try:
            value = float(entry.get("value"))
        except (TypeError, ValueError):
            raise ValidationError(
                "%s 监测值必须为数字" % meta["label"], fields={pollutant: "invalid_number"}
            )
        evaluation = exceedance_rules.evaluate(pollutant, period, value)
        results.append(
            {
                "pollutant": pollutant,
                "pollutant_label": meta["label"],
                "value": value,
                "unit": meta["unit"],
                **evaluation,
            }
        )
    return {"period": period, "results": results, "summary": exceedance_rules.summarize(results)}


def _load_station(station_id):
    station = db.session.get(Station, station_id)
    if station is None:
        raise NotFoundError("监测点不存在: id=%s" % station_id)
    return station


def _min_valid_hours():
    return int(
        current_app.config.get("DAILY_MIN_VALID_HOURS", aggregation.MIN_VALID_HOURLY_POINTS)
    )


def refresh_daily_aggregate(station_id, pollutant, measured_at):
    """Recompute the auto-aggregated daily mean for one station/pollutant/day.

    Invoked after hourly rows are written or removed. The daily row is only
    persisted when the day has enough valid hourly points (GB 3095-2012
    requires at least 20); generated rows participate in exceedance
    evaluation like any other measurement. When the day falls short, any
    previously auto-generated row is dropped and the day is reported as
    incomplete. Manually entered daily rows are never overwritten.
    """
    meta = get_pollutant(pollutant)
    start, end = aggregation.day_bounds(measured_at)
    min_hours = _min_valid_hours()

    hourly_values = [
        row.value
        for row in Measurement.query.filter(
            Measurement.station_id == station_id,
            Measurement.pollutant == pollutant,
            Measurement.period == "hourly",
            Measurement.measured_at >= start,
            Measurement.measured_at < end,
        ).all()
    ]
    stats = aggregation.aggregate_daily_mean(hourly_values, min_hours)
    if stats["value"] is not None and meta:
        stats["value"] = round(stats["value"], meta["precision"])

    result = {
        "pollutant": pollutant,
        "pollutant_label": meta["label"] if meta else pollutant,
        "unit": meta["unit"] if meta else None,
        **stats,
    }

    existing = Measurement.query.filter_by(
        station_id=station_id, pollutant=pollutant, period="daily", measured_at=start
    ).first()

    if not stats["complete"]:
        if existing is not None and existing.data_source == "aggregate":
            db.session.delete(existing)
            result["status"] = "removed"
            result["message"] = "有效小时数降至 %d/%d, 已撤销此前生成的日均值" % (
                stats["valid_hours"], min_hours,
            )
        else:
            result["status"] = "incomplete"
            result["message"] = "数据不完整: 当日有效小时数 %d/%d, 暂不生成日均值" % (
                stats["valid_hours"], min_hours,
            )
        return result

    if existing is not None and existing.data_source != "aggregate":
        result["status"] = "skipped"
        result["message"] = "当日已存在人工录入的日均值, 自动汇总未覆盖"
        return result

    evaluation = exceedance_rules.evaluate(pollutant, "daily", stats["value"])
    is_new = existing is None
    if is_new:
        existing = Measurement(
            station_id=station_id, pollutant=pollutant, period="daily", measured_at=start
        )
        db.session.add(existing)
    existing.value = stats["value"]
    existing.unit = meta["unit"] if meta else None
    existing.limit_value = evaluation["limit"]
    existing.exceed_ratio = evaluation["ratio"]
    existing.is_exceeded = evaluation["exceeded"]
    existing.data_source = "aggregate"
    existing.valid_hours = stats["valid_hours"]
    existing.recorder = None
    existing.remark = "小时值自动汇总(有效小时 %d/%d)" % (
        stats["valid_hours"], aggregation.HOURS_PER_DAY,
    )
    _sync_exceedance(existing, meta, evaluation)
    db.session.flush()

    result.update(
        {
            "status": "created" if is_new else "updated",
            "measurement_id": existing.id,
            "limit": evaluation["limit"],
            "is_exceeded": evaluation["exceeded"],
            "exceed_ratio": evaluation["ratio"],
            "message": "已%s日均值(有效小时 %d/%d)%s" % (
                "生成" if is_new else "更新",
                stats["valid_hours"],
                aggregation.HOURS_PER_DAY,
                ", 判定超标" if evaluation["exceeded"] else "",
            ),
        }
    )
    return result


def record_entries(station_id, measured_at, period, entries, data_source="manual",
                   recorder=None, remark=None, overwrite=False):
    """Persist one measured_at snapshot for a station.

    Duplicate (station, pollutant, period, measured_at) rows are reported back;
    when ``overwrite`` is true the existing row is refreshed instead.
    """
    station = _load_station(station_id)
    if not entries:
        raise ValidationError("至少需要录入一条监测数据", fields={"entries": "empty"})

    existing = {
        row.pollutant: row
        for row in Measurement.query.filter_by(
            station_id=station.id, period=period, measured_at=measured_at
        ).all()
    }

    created, updated, exceeded, duplicates, evaluated = [], [], [], [], []
    touched = []  # 本批次实际写入的因子, 用于触发当日日均自动汇总
    seen = set()
    for entry in entries:
        pollutant = str(entry.get("pollutant", "")).upper()
        meta = get_pollutant(pollutant)
        if meta is None:
            raise ValidationError(
                "未知监测因子: %s" % entry.get("pollutant"), fields={"pollutant": "unknown"}
            )
        if pollutant in seen:
            raise ValidationError(
                "%s 在同一时刻重复提交" % meta["label"], fields={pollutant: "duplicated_in_batch"}
            )
        seen.add(pollutant)

        try:
            value = float(entry.get("value"))
        except (TypeError, ValueError):
            raise ValidationError(
                "%s 监测值必须为数字" % meta["label"], fields={pollutant: "invalid_number"}
            )

        evaluation = exceedance_rules.evaluate(pollutant, period, value)
        evaluated.append(
            {
                "pollutant": pollutant,
                "pollutant_label": meta["label"],
                "value": value,
                "unit": meta["unit"],
                **evaluation,
            }
        )

        record = existing.get(pollutant)
        if record is not None and not overwrite:
            duplicates.append(
                {
                    "pollutant": pollutant,
                    "pollutant_label": meta["label"],
                    "value": value,
                    "existing_id": record.id,
                    "message": "该时刻 %s 数据已存在" % meta["label"],
                }
            )
            continue

        is_new = record is None
        if is_new:
            record = Measurement(station_id=station.id, pollutant=pollutant, period=period,
                                 measured_at=measured_at)
            db.session.add(record)

        record.value = value
        record.unit = meta["unit"]
        record.limit_value = evaluation["limit"]
        record.exceed_ratio = evaluation["ratio"]
        record.is_exceeded = evaluation["exceeded"]
        record.data_source = data_source
        record.recorder = entry.get("recorder") or recorder
        record.remark = entry.get("remark") or remark

        _sync_exceedance(record, meta, evaluation)
        db.session.flush()
        (created if is_new else updated).append(record.to_dict(include_station=True))
        touched.append(pollutant)
        if evaluation["exceeded"]:
            exceeded.append(record.exceedance.to_dict() if record.exceedance else None)

    if not created and not updated and duplicates:
        raise ConflictError(
            "所选时刻已存在相同数据, 如需覆盖请勾选\"覆盖已有数据\": %s"
            % ", ".join(item["pollutant_label"] for item in duplicates)
        )

    daily_aggregation = None
    if period == "hourly" and touched:
        items = [
            refresh_daily_aggregate(station.id, pollutant, measured_at)
            for pollutant in touched
        ]
        daily_aggregation = {
            "date": measured_at.date().isoformat(),
            "min_valid_hours": _min_valid_hours(),
            "generated_count": len([item for item in items if item["status"] in ("created", "updated")]),
            "incomplete_count": len([item for item in items if item["status"] in ("incomplete", "removed")]),
            "items": items,
        }

    db.session.commit()
    return {
        "station": station.to_option(),
        "measured_at": measured_at.isoformat(timespec="seconds"),
        "period": period,
        "created": created,
        "updated": updated,
        "exceedances": [item for item in exceeded if item],
        "duplicates": duplicates,
        "evaluations": evaluated,
        "daily_aggregation": daily_aggregation,
        "summary": {
            "created_count": len(created),
            "updated_count": len(updated),
            "exceeded_count": len([item for item in evaluated if item["exceeded"]]),
            "duplicate_count": len(duplicates),
        },
    }


def _sync_exceedance(record, meta, evaluation):
    """Create / refresh / drop the exceedance row attached to a measurement."""
    if evaluation["exceeded"]:
        if record.exceedance is None:
            record.exceedance = Exceedance(
                station_id=record.station_id,
                pollutant=record.pollutant,
                period=record.period,
                measured_at=record.measured_at,
                value=record.value,
                limit_value=evaluation["limit"],
                exceed_ratio=evaluation["ratio"],
                level=evaluation["level"],
                status="pending",
            )
        else:
            record.exceedance.value = record.value
            record.exceedance.limit_value = evaluation["limit"]
            record.exceedance.exceed_ratio = evaluation["ratio"]
            record.exceedance.level = evaluation["level"]
            record.exceedance.measured_at = record.measured_at
    elif record.exceedance is not None:
        db.session.delete(record.exceedance)


def delete_measurement(measurement):
    payload = measurement.to_dict()
    refresh_key = None
    if measurement.period == "hourly":
        refresh_key = (measurement.station_id, measurement.pollutant, measurement.measured_at)
    db.session.delete(measurement)
    if refresh_key:
        # 小时数据被删除后, 当日有效小时数可能跌破阈值, 需要重算日均值
        refresh_daily_aggregate(*refresh_key)
    db.session.commit()
    return payload
