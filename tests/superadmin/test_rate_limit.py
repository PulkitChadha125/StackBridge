from superadmin_panel import rate_limit


def test_allows_up_to_max_attempts():
    key = "test-key-1"
    for _ in range(5):
        assert rate_limit.is_rate_limited(key, max_attempts=5, window_seconds=60) is False
    assert rate_limit.is_rate_limited(key, max_attempts=5, window_seconds=60) is True


def test_reset_clears_bucket():
    key = "test-key-2"
    for _ in range(3):
        rate_limit.is_rate_limited(key, max_attempts=3, window_seconds=60)
    assert rate_limit.is_rate_limited(key, max_attempts=3, window_seconds=60) is True

    rate_limit.reset(key)
    assert rate_limit.is_rate_limited(key, max_attempts=3, window_seconds=60) is False


def test_different_keys_independent():
    assert rate_limit.is_rate_limited("a", max_attempts=1, window_seconds=60) is False
    assert rate_limit.is_rate_limited("a", max_attempts=1, window_seconds=60) is True
    assert rate_limit.is_rate_limited("b", max_attempts=1, window_seconds=60) is False


def test_old_attempts_expire_out_of_window():
    key = "test-key-3"
    assert rate_limit.is_rate_limited(key, max_attempts=1, window_seconds=0.05) is False
    assert rate_limit.is_rate_limited(key, max_attempts=1, window_seconds=0.05) is True
    import time

    time.sleep(0.1)
    assert rate_limit.is_rate_limited(key, max_attempts=1, window_seconds=0.05) is False


def test_login_endpoint_rate_limited_after_threshold(client, monkeypatch):
    from superadmin_panel import config

    monkeypatch.setattr(config, "LOGIN_RATE_LIMIT_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(config, "LOGIN_RATE_LIMIT_WINDOW_SECONDS", 60)

    for _ in range(3):
        resp = client.post(
            "/api/auth/login",
            json={"username": "nobody", "password": "x", "device_fingerprint": "fp"},
        )
        assert resp.status_code == 401  # invalid_credentials, not yet limited

    limited = client.post(
        "/api/auth/login",
        json={"username": "nobody", "password": "x", "device_fingerprint": "fp"},
    )
    assert limited.status_code == 429
    assert limited.get_json()["error"] == "rate_limited"


def test_superadmin_web_login_rate_limited_after_threshold(client, monkeypatch):
    from superadmin_panel import config

    monkeypatch.setattr(config, "LOGIN_RATE_LIMIT_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(config, "LOGIN_RATE_LIMIT_WINDOW_SECONDS", 60)

    for _ in range(3):
        resp = client.post("/login", data={"username": "owner", "password": "wrong"})
        assert resp.status_code == 200  # rendered login page with an error, not yet limited

    limited = client.post("/login", data={"username": "owner", "password": "wrong"})
    assert limited.status_code == 429
