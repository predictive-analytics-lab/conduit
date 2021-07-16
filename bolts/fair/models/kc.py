"""K&C Baseline model."""
from kit import implements
from torch import Tensor

__all__ = ["KC"]


from bolts.data.structures import TernarySampleIW
from bolts.fair.models.erm import ErmBaseline


class KC(ErmBaseline):
    """Kamiran and Calders instance weighting method."""

    @implements(ErmBaseline)
    def _get_loss(self, logits: Tensor, *, batch: TernarySampleIW) -> Tensor:
        return self._loss_fn(input=logits, target=batch.y.float(), instance_weight=batch.iw)
