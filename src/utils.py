import argparse
import numpy as np
import os
import shutil
import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler

import src.models as models
from src.data import Dataset


class Metric(object):
    def __init__(self):
        self.cnt = 0
        self.h_1 = 0
        self.h_3 = 0
        self.h_10 = 0
        self.mr = 0
        self.mrr = 0

    def _normalize(self):
        return self.h_1 / self.cnt, self.h_3 / self.cnt, self.h_10 / self.cnt, self.mr / self.cnt, self.mrr / self.cnt

    def __str__(self):
        h_1, h_3, h_10, mr, mrr = self._normalize()
        return f'\nH@1: {h_1}\nH@3: {h_3}\nH@10: {h_10}\nMR: {mr}\nMRR: {mrr}\n'

    def __iter__(self):
        h_1, h_3, h_10, mr, mrr = self._normalize()
        yield 'metric/H1', h_1
        yield 'metric/H3', h_3
        yield 'metric/H10', h_10
        yield 'metric/MR', mr
        yield 'metric/MRR', mrr

    def update(self, r):
        self.cnt += 1

        if r < 2:
            self.h_1 += 1
        if r < 4:
            self.h_3 += 1
        if r < 11:
            self.h_10 += 1

        self.mr += r
        self.mrr += 1.0 / r


def get_args():
    argparser = argparse.ArgumentParser()

    argparser.add_argument('-ds', '--dataset', type=str, required=True)
    argparser.add_argument('-m', '--model', type=str, required=True, choices=['TTransE', 'TADistMult', 'TATransE'])
    argparser.add_argument('-d', '--dropout', type=float, default=0)
    argparser.add_argument('-l1', '--l1', default=False, action='store_true')
    argparser.add_argument('-es', '--embedding_size', type=int, default=128)
    argparser.add_argument('-mr', '--margin', type=int, default=1)
    argparser.add_argument('-lr', '--learning_rate', type=float, default=0.001)
    argparser.add_argument('-wd', '--weight_decay', type=float, default=0)
    argparser.add_argument('-e', '--epochs', type=int, default=1000)
    argparser.add_argument('-bs', '--batch_size', type=int, default=512)
    argparser.add_argument('-ns', '--negative_samples', type=int, default=1)
    argparser.add_argument('-f', '--filter', default=True, action='store_true')
    argparser.add_argument('-r', '--resume', type=str, default='')
    argparser.add_argument('-dt', '--deterministic', default=False, action='store_true')
    argparser.add_argument('-o', '--opt_level', type=str, default='O0', choices=['O0', 'O1', 'O2', 'O3'])
    argparser.add_argument('-t', '--test', default=False, action='store_true')
    argparser.add_argument('-md', '--mode', type=str, default='both', choices=['head', 'tail', 'both'])
    argparser.add_argument('-lf', '--log_frequency', type=int, default=100)
    argparser.add_argument('-w', '--workers', type=int, default=1)

    argparser.add_argument('--local_rank', type=int, default=0)  # NOTE: torch.distributed.launch

    args = argparser.parse_args()
    args.world_size = int(os.getenv('WORLD_SIZE', 1))

    return args


def get_data(args):
    bpath = os.path.join('./data', args.dataset)

    with open(os.path.join(bpath, 'entity2id.txt'), 'r') as f:
        e_idx_ln = int(f.readline().strip())
    with open(os.path.join(bpath, 'relation2id.txt'), 'r') as f:
        r_idx_ln = int(f.readline().strip())

    tr_ds = Dataset(args, os.path.join(bpath, 'train2id.txt'), e_idx_ln)
    vd_ds = Dataset(args, os.path.join(bpath, 'valid2id.txt'), e_idx_ln)
    ts_ds = Dataset(args, os.path.join(bpath, 'test2id.txt'), e_idx_ln, ns=False)

    t_idx = {e: i for i, e in enumerate(np.unique(np.concatenate([tr_ds, vd_ds, ts_ds], axis=1)[0, :, 3:].flatten()))}
    t_idx_ln = len(t_idx)

    tr_ds.transform(t_idx, ts_bs={})
    vd_ds.transform(t_idx, ts_bs=tr_ds._ts)
    ts_ds.transform(t_idx, ts=False)

    tr_smp = DistributedSampler(tr_ds, num_replicas=args.world_size, rank=args.local_rank)
    vd_smp = DistributedSampler(vd_ds, num_replicas=args.world_size, rank=args.local_rank)
    ts_smp = DistributedSampler(ts_ds, num_replicas=args.world_size, rank=args.local_rank)

    tr = DataLoader(tr_ds, batch_size=args.batch_size, sampler=tr_smp, num_workers=args.workers, pin_memory=True)
    vd = DataLoader(vd_ds, batch_size=args.batch_size, sampler=vd_smp, num_workers=args.workers, pin_memory=True)
    ts = DataLoader(ts_ds, batch_size=args.batch_size, sampler=ts_smp, num_workers=args.workers, pin_memory=True)

    return tr, vd, ts, e_idx_ln, r_idx_ln, t_idx_ln


