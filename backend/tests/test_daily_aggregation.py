"""小时值自动汇总日均值: 领域规则 + 录入联动 + 统计查询测试."""
from datetime import datetime

from app.domain import aggregation
from app.models import Exceedance, Measurement

DAY = datetime(2026, 9, 10)


def _post_hourly(client, station_id, moment, entries, **overrides):
    payload = {
        "station_id": station_id,
        "measured_at": moment.strftime("%Y-%m-%d %H:%M"),
        "period": "hourly",
        "data_source": "manual",
        "recorder": "测试员",
        "entries": entries,
    }
    payload.update(overrides)
    return client.post("/api/measurements/entries", json=payload)


def _fill_hours(client, station_id, day, hours, value=60.0, pollutant="PM25"):
    """逐小时录入同一因子, 返回最后一次提交的响应体."""
    body = None
    for hour in hours:
        response = _post_hourly(
            client, station_id, day.replace(hour=hour),
            [{"pollutant": pollutant, "value": value}],
        )
        assert response.status_code == 201
        body = response.get_json()
    return body


def _daily_record(station_id, pollutant="PM25"):
    return Measurement.query.filter_by(
        station_id=station_id, pollutant=pollutant, period="daily"
    ).first()


# ---- 领域规则 ----------------------------------------------------------


def test_aggregate_daily_mean_requires_enough_valid_hours():
    stats = aggregation.aggregate_daily_mean([50.0] * 20)
    assert stats == {"valid_hours": 20, "required_hours": 20, "complete": True, "value": 50.0}

    short = aggregation.aggregate_daily_mean([50.0] * 19)
    assert short["complete"] is False
    assert short["valid_hours"] == 19
    assert short["value"] == 50.0  # 参考均值照常计算, 仅供展示

    empty = aggregation.aggregate_daily_mean([])
    assert empty["complete"] is False
    assert empty["valid_hours"] == 0
    assert empty["value"] is None


def test_aggregate_daily_mean_ignores_none_and_supports_custom_threshold():
    stats = aggregation.aggregate_daily_mean([10.0, None, 30.0, None], min_hours=2)
    assert stats["valid_hours"] == 2
    assert stats["complete"] is True
    assert stats["value"] == 20.0


def test_day_bounds_cover_the_natural_day():
    start, end = aggregation.day_bounds(datetime(2026, 9, 10, 15, 45))
    assert start == datetime(2026, 9, 10, 0, 0)
    assert end == datetime(2026, 9, 11, 0, 0)


# ---- 录入联动 ----------------------------------------------------------


def test_daily_aggregate_generated_once_enough_hours(client, station):
    body = _fill_hours(client, station.id, DAY, range(20), value=60.0)
    aggregation = body["daily_aggregation"]
    assert aggregation["date"] == "2026-09-10"
    assert aggregation["min_valid_hours"] == 20
    assert aggregation["generated_count"] == 1
    item = aggregation["items"][0]
    assert item["status"] == "created"
    assert item["valid_hours"] == 20
    assert item["value"] == 60.0
    assert item["is_exceeded"] is False

    record = _daily_record(station.id)
    assert record is not None
    assert record.measured_at == datetime(2026, 9, 10, 0, 0)
    assert record.value == 60.0
    assert record.data_source == "aggregate"
    assert record.valid_hours == 20
    assert record.limit_value == 75.0


def test_incomplete_day_is_flagged_and_not_persisted(client, station):
    body = _fill_hours(client, station.id, DAY, range(5), value=60.0)
    item = body["daily_aggregation"]["items"][0]
    assert item["status"] == "incomplete"
    assert item["complete"] is False
    assert item["valid_hours"] == 5
    assert item["required_hours"] == 20
    assert "数据不完整" in item["message"]
    assert body["daily_aggregation"]["incomplete_count"] == 1
    assert _daily_record(station.id) is None


