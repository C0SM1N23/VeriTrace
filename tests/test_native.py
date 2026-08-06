from veritrace import _native


def test_hello_returns_nonempty_string():
    result = _native.hello()
    assert isinstance(result, str)
    assert result
