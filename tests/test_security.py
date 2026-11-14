from app import security


def test_password_policy():
    assert security.check_password("Abc@1234").ok
    assert not security.check_password("abc12345").ok
    assert not security.check_password("ABCD1234!").ok
    assert not security.check_password("Abcdefg!").ok
    assert not security.check_password("Abc12345").ok
    assert not security.check_password("Abc@12").ok


def test_password_hash_roundtrip():
    stored = security.hash_password("Abc@123456")
    assert security.verify_password("Abc@123456", stored)
    assert not security.verify_password("Abc@123457", stored)
    assert stored.startswith("pbkdf2_sha256$")


def test_token_roundtrip_and_tamper():
    token = security.create_token(7, "leader", "13800000001")
    payload = security.decode_token(token)
    assert payload and payload["uid"] == 7 and payload["role"] == "leader"
    assert security.decode_token(token[:-2] + "xx") is None


def test_phone_validation():
    assert security.check_phone("13800000002")
    assert not security.check_phone("12800000002")
    assert not security.check_phone("1380000000")
