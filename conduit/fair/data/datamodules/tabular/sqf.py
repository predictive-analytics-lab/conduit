"""Law Admissions Dataset."""
from enum import Enum

import attr
from ethicml.data import Dataset, Sqf

from conduit.fair.data.datamodules.tabular.base import EthicMlDataModule

__all__ = ["SqfDataModule", "SqfSens"]


class SqfSens(Enum):
    sex = "Sex"
    race = "Race"
    sexRace = "Race-Sex"
    custom = "Custom"


@attr.define(kw_only=True)
class SqfDataModule(EthicMlDataModule):
    """NYC Stop, Question, Frisk Dataset."""

    sens_feat: SqfSens = SqfSens.sex
    disc_feats_only: bool = False

    @property
    def em_dataset(self) -> Dataset:
        return Sqf(
            split=self.sens_feat.value, discrete_only=self.disc_feats_only, invert_s=self.invert_s
        )
