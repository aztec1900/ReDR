"""
    This is the loadable seq2seq trainer library that is
    in charge of training details, loss compute, and statistics.
    See train.py for a use case of this library.

    Note: To make this a general library, we implement *only*
          mechanism things here(i.e. what to do), and leave the strategy
          things to users(i.e. how to do it). Also see train.py(one of the
          users of this library) for the strategy things we do.
"""

from copy import deepcopy
import itertools
import torch
from tqdm import tqdm
import onmt.utils
from onmt.utils.logging import logger
from random import random


def build_trainer(opt, device_id, model, fields, optim, model_saver=None):
    """
    Simplify `Trainer` creation based on user `opt`s*

    Args:
        opt (:obj:`Namespace`): user options (usually from argument parsing)
        model (:obj:`onmt.models.NMTModel`): the model to train
        fields (dict): dict of fields
        optim (:obj:`onmt.utils.Optimizer`): optimizer used during training
        data_type (str): string describing the type of data
            e.g. "text", "img", "audio"
        model_saver(:obj:`onmt.models.ModelSaverBase`): the utility object
            used to save the model
    """

    tgt_field = dict(fields)["tgt"].base_field
    train_loss = onmt.utils.loss.build_loss_compute(model, tgt_field, opt)
    valid_loss = onmt.utils.loss.build_loss_compute(
        model, tgt_field, opt, train=False)

    trunc_size = opt.truncated_decoder  # Badly named...
    shard_size = opt.max_generator_batches if opt.model_dtype == 'fp32' else 0
    norm_method = opt.normalization
    grad_accum_count = opt.accum_count
    n_gpu = opt.world_size
    average_decay = opt.average_decay
    average_every = opt.average_every
    if device_id >= 0:
        gpu_rank = opt.gpu_ranks[device_id]
    else:
        gpu_rank = 0
        n_gpu = 0
    gpu_verbose_level = opt.gpu_verbose_level

    report_manager = onmt.utils.build_report_manager(opt)
    trainer = onmt.Trainer(model, train_loss, valid_loss, optim, trunc_size,
                           shard_size, norm_method,
                           grad_accum_count, n_gpu, gpu_rank,
                           gpu_verbose_level, report_manager, 
                           tgt_field=tgt_field,
                           model_saver=model_saver if gpu_rank == 0 else None,
                           average_decay=average_decay,
                           average_every=average_every,
                           model_dtype=opt.model_dtype,
                           enable_rl_after=opt.enable_rl_after,
                           rl_save_step=opt.rl_save_step)
    return trainer


