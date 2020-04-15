import collections as co
import copy
import json
import math
import numpy as np
import os
import pathlib
import torch
import tqdm
import torchshapelets


here = pathlib.Path(__file__).resolve().parent


def normalise_data(X, train_X):
    # X is assumed to be of shape (..., length, channel)
    out = []
    for Xi, train_Xi in zip(X.unbind(dim=-1), train_X.unbind(dim=-1)):
        mean = train_Xi.mean()
        std = train_Xi.std()
        out.append((Xi - mean) / (1e-5 + std))
    return torch.stack(out, dim=-1)


def dataloader(dataset, **kwargs):
    if 'shuffle' not in kwargs:
        kwargs['shuffle'] = True
    if 'drop_last' not in kwargs:
        kwargs['drop_last'] = True
    if 'batch_size' not in kwargs:
        kwargs['batch_size'] = 32
    kwargs['batch_size'] = min(kwargs['batch_size'], len(dataset))
    return torch.utils.data.DataLoader(dataset, **kwargs)


class _AttrDict(dict):
    def __setattr__(self, key, value):
        self[key] = value

    def __getattr__(self, item):
        return self[item]


def _get_sample_batch(dataloader, num_shapelets_per_class, num_shapelets):
    batch_elems = []
    y_seen = co.defaultdict(int)
    while True:  # in case we need to iterate through the same dataloader multiple times to find the same samples again
        for X, y in dataloader:
            for Xi, yi in zip(X, y):
                yi = int(yi)
                if y_seen[yi] < num_shapelets_per_class:
                    batch_elems.append(Xi)
                    y_seen[yi] += 1
                if len(batch_elems) == num_shapelets:
                    out = torch.stack(batch_elems, dim=0)
                    out = out + 0.001 * torch.randn_like(out)
                    return out
        # len(y_seen) should now be the number of classes
        if len(y_seen) * num_shapelets_per_class != num_shapelets:
            raise RuntimeError("Could not get a sample batch: Have been told that there should {} shapelets per class, "
                               "and {} shaplets in total, but only found {} classes.".format(num_shapelets_per_class,
                                                                                             num_shapelets,
                                                                                             len(y_seen)))


def _count_parameters(model):
    """Counts the number of parameters in a model."""
    return sum(param.numel() for param in model.parameters() if param.requires_grad_)


def _compute_binary_accuracy(pred_y, true_y):
    """Computes the accuracy of a classifier.

    Arguments:
        pred_y: A one dimensional tensor of floats (a sigmoid will be applied to determine the classification result).
        true_y: A one dimensional tensor of floats; 1.0 corresponds to one class, 0.0 corresponds to the other.

    Returns:
        A single float describing the accuracy.
    """
    assert pred_y.shape == true_y.shape, "Shape mismatch: pred_y has shape {}, true_y has shape {}".format(pred_y.shape,
                                                                                                           true_y.shape)

    binary_prediction = (torch.sigmoid(pred_y) > 0.5).to(true_y.dtype)
    prediction_matches = (binary_prediction == true_y).to(true_y.dtype)
    proportion_correct = prediction_matches.sum() / true_y.size(0)
    return proportion_correct


def _compute_multiclass_accuracy(pred_y, true_y):
    """Computes the accuracy of a classifier.

    Arguments:
        pred_y: A two dimensional tensor of floats, of shape (batch, targets).
        true_y: A one dimensional tensor of targets; integers corresponding to each class.

    Returns:
        A single float describing the accuracy.
    """
    prediction = torch.argmax(pred_y, dim=1)
    prediction_matches = (prediction == true_y).to(pred_y.dtype)
    proportion_correct = prediction_matches.sum() / true_y.size(0)
    return proportion_correct


