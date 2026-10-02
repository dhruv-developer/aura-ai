from aura_shared.redis import KeyFamily, build_key
from aura_shared.redis.keys import key_part


def test_key_part_escapes_separators_without_collisions() -> None:
    assert key_part("ollama/gemma:2b") == "ollama/gemma%3A2b"
    assert key_part("a b{c}") == "a%20b%7Bc%7D"
    assert key_part("x:y") != key_part("x%3Ay")  # the escape char itself is escaped
    key = build_key(KeyFamily.CIRCUIT, "_global", "open", key_part("ollama/gemma:2b"))
    assert key.count(":") == key.replace("gemma%3A2b", "").count(":")  # no extra segment
