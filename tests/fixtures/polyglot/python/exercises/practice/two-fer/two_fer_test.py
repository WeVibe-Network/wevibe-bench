from two_fer import two_fer


def test_two_fer() -> None:
    assert two_fer("Alice") == "One for Alice, one for me."
