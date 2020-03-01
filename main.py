import logging
import numpy as np
import sys
import time
import torch
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm

import src.data as data
import src.utils as utils


def _logger():
    logging.basicConfig(format='%(message)s', stream=sys.stdout, level=logging.INFO)
    return logging.getLogger()


def main(gpu):
    logger = _logger()

    dvc = torch.device(f'cuda:{gpu}' if torch.cuda.is_available() else 'cpu')
    logger.info(f'device: {dvc}\n')

    args = utils.get_args()
    logger.info('\n'.join(map(lambda x: f'{x[0]}: {x[1]}', sorted(vars(args).items()))) + '\n')

    torch.manual_seed(args.seed)

    tr, vd, ts, e_idx_ln, r_idx_ln, t_idx_ln = utils.get_data(args, gpu)

    mdl = utils.get_model(args, e_idx_ln, r_idx_ln, t_idx_ln, dvc).to(dvc)
    loss_f = utils.get_loss_f(args).to(dvc)
    optim = torch.optim.Adam(mdl.parameters(), lr=args.learning_rate)

    tb_sw = SummaryWriter()

    if not args.test:
        for epoch in range(args.epochs):
            tr_loss = 0
            st_tm = time.time()
            mdl.train()
            with tqdm(total=len(tr), desc=f'Epoch {epoch + 1}/{args.epochs}') as t:
                for i, (p, n) in enumerate(tr):
                    p = p.view(-1, p.shape[-1]).to(dvc)
                    n = n.view(-1, n.shape[-1]).to(dvc)

                    loss = utils.get_loss(args, p, n, mdl, loss_f, dvc)
                    loss.backward()
                    optim.step()
                    tr_loss += loss.item()

                    tb_sw.add_scalars(f'epoch/{epoch}', {'loss': loss.item(), 'mean_loss': tr_loss / (i + 1)}, i)

                    t.set_postfix(loss=f'{tr_loss / (i + 1):.4f}')
                    t.update()

            el_tm = time.time() - st_tm
            tr_loss /= len(tr)
            logger.info(f'Epoch {epoch + 1}/{args.epochs}: training_loss={tr_loss:.4f}, time={el_tm:.4f}')

            tb_sw.add_scalar(f'loss/train', tr_loss, epoch)

            if (epoch + 1) % args.log_frequency == 0 or epoch == (args.epochs - 1):
                vd_loss = 0
                st_tm = time.time()
                mdl.eval()
                for i, (p, n) in enumerate(vd):
                    p = p.view(-1, p.shape[-1]).to(dvc)
                    n = n.view(-1, n.shape[-1]).to(dvc)

                    loss = utils.get_loss(args, p, n, mdl, loss_f, dvc)
                    vd_loss += loss.item()

                el_tm = time.time() - st_tm
                vd_loss /= len(vd)
                logger.info(f'Epoch {epoch + 1}/{args.epochs}: validation_loss={vd_loss:.4f}, time={el_tm:.4f}')

                tb_sw.add_scalar(f'loss/validation', vd_loss, epoch)

                utils.save(args, mdl)

    mtr = utils.Metric()
    mdl.eval()
    for b in ts:
        b = b.view(-1, b.shape[-1]).to(dvc)
        utils.evaluate(args, b, mdl, mtr, dvc)
    logger.info(mtr)

    tb_sw.add_hparams(vars(args), dict(mtr))

    tb_sw.close()


if __name__ == '__main__':
    main(0)
