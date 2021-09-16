"""CelebA data-module."""
from functools import partial
from typing import Any, Union

import albumentations as A
import attr
from kit import implements
from kit.misc import str_to_enum
from pytorch_lightning import LightningDataModule

from conduit.data.datamodules.base import CdtDataModule
from conduit.data.datamodules.vision.base import CdtVisionDataModule
from conduit.data.datasets.vision.celeba import CelebA, CelebASplit, CelebAttr
from conduit.data.structures import TrainValTestSplit

__all__ = ["CelebADataModule"]


@attr.define(kw_only=True)
class CelebADataModule(CdtVisionDataModule):
    """Data-module for the CelebA dataset."""

    image_size: int = 224
    superclass: Union[CelebAttr, str] = attr.field(
        converter=partial(str_to_enum, enum=CelebAttr), default=CelebAttr.Smiling
    )
    subclass: Union[CelebAttr, str] = attr.field(
        converter=partial(str_to_enum, enum=CelebAttr), default=CelebAttr.Male
    )
    use_predefined_splits: bool = False

    @implements(LightningDataModule)
    def prepare_data(self, *args: Any, **kwargs: Any) -> None:
        CelebA(root=self.root, download=True)

    @property  # type: ignore[misc]
    @implements(CdtVisionDataModule)
    def _default_train_transforms(self) -> A.Compose:
        base_transforms = A.Compose(
            [
                A.Resize(self.image_size, self.image_size),
                A.CenterCrop(self.image_size, self.image_size),
            ]
        )
        normalization = super()._default_train_transforms
        return A.Compose([base_transforms, normalization])

    @property  # type: ignore[misc]
    @implements(CdtVisionDataModule)
    def _default_test_transforms(self) -> A.Compose:
        return self._default_train_transforms

    @implements(CdtDataModule)
    def _get_splits(self) -> TrainValTestSplit:
        # Split the data according to the pre-defined split indices
        if self.use_predefined_splits:
            train_data, val_data, test_data = (
                CelebA(root=self.root, superclass=self.superclass, transform=None, split=split)
                for split in CelebASplit
            )
        # Split the data randomly according to test- and val-prop
        else:
            all_data = CelebA(root=self.root, superclass=self.superclass, transform=None)
            val_data, test_data, train_data = all_data.random_split(
                props=(self.val_prop, self.test_prop)
            )
        return TrainValTestSplit(train=train_data, val=val_data, test=test_data)
