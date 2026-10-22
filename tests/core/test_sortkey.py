from degreaser.core.sortkey import sort_key


def test_natural_order() -> None:
    # Equal keys ("File2" / "file02") are ordered by raw name.
    names = ["file10", "File2", "file1", "file02", "file", "a", "B"]
    assert sorted(names, key=lambda n: (sort_key(n), n)) == [
        "a",
        "B",
        "file",
        "file1",
        "File2",
        "file02",
        "file10",
    ]


def test_leading_zeros_and_long_numbers() -> None:
    assert sort_key("x007") < sort_key("x08")
    assert sort_key("IMG_9999") < sort_key("IMG_10000")
    assert sort_key("0") == sort_key("00")


def test_unicode_normalisation() -> None:
    nfc, nfd = "café", "café"
    assert sort_key(nfc) == sort_key(nfd)