def _get_discrepancy_fn(discrepancy_fn, input_channels, ablation_pseudometric):
    if discrepancy_fn == 'L2':
        discrepancy_fn = torchshapelets.L2Discrepancy(in_channels=input_channels, pseudometric=ablation_pseudometric)
    elif discrepancy_fn == 'L2_squared':
        discrepancy_fn = torchshapelets.L2DiscrepancySquared(in_channels=input_channels,
                                                             pseudometric=ablation_pseudometric)
    elif discrepancy_fn == 'piecewise_constant_L2_squared':
        def discrepancy_fn(times, path, shapelet):
            return ((path - shapelet) ** 2).sum(dim=(-1, -2))
        discrepancy_fn.parameters = lambda: []
    elif discrepancy_fn == 'DTW':
        # Takes forever, not recommended
        def discrepancy_fn(times, path, shapelet):
            memo = [[torch.tensor(float('inf'), dtype=path.dtype)
                     for _ in range(path.size(-2) + 1)]
                    for _ in range(shapelet.size(-2) + 1)]
            memo[0][0] = torch.tensor(0, dtype=path.dtype)
            for i in range(path.size(-2)):
                for j in range(shapelet.size(-2)):
                    cost = (path[..., i, :] - shapelet[j, :]).norm(dim=-1)
                    memo[i + 1][j + 1] = cost + torch.min(torch.min(memo[i][j + 1], memo[i + 1][j]), memo[i][j])
            return memo[-1][-1]
        discrepancy_fn.parameters = lambda: []
    elif 'logsig' in discrepancy_fn:
        # expects e.g. 'logsig-4'
        split_desc = discrepancy_fn.split('-')
        assert len(split_desc) == 2
        assert split_desc[0] == 'logsig'
        depth = int(split_desc[1])
        discrepancy_fn = torchshapelets.LogsignatureDiscrepancy(in_channels=input_channels, depth=depth,
                                                                pseudometric=ablation_pseudometric)
    return discrepancy_fn


class _LinearShapeletTransform(torch.nn.Module):
    def __init__(self, in_channels, out_channels, num_shapelets, num_shapelet_samples, discrepancy_fn,
                 max_shapelet_length, lengths_per_shapelet, num_continuous_samples, log):
        super(_LinearShapeletTransform, self).__init__()

        self.shapelet_transform = torchshapelets.GeneralisedShapeletTransform(in_channels=in_channels,
                                                                              num_shapelets=num_shapelets,
                                                                              num_shapelet_samples=num_shapelet_samples,
                                                                              discrepancy_fn=discrepancy_fn,
                                                                              max_shapelet_length=max_shapelet_length,
                                                                              lengths_per_shapelet=lengths_per_shapelet,
                                                                              num_continuous_samples=num_continuous_samples)
        self.linear = torch.nn.Linear(num_shapelets * lengths_per_shapelet, out_channels)
        self.linear.weight.register_hook(lambda grad: 100 * grad)
        self.linear.bias.register_hook(lambda grad: 100 * grad)

        self.log = log

    def forward(self, times, X):
        shapelet_similarity = self.shapelet_transform(times, X)
        if self.log:
            log_shapelet_similarity = (shapelet_similarity + 1e-5).log()
        else:
            log_shapelet_similarity = shapelet_similarity
        out = self.linear(log_shapelet_similarity)
        if out.size(-1) == 1:
            out = out.squeeze(-1)
        return out, shapelet_similarity, self.shapelet_transform.lengths, self.shapelet_transform.discrepancy_fn

    def clip_length(self):
        self.shapelet_transform.clip_length()

    def set_shapelets(self, times, path):
        self.shapelet_transform.reset_parameters(times, path)


def _evaluate_metrics(dataloader, model, times, loss_fn, num_classes):
    with torch.no_grad():
        accuracy_fn = _compute_binary_accuracy if num_classes == 2 else _compute_multiclass_accuracy
        total_loss = 0
        total_accuracy = 0
        total_dataset_size = 0
        for batch in dataloader:
            X, y = batch
            batch_size = y.size(0)
            pred_y, _, _, _ = model(times, X)
            if num_classes == 2:
                y = y.to(pred_y.dtype)
            total_accuracy += accuracy_fn(pred_y, y) * batch_size
            total_loss += loss_fn(pred_y, y) * batch_size
            total_dataset_size += batch_size
        total_loss /= total_dataset_size  # assume 'mean' reduction in the loss function
        total_accuracy /= total_dataset_size
        return _AttrDict(loss=total_loss, accuracy=total_accuracy)


