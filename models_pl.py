from abc import ABC
from typing import Tuple

import lightning.pytorch as pl
import torch
import torch.nn.parallel
import torch.utils.data
import torchvision
from omegaconf import DictConfig
from rtpt import RTPT
from torch import nn

from simple_einet.conv_pc import ConvPcConfig, ConvPc
from simple_einet.data import get_data_shape
from simple_einet.dist import Dist, get_distribution
from simple_einet.einet import EinetConfig, Einet
from simple_einet.mixture import Mixture

# Translate the dataloader index to the dataset name
DATALOADER_ID_TO_SET_NAME = {0: "train", 1: "val", 2: "test"}


def make_einet(cfg, num_classes: int = 1) -> Mixture | Einet:
    """
    Make an Einet model based off the given arguments.

    Args:
        cfg: Arguments parsed from argparse.
        num_classes: Number of classes to model.

    Returns:
        Einet model.
    """

    image_shape = get_data_shape(cfg.dataset)
    # leaf_kwargs, leaf_type = {"total_count": 255}, Binomial
    leaf_kwargs, leaf_type = get_distribution(dist=cfg.dist, cfg=cfg)

    config = EinetConfig(
        num_features=image_shape.num_pixels,
        num_channels=image_shape.channels,
        depth=cfg.einet.D,
        num_sums=cfg.einet.S,
        num_leaves=cfg.einet.I,
        num_repetitions=cfg.einet.R,
        num_classes=num_classes,
        leaf_kwargs=leaf_kwargs,
        leaf_type=leaf_type,
        dropout=cfg.dropout,
        layer_type=cfg.einet.layer_type,
        structure=cfg.einet.structure,
    )
    if cfg.mixture:
        return Mixture(n_components=num_classes, config=config)
    else:
        return Einet(config)


def make_convpc(cfg, num_classes: int = 1) -> Mixture | ConvPc:
    """
    Make ConvPc model based off the given arguments.

    Args:
        cfg: Arguments parsed from argparse.
        num_classes: Number of classes to model.

    Returns:
        ConvPc model.
    """

    image_shape = get_data_shape(cfg.dataset)
    # leaf_kwargs, leaf_type = {"total_count": 255}, Binomial
    leaf_kwargs, leaf_type = get_distribution(dist=cfg.dist, cfg=cfg)

    config = ConvPcConfig(
        channels=cfg.convpc.channels,
        num_channels=image_shape.channels,
        num_classes=num_classes,
        leaf_kwargs=leaf_kwargs,
        leaf_type=leaf_type,
        structure=cfg.convpc.structure,
        order=cfg.convpc.order,
        kernel_size=cfg.convpc.kernel_size,
    )
    if cfg.mixture:
        return Mixture(n_components=num_classes, config=config, data_shape=image_shape)
    else:
        return ConvPc(config=config, data_shape=image_shape)


class LitModel(pl.LightningModule, ABC):
    """
    LightningModule for training a model using PyTorch Lightning.

    Args:
        cfg (DictConfig): Configuration dictionary.
        name (str): Name of the model.
        steps_per_epoch (int): Number of steps per epoch.

    Attributes:
        cfg (DictConfig): Configuration dictionary.
        image_shape (ImageShape): Shape of the input data.
        rtpt (RTPT): RTPT logger.
        steps_per_epoch (int): Number of steps per epoch.

    """

    def __init__(self, cfg: DictConfig, name: str, steps_per_epoch: int) -> None:
        super().__init__()
        self.cfg = cfg
        self.image_shape = get_data_shape(cfg.dataset)
        self.rtpt = RTPT(
            name_initials="SB",
            experiment_name="einet_" + name + ("_" + str(cfg.tag) if cfg.tag else ""),
            max_iterations=cfg.epochs + 1,
        )
        self.save_hyperparameters()
        self.steps_per_epoch = steps_per_epoch

    def preprocess(self, data: torch.Tensor):
        """Preprocess data before passing it to the model."""
        if self.cfg.dist == Dist.BINOMIAL:
            data *= 255.0

        return data

    def configure_optimizers(self):
        """
        Configure the optimizer and learning rate scheduler.
        """
        optimizer = torch.optim.Adam(self.parameters(), lr=self.cfg.lr)
        lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(
            optimizer,
            milestones=[int(0.7 * self.cfg.epochs), int(0.9 * self.cfg.epochs)],
            gamma=0.1,
        )
        return [optimizer], [lr_scheduler]

    def on_train_start(self) -> None:
        self.rtpt.start()

    def on_train_epoch_end(self) -> None:
        self.rtpt.step()


