"""Ecoacostics dataset provided A. Eldridge et al.
    Alice Eldridge, Paola Moscoso, Patrice Guyot, & Mika Peck. (2018).
    Data for "Sounding out Ecoacoustic Metrics: Avian species richness
    is predicted by acoustic indices in temperate but not tropical
    habitats" (Final) [Data set].
    Zenodo. https://doi.org/10.5281/zenodo.1255218
"""
from __future__ import annotations
from os import mkdir
from pathlib import Path
from typing import Callable, ClassVar, Optional, Union
import zipfile

from kit import parsable
import pandas as pd
import torch
from torch import Tensor
import torchaudio
import torchaudio.functional as F
import torchaudio.transforms as T
from torchvision.datasets.utils import download_and_extract_archive
from typing_extensions import Literal

from bolts.data.datasets.audio.base import PBAudioDataset
from bolts.data.datasets.utils import AudioTform, FileInfo

__all__ = ["Ecoacoustics"]
SoundscapeAttr = Literal["habitat", "site"]
Extension = Literal[".pt", ".wav"]


class Ecoacoustics(PBAudioDataset):
    """Dataset for audio data collected in various geographic locations."""

    INDICES_DIR: ClassVar[str] = "AvianID_AcousticIndices"
    METADATA_FILENAME: ClassVar[str] = "metadata.csv"

    _FILE_INFO: ClassVar[FileInfo] = FileInfo(name="Ecoacoustics.zip", id="PLACEHOLDER")
    _BASE_FOLDER: ClassVar[str] = "Ecoacoustics"
    _EC_LABELS_FILENAME: ClassVar[str] = "EC_AI.csv"
    _UK_LABELS_FILENAME: ClassVar[str] = "UK_AI.csv"
    _PROCESSED_DIR: ClassVar[str] = "processed_audio"
    _AUDIO_LEN: ClassVar[float] = 60.0  # Audio samples' durations in seconds.

    @parsable
    def __init__(
        self,
        root: Union[str, Path],
        *,
        download: bool = True,
        target_attr: SoundscapeAttr = "habitat",
        transform: Optional[AudioTform] = None,
        resample_rate: Optional[int] = 22050,
        specgram_segment_len: Optional[float] = 15,
        num_freq_bins: Optional[int] = 120,
        hop_length: Optional[int] = 60,
        preprocess_transform: Callable[[Tensor], Tensor] = T.Spectrogram,
    ) -> None:

        self.root = Path(root)
        self.download = download
        self.base_dir = self.root / self._BASE_FOLDER
        self.labels_dir = self.base_dir / self.INDICES_DIR
        self._processed_audio_dir = self.base_dir / self._PROCESSED_DIR
        self._metadata_path = self.base_dir / self.METADATA_FILENAME
        self.ec_labels_path = self.labels_dir / self._EC_LABELS_FILENAME
        self.uk_labels_path = self.labels_dir / self._UK_LABELS_FILENAME

        self._n_sgram_segments = int(self._AUDIO_LEN / specgram_segment_len)

        if self.download:
            self._download_files()
        self._check_files()

        if not self._processed_audio_dir.exists():
            self._preprocess_audio(
                specgram_segment_len, resample_rate, num_freq_bins, hop_length, preprocess_transform
            )

        # Extract labels from indices files.
        if not self._metadata_path.exists():
            self._extract_metadata()

        self.metadata = pd.read_csv(self.base_dir / self.METADATA_FILENAME)

        x = self.metadata["filePath"].to_numpy()
        y = torch.as_tensor(self.metadata[f'{target_attr}_le'])
        s = None

        super().__init__(x=x, y=y, s=s, transform=transform, audio_dir=self.base_dir)

    def _check_files(self) -> bool:
        """Check necessary files are present and unzipped."""

        if not self.labels_dir.exists():
            raise RuntimeError(
                f"Indices file not found at location {self.base_dir.resolve()}."
                "Have you downloaded it?"
            )
        elif zipfile.is_zipfile(self.labels_dir):
            raise RuntimeError("Indices file not unzipped.")

        for dir_ in ["UK_BIRD", "EC_BIRD"]:
            path = self.base_dir / dir_
            if not path.exists():
                raise RuntimeError(
                    f"Data not found at location {self.base_dir.resolve()}."
                    "Have you downloaded it?"
                )
            elif zipfile.is_zipfile(dir_):
                raise RuntimeError(f"{dir_} file not unzipped.")

        return True

    def _label_encode_metadata(self, metadata: pd.DataFrame) -> pd.DataFrame:
        """Label encode the extracted concept/context/superclass information."""
        for col in metadata.columns:
            # Skip over filepath and filename columns
            if "file" not in col and "File" not in col:
                # Add a new column containing the label-encoded data
                metadata[f"{col}_le"] = metadata[col].factorize()[0]
        return metadata

    def _download_files(self) -> None:
        """Download all files necessary for dataset to work."""

        # Create necessary directories if they doesn't already exist.
        self.root.mkdir(parents=True, exist_ok=True)
        self.base_dir.mkdir(parents=True, exist_ok=True)

        urls = [
            "https://zenodo.org/record/1255218/files/AvianID_AcousticIndices.zip",
            "https://zenodo.org/record/1255218/files/EC_BIRD.zip",
            "https://zenodo.org/record/1255218/files/UK_BIRD.zip",
        ]

        for url in urls:
            download_and_extract_archive(url, str(self.base_dir))

    def _extract_metadata(self) -> None:
        """Extract information such as labels from relevant csv files, combining them along with
        information on processed files to produce a master file."""
        self.log("Extracting metadata.")

        def gen_files_df(ext: Extension) -> pd.DataFrame:
            paths: list[Path] = []
            paths.extend(self.base_dir.glob(f"**/*{ext}"))
            str_paths = pd.Series([str(path.relative_to(self.base_dir)) for path in paths])

            df = str_paths.str.rpartition(
                "\\",
            )
            df[0] = df[0] + df[1]
            return df.drop(columns=[1]).rename(columns={0: "filePath", 2: "fileName"})

        ec_labels = pd.read_csv(self.ec_labels_path, encoding="ISO-8859-1")
        uk_labels = pd.read_csv(self.uk_labels_path, encoding="ISO-8859-1")

        sgram_seg_metadata = gen_files_df(".pt")
        sgram_seg_metadata.sort_values(by=["fileName"], axis=0, inplace=True)

        # Merge labels and metadata files.
        metadata = gen_files_df(".wav")
        metadata = metadata.merge(pd.concat([uk_labels, ec_labels]), how="left")
        metadata = metadata.loc[metadata.index.repeat(self._n_sgram_segments)]
        metadata.sort_values(by=["fileName"], axis=0, inplace=True)

        # Replace metadata filename and filepath column values with those in sgram_seg_info.
        metadata["filePath"] = sgram_seg_metadata["filePath"].values
        metadata["fileName"] = sgram_seg_metadata["fileName"].values

        metadata.reset_index(inplace=True, drop=True)
        metadata = self._label_encode_metadata(metadata)
        metadata.to_csv(self._metadata_path)

    def _preprocess_audio(
        self,
        specgram_segment_len: float,
        new_sr: int,
        n_freq_bins: int,
        hop_len: int,
        transform: Callable[[Tensor], Tensor],
    ) -> None:
        """
        Applies transformation to audio samples then segments transformed samples and stores
        them as processed files.
        """

        if not self._processed_audio_dir.exists():
            mkdir(self._processed_audio_dir)

        waveform_paths = list(self.base_dir.glob("**/*.wav"))

        tform = transform(n_fft=n_freq_bins, hop_length=hop_len)
        for path in waveform_paths:
            waveform_filename = str(path).split("\\")[-1].split(".")[0]
            waveform, sr = torchaudio.load(path)
            waveform = F.resample(waveform, sr, new_sr)
            specgram = tform(waveform)

            spectrogram_segments = self._segment_spectrogram(specgram, specgram_segment_len)
            for i, segment in enumerate(spectrogram_segments):
                torch.save(segment, f"{str(self._processed_audio_dir)}\\{waveform_filename}_{i}.pt")

    def _segment_spectrogram(self, specgram: Tensor, segment_len: float) -> list[Tensor]:
        """
        Takes a spectrogram and segments it into as many segments of segment_len width as possible.
        """
        seg_sz = int(specgram.shape[-1] / (self._AUDIO_LEN / segment_len))
        segment_boundaries = [(i - seg_sz, i) for i in range(seg_sz, specgram.shape[-1], seg_sz)]
        return [specgram[:, :, start:end] for start, end in segment_boundaries]
