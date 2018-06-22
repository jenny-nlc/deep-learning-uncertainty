import argparse
import datetime

import numpy as np
from sklearn.metrics import roc_curve, roc_auc_score, precision_recall_curve, auc
import matplotlib.pyplot as plt
from skimage import transform
from tqdm import tqdm

from _context import dl_uncertainty

from dl_uncertainty.data import DataLoader, datasets
from dl_uncertainty import dirs, training
from dl_uncertainty import data_utils, model_utils
from dl_uncertainty.utils.visualization import view_predictions
from dl_uncertainty.processing.shape import fill_to_shape
from dl_uncertainty.processing.data_augmentation import random_crop
from dl_uncertainty.models.odin import Odin

# Use "--trainval" only for training on "trainval" and testing "test".
# CUDA_VISIBLE_DEVICES=0 python test_threshold_ood.py
# CUDA_VISIBLE_DEVICES=1 python test_threshold_ood.py
# CUDA_VISIBLE_DEVICES=2 python test_threshold_ood.py
#   cifar wrn 28 10 /home/igrubisic/projects/dl-uncertainty/data/nets/cifar-trainval/wrn-28-10/2018-04-28-1926/Model
#   cifar dn 100 12 /home/igrubisic/projects/dl-uncertainty/data/nets/cifar-trainval/dn-100-12-e300/2018-05-28-0121/Model
#   cifar rn 34 8
#   cityscapes dn 121 32 /home/igrubisic/projects/dl-uncertainty/data/nets/cityscapes-train/dn-121-32-pretrained-e30/2018-05-16-1623/Model
#   mozgalo rn 50 64
#   mozgalo rn 18 64
#   mozgalo dn 100 12 /home/igrubisic/projects/dl-uncertainty/data/nets/mozgalo-trainval/dn-100-12-e10/2018-05-30-0144/Model
#   mozgalooodtrain dn 100 12 /home/igrubisic/projects/dl-uncertainty/data/nets/mozgalo-trainval/dn-100-12-e10/2018-05-30-1857/Model

parser = argparse.ArgumentParser()
parser.add_argument('ds', type=str)
parser.add_argument('net', type=str)
parser.add_argument('depth', type=int)
parser.add_argument('width', type=int)
parser.add_argument('saved_path', type=str)
parser.add_argument('--dropout', action='store_true')
parser.add_argument('--mcdropout', action='store_true')
parser.add_argument('--test_on_training_set', action='store_true')
parser.add_argument('--ds_size', default=150, type=int)
args = parser.parse_args()
print(args)

# Helper functions


def map_to_dict(f, keys):
    return {k: f(k) for k in keys}


def map_dict_v(f, d):
    return {k: f(v) for k, v in d.items()}


def resize(x, shape):
    x_min, x_max = np.min(x), np.max(x)
    s = x_max - x_min
    x = (x - x_min) / s  # values need to be in [0,1]
    x = transform.resize(x, shape, order=1, clip=False)
    return s * x + x_min  # restore value scaling


# Cached dataset with normalized inputs

print("Setting up data loading...")
test_dataset_ids = ['cifar', 'tinyimagenet', 'isun', 'mozgalo', 'cityscapes']
if args.ds == 'mozgalooodtrain':
    test_dataset_ids.remove('mozgalo')
    test_dataset_ids.append('mozgaloood')
    test_dataset_ids.append('mozgalooodtrain')

get_ds = data_utils.get_cached_dataset_with_normalized_inputs
# test sets
ds_id_to_ds = map_to_dict(lambda ds_id: get_ds(ds_id, trainval_test=True)[1],
                          test_dataset_ids)
# val sets
ds_id_to_ds_val = map_to_dict(lambda ds_id: get_ds(ds_id)[1], test_dataset_ids)

for ds_id, ds in ds_id_to_ds.items():
    print(ds_id, len(ds))  # print dataset length


def take_subsets(ds_id_to_ds_dict, size=args.ds_size):
    return map_dict_v(lambda ds: ds.permute().subset(np.arange(size)),
                      ds_id_to_ds_dict)


ds_id_to_ds_val, ds_id_to_ds = map(take_subsets, [ds_id_to_ds_val, ds_id_to_ds])

# prepare cropped and resized datasets
shape = ds_id_to_ds[args.ds][0][0].shape
size = len(ds_id_to_ds[args.ds])
for ds_id, ds in list(ds_id_to_ds.items()):
    if ds_id == args.ds:
        continue
    if args.ds == 'cifar' and ds_id in ['tinyimagenet', 'cityscapes']:
        ds_id_to_ds[ds_id + '-crop'] = \
            ds.map(lambda d: random_crop(d, shape[:2]), 0, func_name='crop')
    ds_id_to_ds[ds_id + '-res'] = \
        ds.map(lambda d: resize(d, shape[:2]), 0, func_name='resize')
    #ds_id_to_ds[ds_id + '-plus'] = \
    #    ds.map(lambda d: resize(d, shape[:2])*3+3, 0, func_name='resize')
    del ds_id_to_ds[ds_id]

# add noise datasets
ds_id_to_ds['gaussian'] = \
    datasets.WhiteNoiseDataset(shape, size=size).map(lambda x: (x, -1))
ds_id_to_ds['uniform'] = datasets.WhiteNoiseDataset(
    shape, size=size, uniform=True).map(lambda x: (x, -1))

# Model

print("Initializing model and loading state...")
model = model_utils.get_model(
    net_name=args.net,
    ds_train=ds_id_to_ds[args.ds],
    depth=args.depth,
    width=args.width,
    epoch_count=1,
    dropout=args.dropout or args.mcdropout)
