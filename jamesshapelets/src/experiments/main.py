"""
main.py
=========================
Main experiment runner. The aim is for everything to be run through this file with different model configurations
imported via config
"""
from jamesshapelets.definitions import *
from sacred import Experiment

import numpy as np
import torch
from torch import nn, optim
from torch.utils.data import DataLoader
from ignite.engine import Engine, Events, create_supervised_trainer, create_supervised_evaluator
from ignite.metrics import Accuracy
from jamesshapelets.src.experiments.setup import create_fso, basic_gridsearch
from jamesshapelets.src.data.dicts import learning_ts_shapelets
from jamesshapelets.src.data.make_dataset import UcrDataset
from jamesshapelets.src.models.model import ShapeletNet
from jamesshapelets.src.models.dataset import PointsDataset, SigletDataset
from jamesshapelets.src.experiments.utils import ignite_accuracy_transform

import logging
logging.getLogger("ignite").setLevel(logging.WARNING)

import warnings
warnings.simplefilter('ignore', UserWarning)

# Experiment setup
ex_name = 'patrick_discrepancy'
ex = Experiment(ex_name)
save_dir = MODELS_DIR + '/experiments/{}'.format(ex_name)

# CUDA
def get_freest_gpu():
    os.system('nvidia-smi -q -d Memory |grep -A4 GPU|grep Free >tmp')
    memory_available = [int(x.split()[2]) for x in open('tmp', 'r').readlines()]
    return np.argmax(memory_available)
use_cuda = torch.cuda.is_available()
device = torch.device('cuda:{}'.format(get_freest_gpu())) if torch.cuda.is_available() else torch.device('cpu')


# Configuration, setup parameters that can vary here
@ex.config
def my_config():
    multivariate = False
    path_data = 'points'
    num_shapelets = 10
    window_size = 40
    aug_list = ['addtime']
    num_window_sizes = 5
    max_window = 100
    min_window = 3
    depth = 5
    discriminator = 'l2'
    max_epochs = 1000
    lr = 1e-2


# Main run file
@ex.main
def main(_run,
         ds_name,
         multivariate,
         path_tfm,
         num_shapelets,
         window_size,
         num_window_sizes,
         min_window,
         max_window,
         aug_list,
         depth,
         discriminator,
         max_epochs,
         lr):
    # Add in save_dir
    _run.save_dir = save_dir + '/' + _run._id

    # Get model training datsets
    ucr_train, ucr_test = UcrDataset(ds_name, multivariate=multivariate).get_original_train_test()
    if path_tfm == 'points':
        train_ds, test_ds = [PointsDataset(x.data, x.labels, window_size=window_size) for x in (ucr_train, ucr_test)]
    elif path_tfm == 'signature':
        train_ds, test_ds = [
            SigletDataset(
                x.data, x.labels, depth=depth, aug_list=aug_list, ds_length=x.size(1), num_window_sizes=num_window_sizes,
                max_window=max_window, min_window=min_window
            )
            for x in (ucr_train, ucr_test)
        ]
    n_classes = ucr_train.n_classes
    n_outputs = n_classes - 1 if n_classes == 2 else n_classes

    # Loaders
    train_dl = DataLoader(train_ds, batch_size=32)
    test_dl = DataLoader(test_ds, batch_size=test_ds.size(0))

    # Setup
    model = ShapeletNet(
        num_shapelets=num_shapelets,
        shapelet_len=train_ds.shapelet_len,
        num_outputs=n_outputs,
        init_data=train_ds.data,
        discriminator=discriminator,
    )
    model.to(device)
    loss_fn = nn.BCEWithLogitsLoss() if ucr_train.n_classes == 2 else nn.CrossEntropyLoss()
    optimizer = optim.Adam(params=model.parameters(), lr=lr)
    # optimizer = optim.Adam(params=[
    #     {'params': model.shapelets_.parameters()},
    #     {'params': model.discriminator.parameters()},
    #     {'params': model.classifier.parameters(), 'weight_decay': 0.1}
    # ], lr=lr)

    # Setup
    trainer = create_supervised_trainer(model=model, optimizer=optimizer, loss_fn=loss_fn, device=device)
    evaluator = create_supervised_evaluator(
        model=model,
        metrics={
            'acc': Accuracy(output_transform=ignite_accuracy_transform, is_multilabel=True if n_classes > 2 else False)
        },
        device=device
    )

    # Validation history
    validation_history = {
        'acc.train': [],
        'acc.test': [],
        'loss.train': [],
        'epoch': []
    }

    @trainer.on(Events.EPOCH_COMPLETED)
    def evaluate(trainer):
        epoch = trainer.state.epoch
        if epoch % 100 == 0:
            evaluator.run(train_dl)
            train_acc = evaluator.state.metrics['acc']
            evaluator.run(test_dl)
            test_acc = evaluator.state.metrics['acc']
            print('EPOCH: [{}]'.format(epoch))
            print('Train loss: {} - Train acc: {:.2f}%'.format(trainer.state.output, 100 * train_acc))
            print('Acc Test: {:.2f}%'.format(100 * test_acc))

            validation_history['acc.train'].append(train_acc)
            validation_history['acc.test'].append(test_acc)
            validation_history['loss.train'].append(trainer.state.output)
            validation_history['epoch'].append(epoch)

    # Time it
    start = time.time()
    trainer.run(train_dl, max_epochs=max_epochs)
    elapsed = time.time() - start

    _run.log_scalar(elapsed, 'training_time')
    _run.log_scalar('acc.train', validation_history['acc.train'][-1])
    _run.log_scalar('acc.test', validation_history['acc.test'][-1])
    _run.log_scalar('loss.train', validation_history['loss.train'][-1])
    _run.log_scalar('acc.test.best', max(validation_history['acc.test']))

    save_pickle(validation_history, save_dir + '/validation_history.pkl')


if __name__ == '__main__':
    config = {
        # 'ds_name': learning_ts_shapelets,
        'ds_name': ['SonyAIBORobotSurface1', 'Coffee', 'MoteStrain', 'Chinatown', 'ECGFiveDays', 'MedicalImages'],
        'multivariate': [False],
        'path_tfm': ['signature'],
        'aug_list': [['addtime']],

        'num_shapelets': [8],
        'num_window_sizes': [10],
        'min_window': [5],
        'max_window': [200],
        'depth': [3],

        'discriminator': [
            # 'patrick{}'.format(str(x)) for x in range(1, 10)
            'patrick6'
    ],

        'max_epochs': [1000],
        'lr': [1e-1],
    }

    # Create FSO (this creates a folder to log information into).
    create_fso(ex, save_dir, remove_folder=True)

    # Run a gridsearch over all parameter combinations.
    basic_gridsearch(ex, config)


