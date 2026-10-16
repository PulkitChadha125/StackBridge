from superadmin_panel import security


def test_hash_and_verify_password_round_trip():
    password_hash = security.hash_password("Correct-Horse-Battery-Staple1")
    assert security.verify_password("Correct-Horse-Battery-Staple1", password_hash)


def test_verify_password_rejects_wrong_password():
    password_hash = security.hash_password("Correct-Horse-Battery-Staple1")
    assert not security.verify_password("wrong-password", password_hash)


def test_password_hash_is_argon2id():
    password_hash = security.hash_password("anything")
    assert password_hash.startswith("$argon2id$")


def test_verify_password_rejects_malformed_hash():
    assert not security.verify_password("whatever", "not-a-real-hash")


def test_generate_temp_password_length_and_charset():
    pw = security.generate_temp_password()
    assert len(pw) == 12
    assert all(ch in security._TEMP_PASSWORD_ALPHABET for ch in pw)


def test_generate_token_is_unique_and_url_safe():
    tokens = {security.generate_token() for _ in range(50)}
    assert len(tokens) == 50
    for token in tokens:
        assert "/" not in token and "+" not in token


def test_hash_token_is_deterministic_sha256():
    token = "sample-token-value"
    assert security.hash_token(token) == security.hash_token(token)
    assert len(security.hash_token(token)) == 64


def test_constant_time_eq():
    assert security.constant_time_eq("abc", "abc")
    assert not security.constant_time_eq("abc", "abd")