class Trainer(object):
    """
    Class that controls the training process.

    Args:
            model(:py:class:`onmt.models.model.NMTModel`): translation model
                to train
            train_loss(:obj:`onmt.utils.loss.LossComputeBase`):
               training loss computation
            valid_loss(:obj:`onmt.utils.loss.LossComputeBase`):
               training loss computation
            optim(:obj:`onmt.utils.optimizers.Optimizer`):
               the optimizer responsible for update
            trunc_size(int): length of truncated back propagation through time
            shard_size(int): compute loss in shards of this size for efficiency
            data_type(string): type of the source input: [text|img|audio]
            norm_method(string): normalization methods: [sents|tokens]
            grad_accum_count(int): accumulate gradients this many times.
            report_manager(:obj:`onmt.utils.ReportMgrBase`):
                the object that creates reports, or None
            model_saver(:obj:`onmt.models.ModelSaverBase`): the saver is
                used to save a checkpoint.
                Thus nothing will be saved if this parameter is None
    """

    def __init__(self, model, train_loss, valid_loss, optim,
                 trunc_size=0, shard_size=32,
                 norm_method="sents", grad_accum_count=1, n_gpu=1, gpu_rank=1,
                 gpu_verbose_level=0, report_manager=None, model_saver=None,
                 average_decay=0, average_every=1, model_dtype='fp32', enable_rl_after=-1, rl_save_step=1000, tgt_field=None):
        # Basic attributes.
        self.model = model
        self.train_loss = train_loss
        self.valid_loss = valid_loss
        self.optim = optim
        self.trunc_size = trunc_size
        self.shard_size = shard_size
        self.norm_method = norm_method
        self.grad_accum_count = grad_accum_count
        self.n_gpu = n_gpu
        self.gpu_rank = gpu_rank
        self.gpu_verbose_level = gpu_verbose_level
        self.report_manager = report_manager
        self.model_saver = model_saver
        self.average_decay = average_decay
        self.moving_average = None
        self.average_every = average_every
        self.model_dtype = model_dtype
        self.enable_rl_after = enable_rl_after
        self.rl_save_step = rl_save_step
        self.tgt_field = tgt_field
        self.trigger = random()

        assert grad_accum_count > 0
        if grad_accum_count > 1:
            assert self.trunc_size == 0, \
                """To enable accumulated gradients,
                   you must disable target sequence truncating."""

        # Set model in training mode.
        self.model.train()

    def _accum_batches(self, iterator):
        batches = []
        normalization = 0
        for batch in iterator:
            batches.append(batch)
            if self.norm_method == "tokens":
                num_tokens = batch.tgt[1:, :, 0].ne(
                    self.train_loss.padding_idx).sum()
                normalization += num_tokens.item()
            else:
                normalization += batch.batch_size
            if len(batches) == self.grad_accum_count:
                yield batches, normalization
                batches = []
                normalization = 0
        if batches:
            yield batches, normalization

    def _update_average(self, step):
        if self.moving_average is None:
            copy_params = [params.detach().float()
                           for params in self.model.parameters()]
            self.moving_average = copy_params
        else:
            average_decay = max(self.average_decay,
                                1 - (step + 1) / (step + 10))
            for (i, avg), cpt in zip(enumerate(self.moving_average),
                                     self.model.parameters()):
                self.moving_average[i] = \
                    (1 - average_decay) * avg + \
                    cpt.detach().float() * average_decay

    def train(self,
              train_iter,
              train_steps,
              save_checkpoint_steps=5000,
              valid_iter=None,
              valid_steps=10000):
        """
        The main training loop by iterating over `train_iter` and possibly
        running validation on `valid_iter`.

        Args:
            train_iter: A generator that returns the next training batch.
            train_steps: Run training for this many iterations.
            save_checkpoint_steps: Save a checkpoint every this many
              iterations.
            valid_iter: A generator that returns the next validation batch.
            valid_steps: Run evaluation every this many iterations.

        Returns:
            The gathered statistics.
        """
        if valid_iter is None:
            logger.info('Start training loop without validation...')
        else:
            logger.info('Start training loop and validate every %d steps...',
                        valid_steps)

        total_stats = onmt.utils.Statistics()
        report_stats = onmt.utils.Statistics()
        self._start_report_manager(start_time=total_stats.start_time)

        if self.n_gpu > 1:
            train_iter = itertools.islice(
                train_iter, self.gpu_rank, None, self.n_gpu)

        local_step = self.optim.training_step
        for i, (batches, normalization) in tqdm(enumerate(self._accum_batches(train_iter))):
            local_step += 1

            if self.gpu_verbose_level > 1:
                logger.info("GpuRank %d: index: %d", self.gpu_rank, i)
            if self.gpu_verbose_level > 0:
                logger.info("GpuRank %d: reduce_counter: %d \
                            n_minibatch %d"
                            % (self.gpu_rank, i + 1, len(batches)))

            if self.n_gpu > 1:
                normalization = sum(onmt.utils.distributed
                                    .all_gather_list
                                    (normalization))

            self._gradient_accumulation(
                batches, normalization, total_stats,
                report_stats, local_step)

            if self.average_decay > 0 and i % self.average_every == 0:
                self._update_average(local_step)

            report_stats = self._maybe_report_training(
                local_step, train_steps,
                self.optim.learning_rate(),
                report_stats)

            if valid_iter is not None and local_step % valid_steps == 0:
                if self.gpu_verbose_level > 0:
                    logger.info('GpuRank %d: validate step %d'
                                % (self.gpu_rank, local_step))
                valid_stats = self.validate(
                    valid_iter, moving_average=self.moving_average)
                if self.gpu_verbose_level > 0:
                    logger.info('GpuRank %d: gather valid stat \
                                step %d' % (self.gpu_rank, local_step))
                valid_stats = self._maybe_gather_stats(valid_stats)
                if self.gpu_verbose_level > 0:
                    logger.info('GpuRank %d: report stat step %d'
                                % (self.gpu_rank, local_step))
                self._report_step(self.optim.learning_rate(),
                                  local_step, valid_stats=valid_stats)

            if self.enable_rl_after < 0 or local_step <= self.enable_rl_after:
                if (self.model_saver is not None
                    and (save_checkpoint_steps != 0
                         and local_step % save_checkpoint_steps == 0)):
                    self.model_saver.save(local_step, moving_average=self.moving_average)
            else:
                if (self.model_saver is not None
                    and (self.rl_save_step != 0
                         and local_step % self.rl_save_step == 0)):
                    self.model_saver.save(local_step, moving_average=self.moving_average)

            if train_steps > 0 and local_step >= train_steps:
                break


        if self.model_saver is not None:
            self.model_saver.save(local_step, moving_average=self.moving_average)
        return total_stats

    def validate(self, valid_iter, moving_average=None):
        """ Validate model.
            valid_iter: validate data iterator
        Returns:
            :obj:`nmt.Statistics`: validation loss statistics
        """
        if moving_average:
            valid_model = deepcopy(self.model)
            for avg, param in zip(self.moving_average,
                                  valid_model.parameters()):
                param.data = avg.data.half() if self.model_dtype == "fp16" \
                    else avg.data
        else:
            valid_model = self.model

        # Set model in validating mode.
        valid_model.eval()

        with torch.no_grad():
            stats = onmt.utils.Statistics()

            for batch in valid_iter:
                src, src_lengths = batch.src if isinstance(batch.src, tuple) \
                                   else (batch.src, None)
                history, history_lengths = batch.history if isinstance(batch.history, tuple) else (batch.src, None)
                tgt = batch.tgt

                # F-prop through the model.
                outputs, attns, _ = valid_model(src, history, tgt, src_lengths, history_lengths)

                # Compute loss.
                _, batch_stats = self.valid_loss(batch, outputs, attns)

                # Update statistics.
                stats.update(batch_stats)

        if moving_average:
            del valid_model
        else:
            # Set model back to training mode.
            valid_model.train()

        return stats

    def _look_target_tokens(self, tgt):
        vocab = self.tgt_field.vocab
        tokens = []

        for tok in tgt:
            if tok < len(vocab):
                tokens.append(vocab.itos[tok])
            else:
                tokens.append(vocab.itos[1])
            if tokens[-1] == self.tgt_field.eos_token:
                tokens = tokens[:-1]
                break
        return tokens


    def _gradient_accumulation(self, true_batches, normalization, total_stats,
                               report_stats, local_step):

        for batch in true_batches:
            target_size = batch.tgt.size(0)
            # Truncated BPTT(disabled): reminder not compatible with accum > 1
            trunc_size = target_size

            src, src_lengths = batch.src if isinstance(batch.src, tuple) \
                else (batch.src, None)
            if src_lengths is not None:
                report_stats.n_src_words += src_lengths.sum().item()

            history, history_lengths = batch.history if isinstance(batch.history, tuple) \
                else (batch.history, None)

            tgt_outer = batch.tgt

            bptt = False
            # --------------------------------
            # 1. Create truncated target.
            tgt = tgt_outer

            # 2. F-prop all but generator.
            if self.grad_accum_count == 1:
                self.optim.zero_grad()
            # outputs: seq, batch_size, linear_hidden
            outputs, attns, results = self.model(batch, src, history, tgt, src_lengths, history_lengths, bptt=bptt)
            preds_n, preds, src_raws, target_ans = self.model.reverse(results)
            scores = results['scores']
            if self.enable_rl_after >= 0 and local_step > self.enable_rl_after and self.trigger < 0.2:
                torch.autograd.set_detect_anomaly(True)
                beam_size = results['dec_outputs'].size(2)
                for b in range(beam_size):
                    this_outputs = results['dec_outputs'][:, :, b, :]
                    best_value, best_index = this_outputs.max(-1)
                    this_attns = {}
                    for key in results['dec_attns'].keys():
                        this_attns[key] = results['dec_attns'][key][:, :, b, :]
                    assert batch.tgt.size(0) - 1 == this_outputs.size(0), " {} {}".format(batch.tgt.size(), this_outputs.size())
                    scales = []
                    old_tgt = batch.tgt
                    max_len_tgt = old_tgt.size(0)
                    new_tgt = None
                    for batch_id in range(len(preds)):
                        pred = preds[batch_id][b]
                        if len(preds_n[batch_id]) == 0:
                            pred_n = torch.tensor([], dtype=old_tgt.dtype)
                        else:
                            pred_n = preds_n[batch_id][b]
                        assert len(pred_n) < max_len_tgt
                        padding_num = max_len_tgt - 1 - len(pred_n)
                        padding_vec = torch.ones(padding_num, dtype=old_tgt.dtype)
                        if self.n_gpu >= 1:
                            padding_vec = padding_vec.cuda()
                            pred_n = pred_n.cuda()
                        pred_tgt = torch.cat((pred_n, padding_vec), 0).unsqueeze(0) # 1 * (max_len - 1)
                        if new_tgt is not None:
                            new_tgt = torch.cat((new_tgt, pred_tgt), 0)
                        else:
                            new_tgt = pred_tgt
                        src_raw = src_raws[batch_id]
                        ans = target_ans[batch_id]
                        if len(pred) == 0:
                            scales.append(1.0)
                            continue
                        drqa_results = self.model.drqa_predict(
                            doc=' '.join(src_raw), que=' '.join(pred), target=' '.join(ans))
                        f1_score = drqa_results['f1']
                        scales.append(1.0 - f1_score)
                    scales = torch.tensor(scales, device=this_outputs.device)
                    new_tgt = new_tgt.permute(1, 0)
                    # # init token id
                    new_tgt_bos = torch.ones(size=(1, old_tgt.size(1)), dtype=old_tgt.dtype) * 2
                    if self.n_gpu >= 1:
                        new_tgt_bos = new_tgt_bos.cuda()
                    new_tgt = torch.cat([new_tgt_bos, new_tgt], dim=0).unsqueeze(-1)
                    batch.tgt = new_tgt
                    loss, batch_stats = self.train_loss(
                         batch,
                         this_outputs,
                         this_attns,
                         normalization=normalization,
                         shard_size=self.shard_size,
                         trunc_start=0,
                         trunc_size=trunc_size,
                         retain_graph=True,
                         scales=scales)
                    batch.tgt = old_tgt
                    # If truncated, don't backprop fully.
                    # TO CHECK
                    # if dec_state is not None:
                    #    dec_state.detach()
                    if self.model.decoder.state is not None:
                        self.model.decoder.detach_state()
                    if loss is not None:
                        self.optim.backward(loss)
                # --------------------------------
                # update only after all beam have done
                if self.grad_accum_count == 1:
                    if self.n_gpu > 1:
                        grads = [p.grad.data for p in self.model.parameters()
                                 if p.requires_grad
                                 and p.grad is not None]
                        onmt.utils.distributed.all_reduce_and_rescale_tensors(
                            grads, float(1))
                    self.optim.step(rl=True)

            if self.grad_accum_count == 1:
                self.optim.zero_grad()

            # 3. Compute loss.
            loss, batch_stats = self.train_loss(
                batch,
                outputs,
                attns,
                normalization=normalization,
                shard_size=self.shard_size,
                trunc_start=0,
                trunc_size=trunc_size)

            if loss is not None:
                self.optim.backward(loss)


            # 4. Update the parameters and statistics.
            if self.grad_accum_count == 1:
                # Multi GPU gradient gather
                if self.n_gpu > 1:
                    grads = [p.grad.data for p in self.model.parameters()
                             if p.requires_grad
                             and p.grad is not None]
                    onmt.utils.distributed.all_reduce_and_rescale_tensors(
                        grads, float(1))
                self.optim.step()

            total_stats.update(batch_stats)
            report_stats.update(batch_stats)

        # in case of multi step gradient accumulation,
        # update only after accum batches
        if self.grad_accum_count > 1:
            if self.n_gpu > 1:
                grads = [p.grad.data for p in self.model.parameters()
                         if p.requires_grad
                         and p.grad is not None]
                onmt.utils.distributed.all_reduce_and_rescale_tensors(
                    grads, float(1))
            self.optim.step()

    def _start_report_manager(self, start_time=None):
        """
        Simple function to start report manager (if any)
        """
        if self.report_manager is not None:
            if start_time is None:
                self.report_manager.start()
            else:
                self.report_manager.start_time = start_time

    def _maybe_gather_stats(self, stat):
        """
        Gather statistics in multi-processes cases

        Args:
            stat(:obj:onmt.utils.Statistics): a Statistics object to gather
                or None (it returns None in this case)

        Returns:
            stat: the updated (or unchanged) stat object
        """
        if stat is not None and self.n_gpu > 1:
            return onmt.utils.Statistics.all_gather_stats(stat)
        return stat

    def _maybe_report_training(self, step, num_steps, learning_rate,
                               report_stats):
        """
        Simple function to report training stats (if report_manager is set)
        see `onmt.utils.ReportManagerBase.report_training` for doc
        """
        if self.report_manager is not None:
            return self.report_manager.report_training(
                step, num_steps, learning_rate, report_stats,
                multigpu=self.n_gpu > 1)

    def _report_step(self, learning_rate, step, train_stats=None,
                     valid_stats=None):
        """
        Simple function to report stats (if report_manager is set)
        see `onmt.utils.ReportManagerBase.report_step` for doc
        """
        if self.report_manager is not None:
            return self.report_manager.report_step(
                learning_rate, step, train_stats=train_stats,
                valid_stats=valid_stats)
