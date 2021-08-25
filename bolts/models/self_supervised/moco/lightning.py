from __future__ import annotations
from typing import Optional, Protocol, cast

from kit import implements, parsable
from kit.torch.loss import CrossEntropyLoss, ReductionType
import pytorch_lightning as pl
import torch
from torch import Tensor, optim
import torch.nn as nn
import torch.nn.functional as F
from torchvision.models import resnet
from torchvision.models.resnet import ResNet

from bolts.data.datamodules.vision.base import PBVisionDataModule
from bolts.data.datasets.utils import ImageTform
from bolts.data.structures import NamedSample
from bolts.models.base import PBModel
from bolts.models.erm import FineTuner
from bolts.models.self_supervised.base import SelfDistillation, SelfSupervisedModel
from bolts.models.self_supervised.moco.transforms import (
    TwoCropsTransform,
    moco_eval_transform,
)
from bolts.models.utils import precision_at_k
from bolts.types import MetricDict

from .utils import MemoryBank, ResNetArch, concat_all_gather

__all__ = ["MoCoV2"]


class EncoderFn(Protocol):
    def __call__(self, **kwargs) -> resnet.ResNet:
        ...


class MoCoV2(SelfDistillation):
    eval_clf: FineTuner
    student: ResNet
    teacher: ResNet
    use_ddp: bool

    @parsable
    def __init__(
        self,
        *,
        arch: ResNetArch = ResNetArch.resnet18,
        emb_dim: int = 128,
        num_negatives: int = 65_536,
        momentum_teacher: float = 0.999,
        temp: float = 0.07,
        lr: float = 0.03,
        momentum: float = 0.9,
        weight_decay: float = 1.0e-4,
        use_mlp: bool = False,
        eval_epochs: int = 100,
        eval_batch_size: Optional[int] = None,
    ) -> None:
        """
        PyTorch Lightning implementation of `MoCo <https://arxiv.org/abs/2003.04297>`_
        Paper authors: Xinlei Chen, Haoqi Fan, Ross Girshick, Kaiming He.
        Code adapted from `facebookresearch/moco <https://github.com/facebookresearch/moco>`

        Args:
            arch: ResNet architecture to use for the encoders.
            emb_dim: feature dimension (default: 128)
            num_negatives: queue size; number of negative keys (default: 65536)
            encoder_momentum: moco momentum of updating key encoder (default: 0.999)
            temp: softmax temperature (default: 0.07)
            lr: the learning rate
            momentum: optimizer momentum
            weight_decay: optimizer weight decay
            use_mlp: add an mlp to the encoders
        """
        super().__init__(
            lr=lr,
            weight_decay=weight_decay,
            eval_epochs=eval_epochs,
            eval_batch_size=eval_batch_size,
        )
        self._arch_fn = cast(EncoderFn, arch.value)
        self.emb_dim = emb_dim
        self.temp = temp
        self.lr = lr
        self.weight_decay = weight_decay
        self.momentum_teacher = momentum_teacher
        self.momentum = momentum

        self.num_negatives = num_negatives
        # create the queue
        self.mb = MemoryBank(dim=emb_dim, capacity=num_negatives)
        self.use_mlp = use_mlp
        self._loss_fn = CrossEntropyLoss(reduction=ReductionType.mean)

    @implements(PBModel)
    def _build(self) -> None:
        self.use_ddp = "ddp" in str(self.trainer.distributed_backend)
        if isinstance(self.datamodule, PBVisionDataModule):
            # self._datamodule.train_transforms = mocov2_transform()
            self.datamodule.train_transforms = TwoCropsTransform.with_mocov2_transform()
            self.datamodule.test_transforms = moco_eval_transform(train=False)

    @property
    @implements(SelfSupervisedModel)
    def features(self) -> nn.Module:
        return self.student

    @property
    @implements(SelfDistillation)
    def momentum_schedule(self) -> float:
        return self.momentum_teacher

    @torch.no_grad()
    @implements(SelfDistillation)
    def _init_encoders(self) -> tuple[resnet.ResNet, resnet.ResNet]:
        # create the encoders
        # num_classes is the output fc dimension
        student = self._arch_fn(num_classes=self.emb_dim)
        teacher = self._arch_fn(num_classes=self.emb_dim)

        # key and query encoders start with the same weights
        teacher.load_state_dict(student.state_dict())  # type: ignore

        if self.use_mlp:  # hack: brute-force replacement
            dim_mlp = self.student.fc.weight.shape[1]
            student.fc = nn.Sequential(  # type: ignore
                nn.Linear(dim_mlp, dim_mlp), nn.ReLU(), student.fc
            )
            teacher.fc = nn.Sequential(  # type: ignore
                nn.Linear(dim_mlp, dim_mlp), nn.ReLU(), teacher.fc
            )

        # there is no backpropagation through the key-encoder, so no need for gradients
        for p in teacher.parameters():
            p.requires_grad = False

        return student, teacher

    @implements(pl.LightningModule)
    def configure_optimizers(self) -> optim.Optimizer:
        optimizer = optim.SGD(
            self.student.parameters(),
            self.lr,
            momentum=self.momentum,
            weight_decay=self.weight_decay,
        )
        return optimizer

    @torch.no_grad()
    def _dequeue_and_enqueue(self, keys: Tensor) -> None:
        # gather keys before updating queue
        if self.use_ddp:
            keys = concat_all_gather(keys)
        self.mb.push(keys)

    @torch.no_grad()
    def _batch_shuffle_ddp(self, x: Tensor) -> tuple[Tensor, Tensor]:  # pragma: no-cover
        """
        Batch shuffle, for making use of BatchNorm.
        *** Only supports DistributedDataParallel (DDP) model. ***
        """
        # gather from all gpus
        batch_size_this = x.shape[0]
        x_gather = concat_all_gather(x)
        batch_size_all = x_gather.shape[0]

        num_gpus = batch_size_all // batch_size_this

        # random shuffle index
        idx_shuffle = torch.randperm(batch_size_all).cuda()

        # broadcast to all gpus
        torch.distributed.broadcast(idx_shuffle, src=0)  # type: ignore

        # index for restoring
        idx_unshuffle = torch.argsort(idx_shuffle)

        # shuffled index for this gpu
        gpu_idx = torch.distributed.get_rank()  # type: ignore
        idx_this = idx_shuffle.view(num_gpus, -1)[gpu_idx]

        return x_gather[idx_this], idx_unshuffle

    @torch.no_grad()
    def _batch_unshuffle_ddp(self, x: Tensor, idx_unshuffle: Tensor) -> Tensor:  # pragma: no-cover
        """
        Undo batch shuffle.
        *** Only support DistributedDataParallel (DDP) model. ***
        """
        # gather from all gpus
        batch_size_this = x.shape[0]
        x_gather = concat_all_gather(x)
        batch_size_all = x_gather.shape[0]

        num_gpus = batch_size_all // batch_size_this

        # restored index for this gpu
        gpu_idx = torch.distributed.get_rank()  # type: ignore
        idx_this = idx_unshuffle.view(num_gpus, -1)[gpu_idx]

        return x_gather[idx_this]

    def _get_loss(self, img_q: Tensor, img_k: Tensor) -> Tensor:
        """
        Input:
            im_q: a batch of query images
            im_k: a batch of key images
        Output:
            logits, targets
        """

        # compute query features
        student_out = self.student(img_q)  # queries: NxC
        student_out = F.normalize(student_out, dim=1)

        # compute key features
        with torch.no_grad():  # no gradient to keys
            # shuffle for making use of BN
            idx_unshuffle = None
            if self.use_ddp:
                img_k, idx_unshuffle = self._batch_shuffle_ddp(img_k)

            teacher_out = self.teacher(img_k)  # keys: NxC
            teacher_out = F.normalize(teacher_out, dim=1)

            # undo shuffle
            if self.use_ddp:
                assert idx_unshuffle is not None
                teacher_out = self._batch_unshuffle_ddp(teacher_out, idx_unshuffle)

        # compute logits
        # Einstein sum is more intuitive
        # positive logits: Nx1
        l_pos = torch.einsum('nc,nc->n', [student_out, teacher_out]).unsqueeze(-1)
        # negative logits: NxK
        l_neg = torch.einsum('nc,kc->nk', [student_out, self.mb.memory.clone()])

        # logits: Nx(1+K)
        logits = torch.cat([l_pos, l_neg], dim=1)

        # apply temperature
        logits /= self.temp

        # dequeue and enqueue
        self._dequeue_and_enqueue(teacher_out)

        return logits

    @implements(pl.LightningModule)
    def training_step(self, batch: NamedSample, batch_idx: int) -> MetricDict:
        assert isinstance(batch.x, list)
        img_1, img_2 = batch.x
        logits = self._get_loss(img_q=img_1, img_k=img_2)
        targets = logits.new_zeros(size=(logits.size(0),))
        loss = self._loss_fn(input=logits, target=targets)
        acc1, acc5 = precision_at_k(logits, targets, top_k=(1, 5))

        log = {'train_loss': loss.detach(), 'train_acc1': acc1, 'train_acc5': acc5}
        return {'loss': loss, 'log': log, 'progress_bar': log}

    @property
    @implements(SelfDistillation)
    def _eval_train_transform(self) -> ImageTform:
        return moco_eval_transform(train=True)

    @implements(SelfDistillation)
    @torch.no_grad()
    def _init_eval_clf(self) -> FineTuner:
        return FineTuner(
            encoder=self.student,
            classifier=nn.Linear(in_features=self.emb_dim, out_features=self.datamodule.card_y),
        )