model.load_state(args.saved_path)

# Logits


def to_logits(xy, label=0):
    x, _ = xy
    logits, output = model.predict(
        x, single_input=True, outputs=['logits', 'output'])
    return logits, label  # int(output != y)


ds_id_to_logits_ds = {
    k: v.map(
        lambda x: to_logits(x, label=int(k == args.ds)),
        func_name='to_logits').cache()
    for k, v in ds_id_to_ds.items()
}

ds_id_to_max_logits_ds = map_dict_v(
    lambda v: v.map(lambda xy: (np.max(xy[0]), xy[1])), ds_id_to_logits_ds)
ds_id_to_sum_logits_ds = map_dict_v(
    lambda v: v.map(lambda xy: (np.sum(xy[0]), xy[1])), ds_id_to_logits_ds)

max_logits_ds = ds_id_to_max_logits_ds[args.ds]

y_score_in, y_true_in = zip(* [(x, y) for x, y in max_logits_ds])

# Max-logits-sum-logits distributions plots

fig, axes = plt.subplots(
    2, (len(ds_id_to_max_logits_ds) + 1) // 2,
    figsize=(40, 16),
    sharex=True,
    sharey=True)

ds_ids = ds_id_to_max_logits_ds.keys()
for ds_id, ax in zip(ds_ids, axes.flat):
    max_logits = [d[0] for d in ds_id_to_max_logits_ds[ds_id]]
    sum_logits = [d[0] for d in ds_id_to_sum_logits_ds[ds_id]]
    ax.set_title(ds_id)
    ax.scatter(max_logits, sum_logits, alpha=0.5, s=2, edgecolors='none')
for ax in axes[1, :]:
    ax.set_xlabel('max logits')
for ax in axes[:, 0]:
    ax.set_ylabel('sum logits')
plt.show()

# Max-logits distributions, evaluation curves plots

fig, axes = plt.subplots(
    4,
    len(ds_id_to_max_logits_ds),
    figsize=(40, 16),
    sharex='row',
    sharey='row')


def plot_roc(ax, fpr, tpr, fpr95, det_err):
    ax.plot(fpr, fpr, linestyle='--')
    ax.plot(fpr, tpr, label=f'{auc(fpr, tpr):.3f}')
    ax.plot([fpr95], [0.95], label=f'FPR@95%={fpr95:.3f}')
    ax.plot([fpr95], [0.95], label=f'd_err={det_err:.3f}')
    ax.set_xlabel('FPR')
    if i == 0:
        ax.set_ylabel('TPR')
    ax.legend()


def plot_pr(ax, p, p_interp, r, name):
    ax.plot(r, p, label=f'{auc(r, p):.3f}')
    ax.plot(r, p_interp, label=f'{auc(r, p_interp):.3f}')
    ax.set_xlabel(f'R_{name}')
    if i == 0:
        ax.set_ylabel(f'P_{name}')
    ax.legend()


for i, (ds_id, ds) in tqdm(enumerate(ds_id_to_max_logits_ds.items())):
    y_score_out, y_true_out = zip(* [(x, y) for x, y in ds])
    y_true = np.array(y_true_in + y_true_out)
    y_score = np.array(y_score_in + y_score_out)

    # evaluation
    fpr, tpr, thresholds = roc_curve(y_true=y_true, y_score=y_score)

    tpr95idx = np.argmin(np.abs(tpr - 0.95))
    if tpr[tpr95idx] < 0.95:
        fpr95_indices = [tpr95idx, min(len(tpr) - 1, tpr95idx + 1)]
    else:
        fpr95_indices = [max(0, tpr95idx - 1), tpr95idx]
    if fpr95_indices[0] == fpr95_indices[1]:
        fpr95_weights = np.array([0, 1])
    else:
        fpr95_weights = np.array(
            [tpr[fpr95_indices[1]] - 0.95, 0.95 - tpr[fpr95_indices[0]]])
        fpr95_weights /= np.sum(fpr95_weights)
    fpr95t = fpr[fpr95_indices].dot(fpr95_weights)  # tpr95 ~= 0.95
    tpr95t = 0.95

    det_err95t = ((1 - tpr95t) + fpr95t) / 2

    p_in, r_in, thresholds = precision_recall_curve(
        y_true=y_true, probas_pred=y_score)
    pi_in = np.copy(p_in)
    for j, _ in enumerate(p_in):
        pi_in[j] = np.max(p_in[:j + 1])

    p_out, r_out, thresholds = precision_recall_curve(
        y_true=1 - y_true, probas_pred=-y_score)
    pi_out = np.copy(p_out)
    for j, _ in enumerate(p_out):
        pi_out[j] = np.max(p_out[:j + 1])

    # histograms
    ax = axes[0, i]
    ax.set_title(ds_id)
    hist_range = (min(y_score), max(y_score))
    ax.hist(y_score_in, bins=40, range=hist_range, alpha=0.5)
    ax.hist(y_score_out, bins=40, range=hist_range, alpha=0.5)

    # in-distribution ROC, AUROC, FPR@95%TPR
    plot_roc(axes[1, i], fpr, tpr, fpr95t, det_err95t)

    # in-distribution P-R, AUPR
    plot_pr(axes[2, i], p_in, pi_in, r_in, 'in')

    # out-distribution P-R, AUPR
    plot_pr(axes[3, i], p_out, pi_out, r_out, 'out')

#fig.tight_layout(pad=0.1)
plt.tight_layout(pad=0.5)
plt.xticks(fontsize=8)
plt.xticks(fontsize=8)
plt.show()