def _train_loop(train_dataloader, val_dataloader, model, times, optimizer, loss_fn, epochs, num_classes,
                ablation_similarreg):
    """Standard training loop.

    Has a few simple bells and whistles:
    - Decreases learning rate on plateau.
    - Stops training if there's no improvement in training loss for several epochs.
    - Uses the best model (measured by validation accuracy) encountered during training, not just the final one.
    """
    model.train()
    best_model = model
    best_train_loss = math.inf
    best_val_accuracy = 0
    best_epoch = 0
    history = []
    breaking = False

    epoch_per_metric = 10
    plateau_patience = 1  # this will be multiplied by epoch_per_metric for the actual patience
    plateau_terminate = 50
    similarity_coefficient = 0.0001

    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=plateau_patience, mode='max')

    tqdm_range = tqdm.tqdm(range(epochs))
    for epoch in tqdm_range:
        if breaking:
            break
        for batch in train_dataloader:
            if breaking:
                break
            X, y = batch
            pred_y, shapelet_similarity, shapelet_lengths, discrepancy_fn = model(times, X)
            if num_classes == 2:
                y = y.to(pred_y.dtype)
            loss = loss_fn(pred_y, y)
            if ablation_similarreg:
                loss = loss + similarity_coefficient * torchshapelets.similarity_regularisation(shapelet_similarity)
            loss.backward()
            optimizer.step()
            model.clip_length()
            optimizer.zero_grad()

        if epoch % epoch_per_metric == 0 or epoch == epochs - 1:
            model.eval()
            train_metrics = _evaluate_metrics(train_dataloader, model, times, loss_fn, num_classes)
            val_metrics = _evaluate_metrics(val_dataloader, model, times, loss_fn, num_classes)
            model.train()

            if train_metrics.loss * 1.0001 < best_train_loss:
                best_train_loss = train_metrics.loss
                best_epoch = epoch

            if val_metrics.accuracy > best_val_accuracy:
                best_val_accuracy = val_metrics.accuracy
                del best_model  # so that we don't have three copies of a model simultaneously
                best_model = copy.deepcopy(model)

            tqdm_range.write('Epoch: {}  Train loss: {:.3}  Train accuracy: {:.3}  Val loss: {:.3}  '
                             'Val accuracy: {:.3}'
                             ''.format(epoch, train_metrics.loss, train_metrics.accuracy, val_metrics.loss,
                                       val_metrics.accuracy))
            scheduler.step(val_metrics.accuracy)
            history.append(_AttrDict(epoch=epoch, train_loss=train_metrics.loss,
                                     train_accuracy=train_metrics.accuracy,
                                     val_loss=val_metrics.loss, val_accuracy=val_metrics.accuracy))

            if epoch > best_epoch + plateau_terminate:
                tqdm_range.write('Breaking because of no improvement in training loss for {} epochs.'
                                 ''.format(plateau_terminate))
                breaking = True

    for parameter, best_parameter in zip(model.parameters(), best_model.parameters()):
        parameter.data = best_parameter.data
    return history


def _evaluate_model(train_dataloader, val_dataloader, test_dataloader, model, times, loss_fn, history, num_classes):
    model.eval()
    train_metrics = _evaluate_metrics(train_dataloader, model, times, loss_fn, num_classes)
    val_metrics = _evaluate_metrics(val_dataloader, model, times, loss_fn, num_classes)
    test_metrics = _evaluate_metrics(test_dataloader, model, times, loss_fn, num_classes)

    return _AttrDict(times=times,
                     num_classes=num_classes,
                     train_dataloader=train_dataloader,
                     val_dataloader=val_dataloader,
                     test_dataloader=test_dataloader,
                     model=model,
                     parameters=_count_parameters(model),
                     history=history,
                     train_metrics=train_metrics,
                     val_metrics=val_metrics,
                     test_metrics=test_metrics)


class _TensorEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, (torch.Tensor, np.ndarray)):
            return o.tolist()
        else:
            super(_TensorEncoder, self).default(o)


