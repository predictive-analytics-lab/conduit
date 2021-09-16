"""LAFTR model."""
import itertools
from typing import Any, Dict, List, Mapping, NamedTuple, Tuple, Union

import ethicml as em
from kit import implements
from kit.torch import CrossEntropyLoss, ReductionType, TrainingMode
import pandas as pd
import pytorch_lightning as pl
from pytorch_lightning.utilities.types import EPOCH_OUTPUT, STEP_OUTPUT
import torch
from torch import Tensor, nn, optim
from torch.optim.lr_scheduler import CosineAnnealingWarmRestarts
import torchmetrics

from conduit.data.structures import TernarySample
from conduit.fair.misc import FairnessType
from conduit.models.base import CdtModel
from conduit.models.utils import accuracy, aggregate_over_epoch, prediction, prefix_keys
from conduit.types import LRScheduler, Stage

__all__ = ["LAFTR"]


class ModelOut(NamedTuple):
    y: Tensor
    z: Tensor
    s: Tensor
    x: Tensor


class LAFTR(CdtModel):
    """Learning Adversarially Fair and Transferrable Representations model.

    The model is only defined with respect to binary S and binary Y.
    """

    def __init__(
        self,
        *,
        lr: float,
        weight_decay: float,
        disc_steps: int,
        fairness: FairnessType,
        recon_weight: float,
        clf_weight: float,
        adv_weight: float,
        enc: nn.Module,
        dec: nn.Module,
        adv: nn.Module,
        clf: nn.Module,
        lr_initial_restart: int = 10,
        lr_restart_mult: int = 2,
        lr_sched_interval: TrainingMode = TrainingMode.epoch,
        lr_sched_freq: int = 1,
    ) -> None:
        super().__init__(
            lr=lr,
            weight_decay=weight_decay,
            lr_initial_restart=lr_initial_restart,
            lr_restart_mult=lr_restart_mult,
            lr_sched_interval=lr_sched_interval,
            lr_sched_freq=lr_sched_freq,
        )
        self.enc = enc
        self.dec = dec
        self.adv = adv
        self.clf = clf

        self._clf_loss = CrossEntropyLoss(reduction=ReductionType.mean)
        self._recon_loss = nn.L1Loss(reduction="mean")
        self._adv_clf_loss = nn.L1Loss(reduction="none")

        self.disc_steps = disc_steps
        self.fairness = fairness

        self.clf_weight = clf_weight
        self.adv_weight = adv_weight
        self.recon_weight = recon_weight

        self.test_acc = torchmetrics.Accuracy()
        self.train_acc = torchmetrics.Accuracy()
        self.val_acc = torchmetrics.Accuracy()

    @implements(CdtModel)
    def inference_step(self, batch: TernarySample, *, stage: Stage) -> STEP_OUTPUT:
        assert isinstance(batch.x, Tensor)
        model_out = self.forward(x=batch.x, s=batch.s)
        logging_dict = {
            "laftr_loss": self._loss_laftr(y_pred=model_out.y, recon=model_out.x, batch=batch),
            "adv_loss": self._loss_adv(s_pred=model_out.s, batch=batch),
        }
        logging_dict = prefix_keys(dict_=logging_dict, prefix=str(stage), sep="/")
        self.log_dict(logging_dict)

        return {
            "targets": batch.y.view(-1),
            "subgroup_inf": batch.s.view(-1),
            "logits_y": model_out.y,
        }

    @implements(CdtModel)
    def inference_epoch_end(self, outputs: EPOCH_OUTPUT, stage: Stage) -> Dict[str, float]:
        targets_all = aggregate_over_epoch(outputs=outputs, metric="targets")
        subgroup_inf_all = aggregate_over_epoch(outputs=outputs, metric="subgroup_inf")
        logits_y_all = aggregate_over_epoch(outputs=outputs, metric="logits_y")

        preds_y_all = prediction(logits_y_all)

        dt = em.DataTuple(
            x=pd.DataFrame(
                torch.rand_like(subgroup_inf_all).detach().cpu().numpy(), columns=["x0"]
            ),
            s=pd.DataFrame(subgroup_inf_all.detach().cpu().numpy(), columns=["s"]),
            y=pd.DataFrame(targets_all.detach().cpu().numpy(), columns=["y"]),
        )

        results_dict = em.run_metrics(
            predictions=em.Prediction(hard=pd.Series(preds_y_all.detach().cpu().numpy())),
            actual=dt,
            metrics=[em.Accuracy(), em.RenyiCorrelation(), em.Yanovich()],
            per_sens_metrics=[em.Accuracy(), em.ProbPos(), em.TPR()],
        )

        return results_dict

    def _loss_adv(self, s_pred: Tensor, *, batch: TernarySample) -> Tensor:
        # For Demographic Parity, for EqOpp is a different loss term.
        if self.fairness is FairnessType.DP:
            unweighted_loss = self._adv_clf_loss(s_pred, batch.s.view(-1, 1))
            for s in (0, 1):
                mask = batch.s.view(-1) == s
                unweighted_loss[mask] /= mask.sum()
            loss = 1 - unweighted_loss.sum() / 2
        elif self.fairness is FairnessType.EO:
            unweighted_loss = self._adv_clf_loss(s_pred, batch.s.view(-1, 1))
            count = 0
            for s, y in itertools.product([0, 1], repeat=2):
                count += 1
                mask = (batch.s.view(-1) == s) & (batch.y.view(-1) == y)
                unweighted_loss[mask] /= mask.sum()
            loss = 2 - unweighted_loss.sum() / count
        elif self.fairness is FairnessType.EqOp:
            # TODO: How to best handle this if no +ve samples in the batch?
            unweighted_loss = self._adv_clf_loss(s_pred, batch.s.view(-1, 1))
            for s in (0, 1):
                mask = (batch.s.view(-1) == s) & (batch.y.view(-1) == 1)
                unweighted_loss[mask] /= mask.sum()
            unweighted_loss[batch.y.view(-1) == 0] *= 0.0
            loss = 2 - unweighted_loss.sum() / 2
        else:
            loss = s_pred.sum() * 0
        self.log(f"{self.fairness}_adv_loss", self.adv_weight * loss)
        return self.adv_weight * loss

    def _loss_laftr(self, y_pred: Tensor, *, recon: Tensor, batch: TernarySample) -> Tensor:
        clf_loss = self._clf_loss(y_pred, target=batch.y)
        recon_loss = self._recon_loss(recon, target=batch.x)
        self.log_dict(
            {"clf_loss": self.clf_weight * clf_loss, "recon_loss": self.recon_weight * recon_loss}
        )
        return self.clf_weight * clf_loss + self.recon_weight * recon_loss

    @implements(CdtModel)
    def configure_optimizers(
        self,
    ) -> Tuple[List[optim.Optimizer], List[Mapping[str, Union[LRScheduler, int, TrainingMode]]]]:
        laftr_params = itertools.chain(
            [*self.enc.parameters(), *self.dec.parameters(), *self.clf.parameters()]
        )
        adv_params = self.adv.parameters()

        opt_laftr = optim.AdamW(laftr_params, lr=self.lr, weight_decay=self.weight_decay)
        opt_adv = optim.AdamW(adv_params, lr=self.lr, weight_decay=self.weight_decay)

        sched_laftr = {
            "scheduler": CosineAnnealingWarmRestarts(
                optimizer=opt_laftr, T_0=self.lr_initial_restart, T_mult=self.lr_restart_mult
            ),
            "interval": self.lr_sched_interval.name,
            "frequency": self.lr_sched_freq,
        }
        sched_adv = {
            "scheduler": CosineAnnealingWarmRestarts(
                optimizer=opt_adv, T_0=self.lr_initial_restart, T_mult=self.lr_restart_mult
            ),
            "interval": self.lr_sched_interval.name,
            "frequency": self.lr_sched_freq,
        }

        return [opt_laftr, opt_adv], [sched_laftr, sched_adv]

    @implements(pl.LightningModule)
    def optimizer_step(
        self,
        epoch: int,
        batch_idx: int,
        optimizer: torch.optim.Optimizer,
        optimizer_idx: int,
        optimizer_closure: Any,
        on_tpu: bool,
        using_native_amp: bool,
        using_lbfgs: bool,
    ) -> None:
        # update main model every N steps
        if optimizer_idx == 0 and (batch_idx + 1) % self.disc_steps == 0:
            optimizer.step(closure=optimizer_closure)
        if optimizer_idx == 1:  # update discriminator opt every step
            optimizer.step(closure=optimizer_closure)

    @implements(pl.LightningModule)
    def training_step(self, batch: TernarySample, batch_idx: int, optimizer_idx: int) -> Tensor:
        assert isinstance(batch.x, Tensor)
        if optimizer_idx == 0:
            # Main model update
            self.set_requires_grad(self.adv, requires_grad=False)
            model_out = self.forward(x=batch.x, s=batch.s)
            laftr_loss = self._loss_laftr(y_pred=model_out.y, recon=model_out.x, batch=batch)
            adv_loss = self._loss_adv(s_pred=model_out.s, batch=batch)
            _acc = accuracy(logits=model_out.y, targets=batch.y)
            logging_dict = {
                "loss": (laftr_loss + adv_loss).item(),
                "model_loss": laftr_loss.item(),
                "acc": _acc,
            }
            loss = laftr_loss + adv_loss
        elif optimizer_idx == 1:
            # Adversarial update
            self.set_requires_grad([self.enc, self.dec, self.clf], requires_grad=False)
            self.set_requires_grad(self.adv, requires_grad=True)
            model_out = self.forward(x=batch.x, s=batch.s)
            adv_loss = self._loss_adv(s_pred=model_out.s, batch=batch)
            laftr_loss = self._loss_laftr(y_pred=model_out.y, recon=model_out.x, batch=batch)
            target = batch.y.view(-1).long()
            _acc = self.train_acc(model_out.y.argmax(-1), target)
            logging_dict = {
                "loss": (laftr_loss + adv_loss).item(),
                "adv_loss": adv_loss.item(),
                "acc": _acc,
            }
            loss = -(laftr_loss + adv_loss)
        else:
            raise RuntimeError("There should only be 2 optimizers, but 3rd received.")
        logging_dict = prefix_keys(dict_=logging_dict, prefix="train", sep="/")
        self.log_dict(logging_dict)
        return loss

    @staticmethod
    def set_requires_grad(nets: Union[nn.Module, List[nn.Module]], requires_grad: bool) -> None:
        """Change if gradients are tracked."""
        if not isinstance(nets, list):
            nets = [nets]
        for net in nets:
            for param in net.parameters():
                param.requires_grad = requires_grad

    @implements(nn.Module)
    def forward(self, x: Tensor, *, s: Tensor) -> ModelOut:
        embedding = self.enc(x)
        y_pred = self.clf(embedding)
        s_pred = self.adv(embedding)
        recon = self.dec(embedding, s)
        return ModelOut(y=y_pred, z=embedding, x=recon, s=s_pred)