def test_daily_aggregate_created_when_hours_reach_threshold(client, station):
    _fill_hours(client, station.id, DAY, range(19), value=60.0)
    assert _daily_record(station.id) is None

    body = _fill_hours(client, station.id, DAY, [19], value=80.0)
    item = body["daily_aggregation"]["items"][0]
    assert item["status"] == "created"
    assert item["valid_hours"] == 20
    assert item["value"] == 61.0  # (19 * 60 + 80) / 20
    assert _daily_record(station.id).value == 61.0


def test_daily_aggregate_participates_in_exceedance(client, station):
    body = _fill_hours(client, station.id, DAY, range(20), value=90.0)
    item = body["daily_aggregation"]["items"][0]
    assert item["is_exceeded"] is True
    assert item["limit"] == 75.0

    record = _daily_record(station.id)
    assert record.is_exceeded is True
    exceedance = Exceedance.query.filter_by(measurement_id=record.id).one()
    assert exceedance.period == "daily"
    assert exceedance.status == "pending"
    assert exceedance.limit_value == 75.0


def test_daily_aggregate_updates_when_hourly_overwritten(client, station):
    _fill_hours(client, station.id, DAY, range(20), value=60.0)
    assert _daily_record(station.id).value == 60.0

    response = _post_hourly(
        client, station.id, DAY.replace(hour=0),
        [{"pollutant": "PM25", "value": 160.0}], overwrite=True,
    )
    assert response.status_code == 201
    item = response.get_json()["daily_aggregation"]["items"][0]
    assert item["status"] == "updated"
    assert item["value"] == 65.0  # (19 * 60 + 160) / 20
    assert _daily_record(station.id).value == 65.0


def test_daily_aggregate_removed_when_hours_drop_below_threshold(client, station):
    _fill_hours(client, station.id, DAY, range(20), value=60.0)
    assert _daily_record(station.id) is not None

    hourly = Measurement.query.filter_by(period="hourly").first()
    response = client.delete("/api/measurements/%d" % hourly.id)
    assert response.status_code == 200
    assert _daily_record(station.id) is None


def test_manual_daily_value_is_never_overwritten(client, station, entry_payload):
    response = client.post(
        "/api/measurements/entries",
        json=entry_payload(
            station.id,
            measured_at="2026-09-10 00:00",
            period="daily",
            entries=[{"pollutant": "PM25", "value": 55.0}],
        ),
    )
    assert response.status_code == 201

    body = _fill_hours(client, station.id, DAY, range(20), value=90.0)
    item = body["daily_aggregation"]["items"][0]
    assert item["status"] == "skipped"
    assert "人工录入" in item["message"]

    record = _daily_record(station.id)
    assert record.value == 55.0
    assert record.data_source == "manual"
    assert record.valid_hours is None


# ---- 统计查询 ----------------------------------------------------------


def test_aggregated_daily_values_join_query_and_statistics(client, station):
    _fill_hours(client, station.id, DAY, range(20), value=90.0)

    listing = client.get("/api/measurements?period=daily").get_json()
    assert listing["total"] == 1
    row = listing["items"][0]
    assert row["data_source"] == "aggregate"
    assert row["data_source_label"] == "日均自动汇总"
    assert row["is_auto_aggregated"] is True
    assert row["valid_hours"] == 20
    assert row["is_exceeded"] is True
    assert listing["summary"]["exceeded_count"] == 1

    exceeded = client.get("/api/query/measurements?period=daily&is_exceeded=true").get_json()
    assert exceeded["total"] == 1

    stats = client.get("/api/query/statistics?group_by=period&metric=count").get_json()
    buckets = {item["key"]: item for item in stats["items"]}
    assert buckets["daily"]["count"] == 1
    assert buckets["daily"]["exceeded_count"] == 1
    assert buckets["hourly"]["count"] == 20


def test_daily_options_include_aggregate_source(client, station):
    body = client.get("/api/meta/options").get_json()
    sources = {item["value"]: item["label"] for item in body["data_source"]}
    assert sources["aggregate"] == "日均自动汇总"

    pollutants = client.get("/api/meta/pollutants").get_json()
    assert pollutants["daily_min_valid_hours"] == 20
