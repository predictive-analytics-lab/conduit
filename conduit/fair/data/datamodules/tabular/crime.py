"""Crime Dataset."""
import attr
from ethicml.data import Crime, CrimeSplits, Dataset

from conduit.fair.data.datamodules.tabular.base import EthicMlDataModule

__all__ = ["CrimeDataModule", "CrimeSplits"]


@attr.define(kw_only=True)
class CrimeDataModule(EthicMlDataModule):
    """Data Module for the Crime Dataset."""

    sens_feat: CrimeSplits = CrimeSplits.RACE_BINARY
    disc_feats_only: bool = False

    @property
    def em_dataset(self) -> Dataset:
        return Crime(
            split=self.sens_feat, discrete_only=self.disc_feats_only, invert_s=self.invert_s
        )
