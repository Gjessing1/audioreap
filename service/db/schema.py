from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Artist(Base):
    __tablename__ = "artists"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    musicbrainz_artist_id: Mapped[str | None] = mapped_column(String, nullable=True)
    id_algorithm_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    normalize_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    albums: Mapped[list["Album"]] = relationship(back_populates="artist")
    tracks: Mapped[list["Track"]] = relationship(back_populates="artist")


class Album(Base):
    __tablename__ = "albums"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    track_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    musicbrainz_release_id: Mapped[str | None] = mapped_column(String, nullable=True)
    artist_id: Mapped[str] = mapped_column(String, ForeignKey("artists.id"), nullable=False)
    id_algorithm_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    artist: Mapped["Artist"] = relationship(back_populates="albums")
    tracks: Mapped[list["Track"]] = relationship(back_populates="album")


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    artist_id: Mapped[str] = mapped_column(String, ForeignKey("artists.id"), nullable=False)
    album_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("albums.id"), nullable=True
    )
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    track_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    disc_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    musicbrainz_recording_id: Mapped[str | None] = mapped_column(String, nullable=True)
    id_algorithm_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    normalize_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    artist: Mapped["Artist"] = relationship(back_populates="tracks")
    album: Mapped["Album | None"] = relationship(back_populates="tracks")
    file: Mapped["TrackFile | None"] = relationship(back_populates="track", uselist=False)


class TrackFile(Base):
    __tablename__ = "track_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    track_id: Mapped[str] = mapped_column(String, ForeignKey("tracks.id"), nullable=False)
    path: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    codec: Mapped[str] = mapped_column(String, nullable=False)
    container: Mapped[str] = mapped_column(String, nullable=False)
    bitrate_kbps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sample_rate_hz: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    layout_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    track: Mapped["Track"] = relationship(back_populates="file")


class AcquisitionJobRow(Base):
    __tablename__ = "acquisition_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    track_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    provider_ref: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False, default="queued")
    failure_class: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class RejectedId(Base):
    __tablename__ = "rejected_ids"

    internal_id: Mapped[str] = mapped_column(String, primary_key=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
