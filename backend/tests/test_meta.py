"""元数据接口测试."""


def test_health_endpoint(client):
    body = client.get("/api/meta/health").get_json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert "GB 3095-2012" in body["limit_policy"]


def test_pollutant_endpoint_exposes_limits(client):
    body = client.get("/api/meta/pollutants").get_json()
    pm25 = next(item for item in body["items"] if item["code"] == "PM25")
    assert pm25["limits"]["daily"] == 75.0
    assert pm25["limits"]["hourly"] is None
    assert {item["value"] for item in body["periods"]} == {"hourly", "daily"}
    assert body["daily_min_valid_hours"] == 20


def test_options_endpoint_lists_enumerations(client, station):
    body = client.get("/api/meta/options").get_json()
    assert {item["value"] for item in body["station_status"]} == {"active", "maintenance", "offline"}
    assert body["stations"][0]["code"] == "TEST-001"
    assert body["areas"] == ["测试区"]


def test_overview_endpoint_aggregates_everything(client, station, entry_payload):
    client.post("/api/measurements/entries", json=entry_payload(station.id))
    body = client.get("/api/meta/overview").get_json()
    assert body["stations"]["total"] == 1
    # 3 条小时值 + 3 条自动汇总日均值
    assert body["measurements"]["total"] == 6
    assert body["measurements"]["exceeded_count"] == 2
    assert body["exceedances"]["pending"] == 2
    assert body["pending_exceedances"][0]["pollutant"] == "SO2"
    assert body["trend"]["group_by"] == "day"