def resume(args, mdl, opt, amp, dvc):
    if not os.path.exists(args.resume):
        raise FileNotFoundError('can\'t find the saved model with the given path')
    ckpt = torch.load(args.resume, map_location=dvc)
    mdl.load_state_dict(ckpt['mdl'])
    opt.load_state_dict(ckpt['opt'])
    if torch.cuda.is_available():
        amp.load_state_dict(ckpt['amp'])
    return ckpt['e'], ckpt['bst_ls']

def get_model(args, e_cnt, r_cnt, t_cnt, dvc):
    return getattr(models, args.model)(args, e_cnt, r_cnt, t_cnt, dvc)


def get_loss_f(args):
    if args.model == 'TADistMult':
        return nn.BCEWithLogitsLoss(reduction='mean')
    return nn.MarginRankingLoss(args.margin)


def get_loss(args, p, n, mdl, loss_f, dvc):
    p_s, p_o, p_r, p_t = p[:, 0], p[:, 1], p[:, 2], p[:, 3:].squeeze()
    n_s, n_o, n_r, n_t = n[:, 0], n[:, 1], n[:, 2], n[:, 3:].squeeze()

    if mdl.training:
        mdl.zero_grad()
    s_p, s_n = mdl(p_s, p_o, p_r, p_t, n_s, n_o, n_r, n_t)

    if args.model == 'TADistMult':
        x = torch.cat([s_p, s_n])
        y = torch.cat([torch.ones(s_p.shape), torch.zeros(s_n.shape)]).to(dvc)
        loss = loss_f(x, y)
    else:
        loss = loss_f(s_p, s_n, (-1) * torch.ones(s_p.shape + s_n.shape).to(dvc))

    return loss


def _p(args):
    return 1 if args.model == 'TTransE' and args.l1 else 2


def evaluate(args, b, mdl, mtr, dvc):
    ts_r, ts_t = b[:, 2], b[:, 3:].squeeze()
    if args.model == 'TTransE':
        rt_embed = mdl.module.r_embed(ts_r) + mdl.module.t_embed(ts_t)
    else:
        rt_embed = mdl.module.rt_embed(ts_r, ts_t)

    if args.mode != 'tail':
        o_embed = mdl.module.e_embed(b[:, 1])
        if args.model == 'TADistMult':
            ort = rt_embed * o_embed
            s_r = torch.matmul(ort, mdl.module.e_embed.weight.t()).argsort(dim=1, descending=True).cpu().numpy()
        else:
            ort = o_embed - rt_embed
            s_r = torch.cdist(
                mdl.module.e_embed.weight, ort, p=_p(args)).t().argsort(dim=1, descending=True).cpu().numpy()
        for i, s in enumerate(b[:, 0].cpu().numpy()):
            mtr.update(np.argwhere(s_r[i] == s)[0, 0] + 1)

    if args.mode != 'head':
        s_embed = mdl.module.e_embed(b[:, 0])
        if args.model == 'TADistMult':
            srt = s_embed * rt_embed
            o_r = torch.matmul(srt, mdl.module.e_embed.weight.t()).argsort(dim=1, descending=True).cpu().numpy()
        else:
            srt = s_embed + rt_embed
            o_r = torch.cdist(srt, mdl.module.e_embed.weight, p=_p(args)).argsort(dim=1, descending=True).cpu().numpy()
        for i, o in enumerate(b[:, 1].cpu().numpy()):
            mtr.update(np.argwhere(o_r[i] == o)[0, 0] + 1)


def checkpoint(args, e, mdl, opt, amp, bst_ls, is_bst):
    ckpt = {'e': e, 'mdl': mdl.state_dict(), 'opt': opt.state_dict(), 'bst_ls': bst_ls}
    if torch.cuda.is_available():
        ckpt['amp'] = amp.state_dict()

    bpth = os.path.join('./models', args.dataset)
    os.makedirs(bpth, exist_ok=True)

    inc = {'model', 'dropout', 'l1', 'embedding_size', 'margin', 'learning_rate', 'weight_decay', 'negative_samples'}
    fn = '-'.join(map(lambda x: f'{x[0]}_{x[1]}', sorted(filter(lambda x: x[0] in inc, vars(args).items())))) + '.ckpt'
    torch.save(ckpt, os.path.join(bpth, f'e_{e}-' + fn))

    if is_bst:
        shutil.copyfile(os.path.join(bpth, f'e_{e}-' + fn), os.path.join(bpth, f'bst-' + fn))