def save_results(result_folder, result_subfolder, results):
    base_loc = here / 'results' / result_folder
    if not os.path.exists(base_loc):
        os.mkdir(base_loc)
    loc = base_loc / result_subfolder
    if not os.path.exists(loc):
        os.mkdir(loc)
    num = -1
    for filename in os.listdir(loc):
        try:
            num = max(num, int(filename))
        except ValueError:
            pass
    result_to_save = results.copy()
    del result_to_save['train_dataloader']
    del result_to_save['val_dataloader']
    del result_to_save['test_dataloader']
    result_to_save['model'] = str(result_to_save['model'])

    num += 1
    with open(loc / str(num), 'w') as f:
        json.dump(result_to_save, f, cls=_TensorEncoder)


def main(times,
         train_dataloader,
         val_dataloader,
         test_dataloader,
         num_classes,
         input_channels,
         result_folder,
         result_subfolder,
         epochs,
         num_shapelets_per_class,
         num_shapelet_samples,
         discrepancy_fn,
         max_shapelet_length_proportion,
         lengths_per_shapelet,
         num_continuous_samples,
         ablation_pseudometric,
         ablation_learntlengths,
         ablation_similarreg,
         old_shapelets):

    if old_shapelets:
        discrepancy_fn = 'piecewise_constant_L2_squared'
        max_shapelet_length_proportion = 0.3
        ablation_pseudometric = False
        ablation_learntlengths = False
        ablation_similarreg = False
        num_continuous_samples = None
        log = False
    else:
        log = True

    # Select some sensible options based on the length of the dataset
    timespan = times[-1] - times[0]
    if max_shapelet_length_proportion is None:
        max_shapelet_length_proportion = min((10 / timespan).sqrt(), 1)
    max_shapelet_length = timespan * max_shapelet_length_proportion
    if num_shapelet_samples is None:
        num_shapelet_samples = int(max_shapelet_length_proportion * times.size(0))
    if num_continuous_samples is None:
        num_continuous_samples = times.size(0)

    discrepancy_fn = _get_discrepancy_fn(discrepancy_fn, input_channels, ablation_pseudometric)

    num_shapelets = num_shapelets_per_class * num_classes

    if num_classes == 2:
        out_channels = 1
    else:
        out_channels = num_classes

    model = _LinearShapeletTransform(in_channels=input_channels,
                                     out_channels=out_channels,
                                     num_shapelets=num_shapelets,
                                     num_shapelet_samples=num_shapelet_samples,
                                     discrepancy_fn=discrepancy_fn,
                                     max_shapelet_length=max_shapelet_length,
                                     lengths_per_shapelet=lengths_per_shapelet,
                                     num_continuous_samples=num_continuous_samples,
                                     log=log)

    if old_shapelets:
        new_lengths = torch.full_like(model.shapelet_transform.lengths, max_shapelet_length)
        del model.shapelet_transform.lengths
        model.shapelet_transform.register_buffer('lengths', new_lengths)

    sample_batch = _get_sample_batch(train_dataloader, num_shapelets_per_class, num_shapelets)
    model.set_shapelets(times.to('cpu'), sample_batch.to('cpu'))  # smart initialisation of shapelets

    if not ablation_learntlengths:
        model.shapelet_transform.lengths.requires_grad_(False)

    if num_classes == 2:
        loss_fn = torch.nn.functional.binary_cross_entropy_with_logits
    else:
        loss_fn = torch.nn.functional.cross_entropy

    optimizer = torch.optim.Adam(model.parameters(), lr=0.005)

    history = _train_loop(train_dataloader, val_dataloader, model, times, optimizer, loss_fn, epochs, num_classes,
                          ablation_similarreg)
    results = _evaluate_model(train_dataloader, val_dataloader, test_dataloader, model, times, loss_fn, history,
                              num_classes)
    results.num_shapelets_per_class = num_shapelets_per_class
    results.num_shapelet_samples = num_shapelet_samples
    results.max_shapelet_length_proportion = max_shapelet_length_proportion
    results.ablation_pseudometric = ablation_pseudometric
    results.ablation_learntlengths = ablation_learntlengths
    results.ablation_similarreg = ablation_similarreg
    results.old_shapelets = old_shapelets
    if result_folder is not None:
        save_results(result_folder, result_subfolder, results)
    return results
