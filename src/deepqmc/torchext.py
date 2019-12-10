import numpy as np
import torch
import torch.nn.functional as F
from torch import nn

from .utils import InfoException, batch_eval


def merge_tensors(mask, source_true, source_false):
    x = torch.empty_like(mask, dtype=source_false.dtype)
    x[mask] = source_true
    x[~mask] = source_false
    return x


def triu_flat(x):
    i, j = np.triu_indices(x.shape[1], k=1)
    return x[:, i, j, ...]


def ssp(*args, **kwargs):
    return F.softplus(*args, **kwargs) - np.log(2)


class SSP(nn.Softplus):
    def forward(self, xs):
        return ssp(xs, self.beta, self.threshold)


def get_log_dnn(start_dim, end_dim, activation_factory, last_bias=True, *, n_layers):
    qs = [k / n_layers for k in range(n_layers + 1)]
    dims = [int(np.round(start_dim ** (1 - q) * end_dim ** q)) for q in qs]
    return get_custom_dnn(dims, activation_factory, last_bias=last_bias)


def get_custom_dnn(dims, activation_factory, last_bias=True):
    n_layers = len(dims) - 1
    modules = []
    for k in range(n_layers):
        bias = k + 1 < n_layers or last_bias
        modules.extend(
            [nn.Linear(dims[k], dims[k + 1], bias=bias), activation_factory()]
        )
    return nn.Sequential(*modules[:-1])


class LUFactError(InfoException):
    pass


class BDet(torch.autograd.Function):
    @staticmethod
    def forward(ctx, Xs):
        lus, pivots, is_fail = Xs.lu(get_infos=True)
        is_fail = is_fail == 1
        if is_fail.any():
            idxs = torch.arange(len(Xs))[is_fail]
            raise LUFactError({'idxs': idxs, 'dets': list(map(torch.det, Xs[idxs]))})
        idx = torch.arange(1, Xs.shape[-1] + 1, dtype=torch.int32).to(pivots.device)
        changed_sign = (pivots != idx).sum(dim=-1) % 2 == 1
        udets = lus.diagonal(dim1=-2, dim2=-1).prod(dim=-1)
        dets = torch.where(changed_sign, -udets, udets)
        # the need for clone() is probably related to
        # https://github.com/pytorch/pytorch/issues/18619
        ctx.save_for_backward(Xs, dets.clone())
        return dets

    @staticmethod
    def backward(ctx, vs):
        Xs, ys = ctx.saved_tensors
        return BDetBackward.apply(Xs, vs, ys)


class BDetBackward(torch.autograd.Function):
    @staticmethod
    def forward(ctx, Xs, vs, ys):
        vys = vs * ys
        # inverse() seems to be limited in batch dimension on CUDA
        Ks = batch_eval(lambda x: x.inverse(), Xs.split(2 ** 16 - 1))
        ctx.save_for_backward(Xs, Ks, ys, vys)
        return vys[..., None, None] * Ks.transpose(-1, -2)

    @staticmethod
    def backward(ctx, Vps):
        Xs, Ks, ys, vys = ctx.saved_tensors
        TrKVps = (Ks.transpose(-1, -2) * Vps).sum(dim=(-1, -2))
        grad_Xs = BDetDoubleBackward.apply(Xs, Vps, Ks, vys, TrKVps)
        grad_vs = ys * TrKVps
        return grad_Xs, grad_vs, None


class BDetDoubleBackward(torch.autograd.Function):
    @staticmethod
    def forward(ctx, Xs, Vps, Ks, vys, TrKVps):
        vyKs = vys[..., None, None] * Ks
        KVps = Ks @ Vps
        ctx.save_for_backward(Xs, Vps, Ks, vys, TrKVps, vyKs, KVps)
        tmp1 = vyKs * TrKVps[..., None, None]
        tmp2 = KVps @ vyKs
        return (tmp1 - tmp2).transpose(-1, -2)

    @staticmethod
    def backward(ctx, Vpps):
        Xs, Vps, Ks, vys, TrKVps, vyKs, KVps = ctx.saved_tensors
        TrKVpps = (Ks.transpose(-1, -2) * Vpps).sum(dim=(-1, -2))
        KVpps = Ks @ Vpps
        grad_Xs = BDetTripleBackward.apply(
            Xs, Vpps, Ks, vys, Vps, TrKVps, TrKVpps, KVps, KVpps, vyKs
        )
        grad_Vps = vyKs * TrKVpps[..., None, None] - KVpps @ vyKs
        grad_Vps = grad_Vps.transpose(-1, -2)
        return grad_Xs, grad_Vps, None, None, None


class BDetTripleBackward(torch.autograd.Function):
    @staticmethod
    def forward(ctx, Xs, Vpps, Ks, vys, Vps, TrKVps, TrKVpps, KVps, KVpps, vyKs):
        TrKVpKVpps = (KVps.transpose(-1, -2) * KVpps).sum(dim=(-1, -2))
        contractions = (
            KVps @ KVpps
            + KVpps @ KVps
            - KVps * TrKVpps[..., None, None]
            - KVpps * TrKVps[..., None, None]
        )
        return (
            vyKs * (TrKVps * TrKVpps - TrKVpKVpps)[..., None, None]
            + contractions @ vyKs
        ).transpose(-1, -2)


bdet = BDet.apply