class SpnGenerative(LitModel):
    """
    A class representing a generative model based on Sum-Product Networks (SPNs).

    Args:
        cfg (DictConfig): A configuration dictionary.
        steps_per_epoch (int): The number of steps per epoch.

    Attributes:
        spn (einet.EinSumProductNetwork): The SPN model.
    """

    def __init__(self, cfg: DictConfig, steps_per_epoch: int):
        super().__init__(cfg=cfg, name="gen", steps_per_epoch=steps_per_epoch)
        if cfg.model == "einet":
            self.spn = make_einet(cfg, num_classes=cfg.num_classes)
        elif cfg.model == "convpc":
            self.spn = make_convpc(cfg, num_classes=cfg.num_classes)
        else:
            raise ValueError(f"Unknown model {cfg.model}")


    def training_step(self, train_batch, batch_idx):
        data, labels = train_batch
        data = self.preprocess(data)
        nll = self.negative_log_likelihood(data)
        self.log("Train/loss", nll, prog_bar=True)
        return nll

    def validation_step(self, val_batch, batch_idx):
        data, labels = val_batch
        data = self.preprocess(data)
        nll = self.negative_log_likelihood(data)
        self.log("Val/loss", nll, prog_bar=True)
        return nll

    def negative_log_likelihood(self, data, reduction="mean"):
        """
        Compute negative log likelihood of data.

        Args:
            data: Data to compute negative log likelihood of.
            reduction: Reduction method.

        Returns:
            Negative log likelihood of data.
        """
        nll = -1 * self.spn(data)
        if reduction == "mean":
            return nll.mean()
        elif reduction == "sum":
            return nll.sum()
        else:
            raise ValueError(f"Unknown reduction {reduction}")

    def generate_samples(self, num_samples: int, differentiable: bool):
        """
        Generates a batch of samples from the model.

        Args:
            num_samples (int): The number of samples to generate.
            differentiable (bool): Whether to use a differentiable sampling method.

        Returns:
            torch.Tensor: A tensor of shape (num_samples, *self.image_shape) containing the generated samples.
        """
        if not differentiable:
            samples = self.spn.sample(num_samples=num_samples, mpe_at_leaves=True).view(-1, *self.image_shape)
        else:
            samples = self.spn.sample(num_samples=num_samples, mpe_at_leaves=True, is_differentiable=True).view(
                -1, *self.image_shape
            )
        samples = samples / 255.0
        return samples

    def on_train_epoch_end(self):
        with torch.no_grad():
            samples = self.generate_samples(num_samples=64, differentiable=False)
            grid = torchvision.utils.make_grid(samples.data[:64], nrow=8, pad_value=0.0, normalize=True)
            self.logger.log_image(key="samples", images=[grid])

            # samples_diff = self.generate_samples(num_samples=64, differentiable=True)
            # grid_diff = torchvision.utils.make_grid(samples_diff.data[:64], nrow=8, pad_value=0.0, normalize=True)
            # self.logger.log_image(key="samples_diff", images=[grid_diff])

        super().on_train_epoch_end()

    def test_step(self, batch, batch_idx, dataloader_idx=0):
        data, labels = batch

        data = self.preprocess(data)
        nll = self.negative_log_likelihood(data)

        set_name = DATALOADER_ID_TO_SET_NAME[dataloader_idx]
        self.log(f"Test/{set_name}_nll", nll, add_dataloader_idx=False)


class SpnDiscriminative(LitModel):
    """
    Discriminative SPN model. Models the class conditional data distribution at its C root nodes.
    """

    def __init__(self, cfg: DictConfig, steps_per_epoch: int):
        super().__init__(cfg, name="disc", steps_per_epoch=steps_per_epoch)

        # Construct SPN
        if cfg.model == "einet":
            self.spn = make_einet(cfg, num_classes=cfg.num_classes)
        elif cfg.model == "convpc":
            self.spn = make_convpc(cfg, num_classes=cfg.num_classes)
        else:
            raise ValueError(f"Unknown model {cfg.model}")

        # Define loss function
        self.criterion = nn.NLLLoss()

    def training_step(self, train_batch, batch_idx):
        loss, accuracy = self._get_cross_entropy_and_accuracy(train_batch)
        self.log("Train/accuracy", accuracy, on_step=True, prog_bar=True)
        self.log("Train/loss", loss, on_step=True)
        return loss

    def validation_step(self, val_batch, batch_idx):
        loss, accuracy = self._get_cross_entropy_and_accuracy(val_batch)
        self.log("Val/accuracy", accuracy, prog_bar=True)
        self.log("Val/loss", loss)
        return loss

    def test_step(self, batch, batch_idx, dataloader_idx=0):
        loss, accuracy = self._get_cross_entropy_and_accuracy(batch)
        set_name = DATALOADER_ID_TO_SET_NAME[dataloader_idx]
        self.log(f"Test/{set_name}_accuracy", accuracy, add_dataloader_idx=False)

    def _get_cross_entropy_and_accuracy(self, batch) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Compute cross entropy loss and accuracy of batch.
        Args:
            batch: Batch of data.

        Returns:
            Tuple of (cross entropy loss, accuracy).
        """
        data, labels = batch
        data = self.preprocess(data)

        ll_y_g_x = self.spn.posterior(data)

        # Criterion is NLL which takes logp( y | x)
        # NOTE: Don't use nn.CrossEntropyLoss because it expects unnormalized logits
        # and applies LogSoftmax first
        loss = self.criterion(ll_y_g_x, labels)
        accuracy = (labels == ll_y_g_x.argmax(-1)).sum() / ll_y_g_x.shape[0]
        return loss, accuracy
