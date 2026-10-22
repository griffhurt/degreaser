"""Video and audio preview through QMediaPlayer (FFmpeg backend).

Rules from spikes/smb_streaming: sources are URLs only (local files now, the loopback media
server for SMB in M2) — never setSourceDevice() with a Python QIODevice. Teardown is stop()
then setSource(QUrl()). MPEG-PS/TS seeks land on the next keyframe, so the UI always reads
position() back instead of assuming a seek landed where it was asked to.
"""

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtMultimedia import (
    QAudioOutput,
    QMediaFormat,
    QMediaMetaData,
    QMediaPlayer,
    QVideoFrame,
)
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSlider,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from degreaser.core.preview.containers import ContainerInfo
from degreaser.core.preview.metadata import (
    MetaRow,
    container_rows,
    dimensions,
    format_duration,
)

POSTER_POSITION_MS = 1000


class MediaView(QWidget):
    """Emits `ready` once there is something to show: a first video frame, or loaded audio."""

    ready = Signal()
    failed = Signal(str)
    metadataChanged = Signal()

    def __init__(
        self,
        url: QUrl,
        *,
        autoplay_muted: bool = False,
        container: ContainerInfo | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.autoplay_muted = autoplay_muted
        self.container = container
        self._ready = False
        self._started = False
        self._dragging = False
        self._torn_down = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.video = QVideoWidget()
        self.video.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout.addWidget(self.video, 1)
        self.audio_label = QLabel("♪ Audio")
        self.audio_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = self.audio_label.font()
        font.setPointSizeF(font.pointSizeF() * 2)
        self.audio_label.setFont(font)
        self.audio_label.hide()
        layout.addWidget(self.audio_label, 1)

        controls = QHBoxLayout()
        controls.setContentsMargins(6, 2, 6, 4)
        style = self.style()
        self.play_button = QToolButton()
        self.play_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.play_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.play_button.clicked.connect(self.toggle_play)
        controls.addWidget(self.play_button)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.slider.sliderPressed.connect(self._on_slider_pressed)
        self.slider.sliderReleased.connect(self._on_slider_released)
        controls.addWidget(self.slider, 1)
        self.time_label = QLabel("0:00 / 0:00")
        controls.addWidget(self.time_label)
        self.mute_button = QToolButton()
        self.mute_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.mute_button.setCheckable(True)
        self.mute_button.toggled.connect(self._on_mute_toggled)
        controls.addWidget(self.mute_button)
        layout.addLayout(controls)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.mute_button.setChecked(autoplay_muted)
        self._on_mute_toggled(autoplay_muted)

        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.errorOccurred.connect(self._on_error)
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.metaDataChanged.connect(self.metadataChanged)
        self.player.tracksChanged.connect(self.metadataChanged)
        self.video.videoSink().videoFrameChanged.connect(self._on_frame)
        self.player.setSource(url)

    # Playback ----------------------------------------------------------------------------------

    def toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def seek(self, position_ms: int) -> None:
        self.player.setPosition(max(0, position_ms))

    def position(self) -> int:
        return self.player.position()

    def is_ready(self) -> bool:
        return self._ready

    def teardown(self) -> None:
        if self._torn_down:
            return
        self._torn_down = True
        self.player.stop()
        self.player.setSource(QUrl())

    # Metadata --------------------------------------------------------------------------------

    def metadata_rows(self) -> list[MetaRow]:
        md = self.player.metaData()
        rows: list[MetaRow] = []
        if self.player.duration() > 0:
            rows.append(MetaRow("Media", "Duration", format_duration(self.player.duration())))
        if self.container and self.container.container:
            rows.append(MetaRow("Media", "Container", self.container.container))
        resolution = md.value(QMediaMetaData.Key.Resolution)
        if isinstance(resolution, QSize) and resolution.isValid():
            rows.append(
                MetaRow("Media", "Resolution", dimensions(resolution.width(), resolution.height()))
            )
        frame_rate = md.value(QMediaMetaData.Key.VideoFrameRate)
        if isinstance(frame_rate, float) and frame_rate > 0:
            rows.append(MetaRow("Media", "Frame rate", f"{frame_rate:.3g} fps"))
        # Container headers name old codecs precisely (DivX 3, WMV 8, …); Qt is the fallback.
        c = self.container
        video = (c.video if c and c.video else []) or _codec_names(
            self.player.videoTracks(), "video"
        )
        audio = (c.audio if c and c.audio else []) or _codec_names(
            self.player.audioTracks(), "audio"
        )
        if video:
            rows.append(MetaRow("Media", "Video codec", ", ".join(video)))
        if audio:
            rows.append(MetaRow("Media", "Audio codec", ", ".join(audio)))
        for key, label in (
            (QMediaMetaData.Key.VideoBitRate, "Video bitrate"),
            (QMediaMetaData.Key.AudioBitRate, "Audio bitrate"),
        ):
            value = md.value(key)
            if isinstance(value, int) and value > 0:
                rows.append(MetaRow("Media", label, f"{value / 1000:.0f} kb/s"))
        for key, label in (
            (QMediaMetaData.Key.Title, "Title"),
            (QMediaMetaData.Key.ContributingArtist, "Artist"),
            (QMediaMetaData.Key.AlbumArtist, "Album artist"),
            (QMediaMetaData.Key.AlbumTitle, "Album"),
            (QMediaMetaData.Key.Date, "Date"),
        ):
            text = md.stringValue(key)
            if text:
                rows.append(MetaRow("Media", label, text))
        if not rows and self.container:
            rows = container_rows(self.container)
        return rows

    # Signal handlers ---------------------------------------------------------------------------

    def _mark_ready(self) -> None:
        if not self._ready:
            self._ready = True
            self.ready.emit()

    def _on_status(self, status: QMediaPlayer.MediaStatus) -> None:
        if self._torn_down:
            return
        if status == QMediaPlayer.MediaStatus.LoadedMedia and not self._started:
            # Qt re-reports LoadedMedia after seeks; the poster/autoplay setup runs only once.
            self._started = True
            has_video = self.player.hasVideo()
            self.video.setVisible(has_video)
            self.audio_label.setVisible(not has_video)
            if self.autoplay_muted:
                self.player.play()
            elif has_video:
                # Pausing (not stopping) makes the backend decode a poster frame.
                duration = self.player.duration()
                self.player.setPosition(min(POSTER_POSITION_MS, duration // 10) if duration else 0)
                self.player.pause()
            if not has_video:
                self._mark_ready()
            self.metadataChanged.emit()
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self.failed.emit(self.player.errorString() or "This file can't be played.")

    def _on_error(self, error: QMediaPlayer.Error, message: str) -> None:
        if error != QMediaPlayer.Error.NoError and not self._torn_down:
            self.failed.emit(message or "This file can't be played.")

    def _on_frame(self, frame: QVideoFrame) -> None:
        if frame.isValid():
            self._mark_ready()

    def _on_position(self, position: int) -> None:
        if not self._dragging:
            self.slider.setValue(position)
        self._update_time()

    def _on_duration(self, duration: int) -> None:
        self.slider.setRange(0, max(0, duration))
        self._update_time()

    def _on_state(self, state: QMediaPlayer.PlaybackState) -> None:
        icon = (
            QStyle.StandardPixmap.SP_MediaPause
            if state == QMediaPlayer.PlaybackState.PlayingState
            else QStyle.StandardPixmap.SP_MediaPlay
        )
        self.play_button.setIcon(self.style().standardIcon(icon))

    def _on_slider_pressed(self) -> None:
        self._dragging = True

    def _on_slider_released(self) -> None:
        self._dragging = False
        self.seek(self.slider.value())

    def _on_mute_toggled(self, muted: bool) -> None:
        self.audio.setMuted(muted)
        icon = (
            QStyle.StandardPixmap.SP_MediaVolumeMuted
            if muted
            else QStyle.StandardPixmap.SP_MediaVolume
        )
        self.mute_button.setIcon(self.style().standardIcon(icon))

    def _update_time(self) -> None:
        self.time_label.setText(
            f"{format_duration(self.player.position())} / {format_duration(self.player.duration())}"
        )


def _codec_names(tracks: list[QMediaMetaData], kind: str) -> list[str]:
    """Codec names Qt knows; old codecs come back as "Unspecified" and are skipped."""
    key = QMediaMetaData.Key.VideoCodec if kind == "video" else QMediaMetaData.Key.AudioCodec
    names = []
    for track in tracks:
        codec = track.value(key)
        if codec is None:
            continue
        name = (
            QMediaFormat.videoCodecName(codec)
            if kind == "video"
            else QMediaFormat.audioCodecName(codec)
        )
        if name and name != "Unspecified" and name not in names:
            names.append(name)
    return names
