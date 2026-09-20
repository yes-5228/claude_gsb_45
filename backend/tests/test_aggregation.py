"""小时值自动汇总日均值测试."""
from datetime import datetime

import pytest

from app.extensions import db
from app.models import Exceedance, Measurement


@pytest.fixture
def low_threshold(app):
    """降低日均值有效小时数门槛, 让测试只需录入少量小时数据."""
    app.config["DAILY_MIN_VALID_HOURS"] = 4
    return 4


def _post_hourly(client, station_id, entries, date="2026-09-01", hour=10, **overrides):
    payload = {
        "station_id": station_id,
        "measured_at": "%s %02d:00" % (date, hour),
        "period": "hourly",
        "data_source": "device",
        "entries": entries,
    }
    payload.update(overrides)
    return client.post("/api/measurements/entries", json=payload)


def _fill_hours(client, station_id, hours, entries, date="2026-09-01"):
    for hour in hours:
        response = _post_hourly(client, station_id, entries, date=date, hour=hour)
        assert response.status_code == 201


def _daily(pollutant):
    return Measurement.query.filter_by(pollutant=pollutant, period="daily").one()


def test_daily_average_is_generated_once_threshold_met(client, station, low_threshold):
    _fill_hours(client, station.id, (0, 6, 12, 18), [{"pollutant": "PM25", "value": 60.0}])
    daily = _daily("PM25")
    assert daily.value == 60.0
    assert daily.valid_hours == 4
    assert daily.is_complete is True
    assert daily.data_source == "auto"
    assert daily.measured_at == datetime(2026, 9, 1, 0, 0)
    assert daily.is_exceeded is False


def test_incomplete_daily_is_flagged_when_hours_insufficient(client, station, low_threshold):
    _fill_hours(client, station.id, (8, 9), [{"pollutant": "PM25", "value": 50.0}])
    daily = _daily("PM25")
    assert daily.valid_hours == 2
    assert daily.is_complete is False

    response = _post_hourly(client, station.id, [{"pollutant": "SO2", "value": 100.0}], hour=10)
    aggregation = response.get_json()["daily_aggregation"]
    assert aggregation["required_hours"] == low_threshold
    item = next(item for item in aggregation["items"] if item["pollutant"] == "SO2")
    assert item["status"] == "created"
    assert item["valid_hours"] == 1
    assert item["is_complete"] is False
    assert aggregation["summary"]["incomplete"] == 2


def test_default_threshold_requires_twenty_valid_hours(client, station):
    response = _post_hourly(client, station.id, [{"pollutant": "PM25", "value": 50.0}], hour=8)
    aggregation = response.get_json()["daily_aggregation"]
    assert aggregation["required_hours"] == 20
    daily = _daily("PM25")
    assert daily.valid_hours == 1
    assert daily.is_complete is False


def test_daily_value_averages_valid_hours(client, station, low_threshold):
    _fill_hours(client, station.id, (0, 6), [{"pollutant": "PM25", "value": 40.0}])
    _fill_hours(client, station.id, (12, 18), [{"pollutant": "PM25", "value": 80.0}])
    assert _daily("PM25").value == 60.0


def test_same_hour_records_count_as_one_valid_hour(client, station, low_threshold):
    _post_hourly(client, station.id, [{"pollutant": "PM25", "value": 40.0}], hour=10)
    response = client.post(
        "/api/measurements/entries",
        json={
            "station_id": station.id,
            "measured_at": "2026-09-01 10:30",
            "period": "hourly",
            "entries": [{"pollutant": "PM25", "value": 80.0}],
        },
    )
    assert response.status_code == 201
    daily = _daily("PM25")
    # 同一小时内的多条记录先取平均, 只计 1 个有效小时
    assert daily.valid_hours == 1
    assert daily.value == 60.0
    assert daily.is_complete is False


def test_generated_daily_average_joins_exceedance_check(client, station, low_threshold):
    _fill_hours(client, station.id, (0, 6, 12, 18), [{"pollutant": "PM25", "value": 120.0}])
    daily = _daily("PM25")
    assert daily.is_exceeded is True
    assert daily.limit_value == 75.0
    exceedance = Exceedance.query.filter_by(period="daily").one()
    assert exceedance.value == 120.0
    assert exceedance.level == "moderate"  # 120 / 75 = 1.6
    assert exceedance.status == "pending"


