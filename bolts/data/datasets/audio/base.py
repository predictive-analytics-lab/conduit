from __future__ import annotations
from pathlib import Path

from kit import implements
import numpy as np
import numpy.typing as npt
from torch import Tensor
import torchaudio

from bolts.data.datasets.base import PBDataset
from bolts.data.datasets.utils import (
    AudioLoadingBackend,
    AudioTform,
    apply_waveform_transform,
    infer_al_backend,
)
from bolts.data.structures import TargetData

__all__ = ["PBAudioDataset"]


class PBAudioDataset(PBDataset):
    """Base dataset for audio data."""

    x: npt.NDArray[np.string_]

    def __init__(
        self,
        *,
        x: npt.NDArray[np.string_],
        audio_dir: Path | str,
        y: TargetData | None = None,
        s: TargetData | None = None,
        transform: AudioTform | None = None,
    ) -> None:
        super().__init__(x=x, y=y, s=s)

        # Convert string path to Path object.
        if isinstance(audio_dir, str):
            audio_dir = Path(audio_dir)

        self.audio_dir = audio_dir
        self.transform = transform

        # Infer the appropriate audio-loading backend based on the operating system.
        self.al_backend: AudioLoadingBackend = infer_al_backend()
        self.log(f'Using {self.al_backend} as backend for audio-loading')
        torchaudio.set_audio_backend(self.al_backend)

    def __repr__(self) -> str:
        head = "Dataset " + self.__class__.__name__
        body = [
            f"Number of datapoints: {len(self)}",
            f"Base audio-directory location: {self.audio_dir.resolve()}",
            *self.extra_repr().splitlines(),
        ]
        if hasattr(self, "transform") and self.transform is not None:
            body += [repr(self.transform)]
        lines = [head] + [" " * self._repr_indent + line for line in body]
        return '\n'.join(lines)

    def load_waveform(self, index: int) -> Tensor:
        return torchaudio.load(self.audio_dir / self.x[index])

    @implements(PBDataset)
    def _sample_x(self, index: int, *, coerce_to_tensor: bool = False) -> Tensor:
        waveform = self.load_waveform(index)
        return apply_waveform_transform(waveform)
