from degreaser.core.preview.detect import Detection, Kind
from degreaser.core.preview.plan import Confirm, Strategy, plan_preview
from degreaser.core.settings import Downloads
from degreaser.core.sources.base import Entry

LIMITS = Downloads(auto_load_bytes=100, large_warning_bytes=1000, text_ceiling_bytes=5000)


def entry(size: int) -> Entry:
    return Entry("f", "file", size, 0.0, None)


def det(kind: Kind, mime: str | None = None) -> Detection:
    return Detection(kind, kind.value, mime)


def test_text_and_hex_are_samples() -> None:
    text = plan_preview(det(Kind.CODE), entry(10_000), LIMITS, local=True)
    assert (text.strategy, text.initial_bytes, text.ceiling_bytes) == (
        Strategy.TEXT_SAMPLE,
        64 * 1024,
        5000,
    )
    hexp = plan_preview(det(Kind.BINARY), entry(10_000), LIMITS, local=True)
    assert hexp.strategy == Strategy.HEX_SAMPLE and hexp.expandable
    assert plan_preview(det(Kind.BINARY), entry(0), LIMITS, local=True).strategy == Strategy.NONE


def test_image_and_pdf_confirmation() -> None:
    jpeg = det(Kind.IMAGE, "image/jpeg")
    assert plan_preview(jpeg, entry(50), LIMITS, local=True).confirm is None
    small = plan_preview(jpeg, entry(50), LIMITS, local=True)
    assert small.exif_thumbnail_first
    assert plan_preview(jpeg, entry(500), LIMITS, local=True).confirm == Confirm.AUTO_LOAD_LIMIT
    assert plan_preview(jpeg, entry(5000), LIMITS, local=False).confirm == Confirm.LARGE_DOWNLOAD
    # Local files are never "downloads".
    assert plan_preview(jpeg, entry(5000), LIMITS, local=True).confirm == Confirm.AUTO_LOAD_LIMIT
    pdf = plan_preview(det(Kind.PDF), entry(500), LIMITS, local=True)
    assert (pdf.strategy, pdf.confirm) == (Strategy.PDF, Confirm.AUTO_LOAD_LIMIT)
    assert not plan_preview(
        det(Kind.IMAGE, "image/png"), entry(5), LIMITS, local=True
    ).exif_thumbnail_first


def test_media_streams_and_zip_lists() -> None:
    huge = entry(50 * 1024**3)
    assert plan_preview(det(Kind.VIDEO), huge, LIMITS, local=False).strategy == Strategy.MEDIA
    assert plan_preview(det(Kind.VIDEO), huge, LIMITS, local=False).confirm is None
    assert plan_preview(det(Kind.ZIP), huge, LIMITS, local=False).strategy == Strategy.ZIP_LISTING
