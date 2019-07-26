from functools import partial
from itertools import cycle

import torch
from torch.nn.utils import clip_grad_norm_
from torch.utils.data import DataLoader, TensorDataset

from .physics import local_energy
from .sampling import samples_from
from .stats import outlier_mask
from .utils import NULL_DEBUG, state_dict_copy


def loss_local_energy(Es_loc, psis, psi0s, E_ref=None, p=1):
    assert psis.grad_fn is None
    ws = psis ** 2 / psi0s ** 2
    ws = ws / ws.mean()
    E0 = E_ref if E_ref is not None else (ws * Es_loc).mean()
    return (ws * (Es_loc - E0).abs() ** p).mean()


def loss_total_energy_indirect(Es_loc, psis, psi0s):
    assert Es_loc.grad_fn is None
    ws = psis.detach() ** 2 / psi0s ** 2
    ws = ws / ws.mean()
    E0 = (ws * Es_loc).mean()
    return 2 * (ws * psis / psis.detach() * (Es_loc - E0)).mean()


def loss_least_squares(y_pred, y_true):
    return ((y_pred - y_true) ** 2).mean()


def fit_wfnet_multi(wfnet, loss_funcs, opts, gen_factory, gen_kwargs, writers):
    for loss_func, opt, kwargs, writer in zip(loss_funcs, opts, gen_kwargs, writers):
        with writer:
            fit_wfnet(wfnet, loss_func, opt, gen_factory(**kwargs), writer=writer)


def fit_wfnet(
    wfnet,
    loss_func,
    opt,
    sample_gen,
    indirect=False,
    clip_grad=None,
    writer=None,
    start=0,
    debug=NULL_DEBUG,
    scheduler=None,
    epoch_size=100,
    skip_outliers=True,
    p=0.01,
    q=4,
    sub_batch_size=int(1e18),
):
    for step, (rs, psi0s) in enumerate(sample_gen, start=start):
        d = debug[step]
        d['psi0s'], d['rs'] = psi0s, rs
        Es_loc, outliers, loss = (
            torch.tensor([], device=rs.device),
            torch.tensor([], dtype=torch.uint8, device=rs.device),
            0,
        )
        for (rs_sub, psi0s_sub) in zip(
            *map(
                partial(torch.split, split_size_or_sections=sub_batch_size, dim=0),
                (rs, psi0s),
            )
        ):
            Es_loc_sub, psis_sub = local_energy(
                rs_sub, wfnet, create_graph=not indirect, keep_graph=indirect
            )
            outliers_sub = (
                outlier_mask(Es_loc_sub, p, q)[0]
                if skip_outliers
                else torch.zeros_like(Es_loc_sub, dtype=torch.uint8)
            )
            loss_sub = loss_func(
                Es_loc_sub[~outliers_sub],
                psis_sub[~outliers_sub],
                psi0s_sub[~outliers_sub],
            )
            loss_sub.backward()
            loss += loss_sub.detach()
            Es_loc = torch.cat((Es_loc, Es_loc_sub.detach()))
            outliers = torch.cat((outliers, outliers_sub))
        d['Es_loc'] = Es_loc
        del Es_loc_sub, loss_sub, psis_sub
        if writer:
            writer.add_scalar('loss', loss, step)
            writer.add_scalar('E_loc/mean', Es_loc.mean(), step)
            writer.add_scalar('E_loc/var', Es_loc.var(), step)
            if skip_outliers:
                writer.add_scalar('E_loc/mean0', Es_loc[~outliers].mean(), step)
                writer.add_scalar('E_loc/var0', Es_loc[~outliers].var(), step)
            for label, value in wfnet.tracked_parameters():
                writer.add_scalar(f'param/{label}', value, step)
        if clip_grad:
            clip_grad_norm_(wfnet.parameters(), clip_grad)
        opt.step()
        opt.zero_grad()
        d['state_dict'] = state_dict_copy(wfnet)
        if scheduler and (step + 1) % epoch_size == 0:
            scheduler.step()


def wfnet_fit_driver(
    sampler,
    *,
    samplings,
    n_epochs,
    n_sampling_steps,
    batch_size=10_000,
    n_discard=0,
    n_decorrelate=0,
    range_sampling=range,
    range_training=range,
):
    for _ in samplings:
        sampler.recompute_forces()
        rs, psis, _ = samples_from(
            sampler,
            range_sampling(n_sampling_steps),
            n_discard=n_discard,
            n_decorrelate=n_decorrelate,
        )
        samples_ds = TensorDataset(rs.flatten(end_dim=1), psis.flatten(end_dim=1))
        rs_dl = DataLoader(samples_ds, batch_size=batch_size, shuffle=True)
        n_steps = n_epochs * len(rs_dl)
        for _, (rs, psis) in zip(range_training(n_steps), cycle(rs_dl)):
            yield rs, psis


def wfnet_fit_driver_simple(
    sampler, *, samplings, n_sampling_steps, n_discard=0, n_decorrelate=0
):
    for _ in samplings:
        rs, psis, _ = samples_from(
            sampler,
            range(n_sampling_steps),
            n_discard=n_discard,
            n_decorrelate=n_decorrelate,
        )
        yield rs.flatten(end_dim=1), psis.flatten(end_dim=1)


def fit_wfnet_supervised(
    fit_net,
    true_net,
    loss_func,
    opt,
    sample_gen,
    correlated_sampling=True,
    acc_grad=1,
    writer=None,
    start=0,
    debug=NULL_DEBUG,
    scheduler=None,
    epoch_size=100,
):
    for step, (rs, psi0s) in enumerate(sample_gen, start=start):
        d = debug[step]
        d['psi0s'], d['rs'] = psi0s, rs
        psis_fit = fit_net(rs)
        psis_true = true_net(rs)
        loss = loss_func(psis_fit, psis_true)
        if writer:
            writer.add_scalar('loss', loss, step)
            for label, value in fit_net.tracked_parameters():
                writer.add_scalar(f'param/{label}', value, step)
        loss.backward()
        if (step + 1) % acc_grad == 0:
            opt.step()
            opt.zero_grad()
        d['state_dict'] = state_dict_copy(fit_net)
        if scheduler and (step + 1) % epoch_size == 0:
            scheduler.step()