def test_daily_value_refreshes_after_hourly_overwrite(client, station, low_threshold):
    _fill_hours(client, station.id, (0, 6, 12, 18), [{"pollutant": "PM25", "value": 40.0}])
    assert _daily("PM25").value == 40.0

    _post_hourly(
        client, station.id, [{"pollutant": "PM25", "value": 140.0}], hour=0, overwrite=True
    )
    daily = _daily("PM25")
    assert daily.value == 65.0  # (140 + 40 * 3) / 4
    assert daily.valid_hours == 4
    assert daily.is_complete is True


def test_manual_daily_record_is_not_overwritten(client, station, low_threshold, entry_payload):
    client.post(
        "/api/measurements/entries",
        json=entry_payload(
            station.id,
            measured_at="2026-09-01 00:00",
            period="daily",
            data_source="import",
            entries=[{"pollutant": "PM25", "value": 33.0}],
        ),
    )
    response = _post_hourly(client, station.id, [{"pollutant": "PM25", "value": 100.0}], hour=10)
    item = next(
        item
        for item in response.get_json()["daily_aggregation"]["items"]
        if item["pollutant"] == "PM25"
    )
    assert item["status"] == "skipped"
    daily = _daily("PM25")
    assert daily.value == 33.0
    assert daily.data_source == "import"
    assert daily.valid_hours is None


def test_auto_daily_removed_when_last_hourly_deleted(client, station, low_threshold):
    _fill_hours(client, station.id, (8, 9), [{"pollutant": "PM25", "value": 50.0}])
    assert Measurement.query.filter_by(period="daily").count() == 1

    for row in Measurement.query.filter_by(period="hourly").all():
        response = client.delete("/api/measurements/%d" % row.id)
        assert response.status_code == 200
    assert Measurement.query.filter_by(period="daily").count() == 0


def test_daily_entry_does_not_trigger_aggregation(client, station, entry_payload):
    response = client.post(
        "/api/measurements/entries",
        json=entry_payload(
            station.id, period="daily", entries=[{"pollutant": "PM25", "value": 60.0}]
        ),
    )
    assert response.status_code == 201
    assert "daily_aggregation" not in response.get_json()


def test_aggregate_daily_endpoint_backfills(client, app, station, low_threshold):
    # 直接写库构造历史小时数据, 模拟补算场景
    for hour in (0, 6, 12, 18):
        db.session.add(
            Measurement(
                station_id=station.id,
                pollutant="NO2",
                period="hourly",
                value=90.0,
                unit="μg/m³",
                measured_at=datetime(2026, 9, 1, hour),
            )
        )
    db.session.commit()
    assert Measurement.query.filter_by(period="daily").count() == 0

    response = client.post("/api/measurements/aggregate-daily", json={"date": "2026-09-01"})
    assert response.status_code == 200
    body = response.get_json()
    assert body["summary"]["stations"] == 1
    assert body["summary"]["created"] == 1
    daily = _daily("NO2")
    assert daily.value == 90.0
    assert daily.is_complete is True

    again = client.post(
        "/api/measurements/aggregate-daily",
        json={"date": "2026-09-01", "station_id": station.id},
    )
    assert again.get_json()["summary"]["updated"] == 1


def test_aggregate_daily_endpoint_validates_input(client, station):
    assert client.post("/api/measurements/aggregate-daily", json={}).status_code == 422
    assert (
        client.post(
            "/api/measurements/aggregate-daily",
            json={"date": "2026-09-01", "station_id": 9999},
        ).status_code
        == 404
    )


def test_generated_daily_joins_query_filters_and_statistics(client, station, low_threshold):
    _fill_hours(client, station.id, (0, 6, 12, 18), [{"pollutant": "PM25", "value": 60.0}])
    _fill_hours(
        client, station.id, (0, 6), [{"pollutant": "SO2", "value": 100.0}], date="2026-09-02"
    )

    incomplete = client.get("/api/query/measurements?is_complete=false").get_json()
    assert incomplete["total"] == 1
    assert incomplete["items"][0]["pollutant"] == "SO2"
    assert incomplete["items"][0]["is_complete"] is False
    assert incomplete["items"][0]["completeness_label"] == "数据不完整"

    complete = client.get("/api/query/measurements?is_complete=true").get_json()
    assert complete["total"] == 1
    assert complete["items"][0]["pollutant"] == "PM25"

    stats = client.get("/api/query/statistics?group_by=period&metric=count").get_json()
    buckets = {item["key"]: item["count"] for item in stats["items"]}
    assert buckets == {"hourly": 6, "daily": 2}

    by_source = client.get("/api/query/statistics?group_by=data_source&metric=count").get_json()
    sources = {item["key"]: item["count"] for item in by_source["items"]}
    assert sources["auto"] == 2
