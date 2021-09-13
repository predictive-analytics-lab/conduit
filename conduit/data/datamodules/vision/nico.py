"""Nico data-module."""
from __future__ import annotations
from typing import Any, Optional, Union

import albumentations as A
from kit import implements, parsable
from kit.torch.data import TrainingMode
from pytorch_lightning import LightningDataModule

from conduit.data.datamodules.base import CdtDataModule
from conduit.data.datamodules.vision.base import CdtVisionDataModule
from conduit.data.datasets.utils import ImageTform
from conduit.data.datasets.vision.nico import NICO, NicoSuperclass
from conduit.data.structures import TrainValTestSplit

__all__ = ["NICODataModule"]


class NICODataModule(CdtVisionDataModule):
    """Data-module for the NICO dataset."""

    @parsable
    def __init__(
        self,
        root: str,
        *,
        image_size: int = 224,
        train_batch_size: int = 32,
        eval_batch_size: Optional[int] = 64,
        num_workers: int = 0,
        val_prop: float = 0.2,
        test_prop: float = 0.2,
        class_train_props: Optional[dict] = None,
        seed: int = 47,
        persist_workers: bool = False,
        pin_memory: bool = True,
        superclass: NicoSuperclass = NicoSuperclass.animals,
        stratified_sampling: bool = False,
        instance_weighting: bool = False,
        training_mode: Union[TrainingMode, str] = "epoch",
        train_transforms: Optional[ImageTform] = None,
        test_transforms: Optional[ImageTform] = None,
    ) -> None:
        super().__init__(
            root=root,
            train_batch_size=train_batch_size,
            eval_batch_size=eval_batch_size,
            num_workers=num_workers,
            val_prop=val_prop,
            test_prop=test_prop,
            seed=seed,
            persist_workers=persist_workers,
            pin_memory=pin_memory,
            stratified_sampling=stratified_sampling,
            instance_weighting=instance_weighting,
            training_mode=training_mode,
            train_transforms=train_transforms,
            test_transforms=test_transforms,
        )
        self.image_size = image_size
        self.superclass = superclass
        self.class_train_props = class_train_props

    @property  # type: ignore[misc]
    @implements(CdtVisionDataModule)
    def _default_train_transforms(self) -> A.Compose:
        base_transforms = A.Compose(
            [
                A.Resize(self.image_size, self.image_size),
                A.CenterCrop(self.image_size, self.image_size),
            ]
        )
        normalization = super()._default_train_transforms()
        return A.Compose([base_transforms, normalization])

    @property  # type: ignore[misc]
    @implements(CdtVisionDataModule)
    def _default_test_transforms(self) -> A.Compose:
        return self._default_train_transforms

    @implements(LightningDataModule)
    def prepare_data(self, *args: Any, **kwargs: Any) -> None:
        NICO(root=self.root, download=True)

    @implements(CdtDataModule)
    def _get_splits(self) -> TrainValTestSplit:
        all_data = NICO(root=self.root, superclass=self.superclass, transform=None)
        train_val_prop = 1 - self.test_prop
        train_val_data, test_data = all_data.train_test_split(
            default_train_prop=train_val_prop,
            train_props=self.class_train_props,
            seed=self.seed,
        )
        val_data, train_data = train_val_data.random_split(props=self.val_prop / train_val_prop)

        return TrainValTestSplit(train=train_data, val=val_data, test=test_data)
