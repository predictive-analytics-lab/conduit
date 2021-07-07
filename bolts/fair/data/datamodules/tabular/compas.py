"""COMPAS Dataset."""
from typing import Optional, Union

import ethicml as em
from ethicml.preprocessing.scaling import ScalerType
from kit import parsable

from .base import TabularDataModule

__all__ = ["CompasDataModule"]


class CompasDataModule(TabularDataModule):
    """COMPAS Dataset."""

    @parsable
    def __init__(
        self,
        val_split: Union[float, int] = 0.2,
        test_split: Union[float, int] = 0.2,
        num_workers: int = 0,
        batch_size: int = 32,
        scaler: Optional[ScalerType] = None,
        seed: int = 0,
        persist_workers: bool = False,
        stratified_sampling: bool = False,
        sample_with_replacement: bool = False,
    ):
        super().__init__(
            batch_size=batch_size,
            num_workers=num_workers,
            scaler=scaler,
            seed=seed,
            test_split=test_split,
            val_split=val_split,
            persist_workers=persist_workers,
            stratified_sampling=stratified_sampling,
            sample_with_replacement=sample_with_replacement,
        )
        self._em_dataset = em.compas(split="Sex")
        self.num_classes = 2
        self.num_sens = 2

    @property
    def em_dataset(self) -> em.Dataset:
        return self._em_dataset
