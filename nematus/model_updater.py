import math

import numpy
import tensorflow as tf


class ModelUpdater(object):
    """Helper class for training using multiple GPUs and/or large minibatches.

    Given a set of model replicas and an optimizer, ModelUpdater takes care of
    splitting minibatches into sub-batches, feeding them to the individual
    replicas, and then combining and applying the resulting gradients (and
    losses).

    If a minibatch is too large to be processed in a single session, even
    using all GPUs (i.e. it is larger than the number of devices multiplied by
    the per-device limit) then gradients and losses can be accumulated over
    multiple runs and averaged.
    """

    def __init__(self, config, num_gpus, replicas, optimizer, global_step,
                 summary_writer=None):
        """Builds TF graph nodes for model updating (via _ModelUpdateGraph).

        Args:
            config: the model config (an argparse.Namespace)
            num_gpus: the number of available GPUs.
            replicas: a list of RNNModel or Transformer objects.
            optimizer: a TensorFlow optimizer.
            global_step: a tf.Variable to be updated by optimizer.
            summary_writer: a tf.summary.FileWriter object.
        """
        assert len(replicas) > 0

        assert (len(replicas) == num_gpus
                or (len(replicas) == 1 and num_gpus == 0))

        self._config = config
        self._replicas = replicas
        self._summary_writer = summary_writer

        self._graph = _ModelUpdateGraph(config, num_gpus, replicas, optimizer,
                                        global_step)

    def update(self, session, x, x_mask, y, y_mask, write_summary):
        """Updates the model for a single minibatch.

        Args:
            x: Numpy array with shape (factors, seq_len, batch_size)
            x_mask: Numpy array with shape (seq_len, batch_size)
            y: Numpy array with shape (seq_len, batch_size)
            y_mask: Numpy array with shape (seq_len, batch_size)
            write_summary: Boolean

        Returns:
            The sum of the individual sentence losses. The loss for a sentence
            is the sentence-level or token-level cross entropy, optionally with
            L2 or MAP-L2 regularization.
        """

        # Split the minibatch into sub-batches. The number of sub-batches is
        # determined based on either the per-device size limit, if set, or on
        # a fixed number of aggregation steps (which defaults to 1).
        #
        # If necessary, dummy sub-batches are added to get a multiple of the
        # number of replicas, since each replica has to receive some input (the
        # dummy sub-batches will have a weight of zero).

        if (self._config.max_sentences_per_device != 0
            or self._config.max_tokens_per_device != 0):
            start_points = self._split_minibatch_for_device_size(
                x_mask, y_mask, self._config.max_sentences_per_device,
                self._config.max_tokens_per_device)
        else:
            n = len(self._replicas) * self._config.gradient_aggregation_steps
            start_points = self._split_minibatch_into_n(x_mask, y_mask, n)

        split_x, split_x_mask, split_y, split_y_mask, weights = \
            self._split_and_pad_minibatch(x, x_mask, y, y_mask, start_points)

        # TODO REMOVE ME
        # _print_debug_stuff(split_x_mask, split_y_mask, weights)

        # Normalize the weights so that _ModelUpdateGraph can just sum the
        # weighted gradients from each sub-batch (without needing a
        # subsequent division step).
        normalized_weights = [w / sum(weights) for w in weights]

        # Accumulate gradients.
        for i in range(0, len(split_x), len(self._replicas)):
            feed_dict = {}
            for j in range(len(self._replicas)):
                feed_dict[self._graph.replica_weights[j]] \
                    = normalized_weights[i+j]
                feed_dict[self._replicas[j].inputs.x] = split_x[i+j]
                feed_dict[self._replicas[j].inputs.x_mask] = split_x_mask[i+j]
                feed_dict[self._replicas[j].inputs.y] = split_y[i+j]
                feed_dict[self._replicas[j].inputs.y_mask] = split_y_mask[i+j]
                feed_dict[self._replicas[j].inputs.training] = True
            session.run([self._graph.accum_ops], feed_dict=feed_dict)

        # Apply the gradients (and optionally write the summary).
        fetches = self._graph.apply_ops
        if not write_summary:
            global_step, apply_grads, mean_loss_per_sent = session.run(fetches)
        else:
            assert self._summary_writer is not None
            fetches += self._graph.summary_ops
            global_step, apply_grads, mean_loss_per_sent, merged_summary = \
                session.run(fetches)
            self._summary_writer.add_summary(merged_summary, global_step)

        # Reset accumulated values to zero ready for the next call.
        session.run(self._graph.reset_ops)

        # Return the sum of the individual sentence losses.
        return mean_loss_per_sent * x.shape[-1]

    def _split_minibatch_into_n(self, x_mask, y_mask, n):
        """Determines how to split a minibatch into n equal-sized sub-batches.

        The sub-batch size is (approximately) the minibatch size divided by n,
        where size is defined as the number of source + target tokens.

        Args:
            x_mask: Numpy array with shape (seq_len, batch_size)
            y_mask: Numpy array with shape (seq_len, batch_size)
            n: int

        Returns:
            A list of indices representing the starting points of each
            sub-batch.
        """

        source_lengths = numpy.sum(x_mask, axis=0)
        target_lengths = numpy.sum(y_mask, axis=0)
        assert len(source_lengths) == len(target_lengths)
        num_sents = len(source_lengths)

        # Calculate the source + target batch sizes, then divide by n to get
        # the max size of each sub-batch.
        s_total = max(source_lengths) * num_sents
        t_total = max(target_lengths) * num_sents
        soft_limit = math.ceil((s_total + t_total) / n)

        start_points = [0]
        while True:
            i = start_points[-1]
            s_longest = source_lengths[i]
            t_longest = target_lengths[i]
            next_start_point = None
            for j in range(i+1, num_sents):
                s_longest = max(s_longest, source_lengths[j])
                t_longest = max(t_longest, target_lengths[j])
                s_tokens = s_longest * (j-i+1)
                t_tokens = t_longest * (j-i+1)
                if s_tokens + t_tokens > soft_limit:
                    # Allow the sub-batch to be over-filled, but only by one
                    # sentence worth of tokens.
                    next_start_point = j + 1
                    break
            if next_start_point is None or next_start_point >= num_sents:
                break
            start_points.append(next_start_point)

        assert len(start_points) <= n
        return start_points

    def _split_minibatch_for_device_size(self, x_mask, y_mask,
                                         max_sents_per_device=0,
                                         max_tokens_per_device=0):
        """Determines how to split a minibatch into device-sized sub-batches.

        Either max_sents_per_device or max_tokens_per_device must be given.

        Args:
            x_mask: Numpy array with shape (seq_len, batch_size)
            y_mask: Numpy array with shape (seq_len, batch_size)
            max_sents_per_device: int
            max_tokens_per_device: int

        Returns:
            A list of indices representing the starting points of each
            sub-batch.
        """

        assert max_sents_per_device == 0 or max_tokens_per_device == 0
        assert not (max_sents_per_device == 0 and max_tokens_per_device == 0)

        # Determine where to split the minibatch to produce sub-batches that
        # fit the device capacity.
        if max_sents_per_device != 0:
            start_points = range(0, num_sents, max_sents_per_device)
        else:
            source_lengths = numpy.sum(x_mask, axis=0)
            target_lengths = numpy.sum(y_mask, axis=0)
            assert len(source_lengths) == len(target_lengths)
            num_sents = len(source_lengths)

            start_points = [0]
            while True:
                i = start_points[-1]
                s_longest = source_lengths[i]
                t_longest = target_lengths[i]
                next_start_point = None
                for j in range(i+1, num_sents):
                    s_longest = max(s_longest, source_lengths[j])
                    t_longest = max(t_longest, target_lengths[j])
                    s_tokens = s_longest * (j-i+1)
                    t_tokens = t_longest * (j-i+1)
                    if (s_tokens > max_tokens_per_device
                        or t_tokens > max_tokens_per_device):
                        next_start_point = j
                        break
                if next_start_point is None:
                    break
                start_points.append(next_start_point)

        return start_points

    def _split_and_pad_minibatch(self, x, x_mask, y, y_mask, start_points):
        """Splits a minibatch according to a list of split points.

        Args:
            x: Numpy array with shape (factors, seq_len, batch_size)
            x_mask: Numpy array with shape (seq_len, batch_size)
            y: Numpy array with shape (seq_len, batch_size)
            y_mask: Numpy array with shape (seq_len, batch_size)
            start_points: list of zero-based indices

        Returns:
            Five lists: for each of x, x_mask, y, and y_mask, respectively,
            a list is returned containing the split version. The fifth list
            contains the (unnormalized) weights of the sub-batches.
        """

        # Split the individual arrays.

        def split_array(a, start_points):
            batch_size = a.shape[-1]
            next_points = start_points[1:] + [batch_size]
            return [a[..., p:q] for p, q in zip(start_points, next_points)]

        split_x = split_array(x, start_points)
        split_x_mask = split_array(x_mask, start_points)
        split_y = split_array(y, start_points)
        split_y_mask = split_array(y_mask, start_points)

        # Trim arrays so that the seq_len dimension is equal to the longest
        # source / target sentence in the sub-batch (rather than the whole
        # minibatch).

        def trim_arrays(arrays, new_seq_lens):
            return [a[..., 0:l, :] for a, l in zip(arrays, new_seq_lens)]

        max_lens = [int(numpy.max(numpy.sum(m, axis=0))) for m in split_x_mask]
        split_x = trim_arrays(split_x, max_lens)
        split_x_mask = trim_arrays(split_x_mask, max_lens)

        max_lens = [int(numpy.max(numpy.sum(m, axis=0))) for m in split_y_mask]
        split_y = trim_arrays(split_y, max_lens)
        split_y_mask = trim_arrays(split_y_mask, max_lens)

        # Compute the weight of each sub-batch by summing the number of
        # source and target tokens. Note that this counts actual tokens
        # (up to and including the <EOS> tokens) not the capacity of the
        # sub-batch.
        weights = [numpy.sum(s) + numpy.sum(t)
                      for s, t in zip(split_x_mask, split_y_mask)]

        # Pad the split lists with dummy arrays so that the total number of
        # sub-batches is a multiple of the number of replicas.

        remainder = len(start_points) % len(self._replicas)
        padding_size = 0 if remainder == 0 else len(self._replicas) - remainder

        def pad(split_a, padding_size):
            assert len(split_a) > 0
            dummy_array = split_a[0][..., -1:]
            for i in range(padding_size):
                split_a.append(dummy_array)

        pad(split_x, padding_size)
        pad(split_x_mask, padding_size)
        pad(split_y, padding_size)
        pad(split_y_mask, padding_size)

        for i in range(padding_size):
            weights.append(0.0)

        return split_x, split_x_mask, split_y, split_y_mask, weights


class _ModelUpdateGraph(object):
    """Defines the TensorFlow graph used by ModelUpdater."""

    def __init__(self, config, num_gpus, replicas, optimizer, global_step):
        """Constructs the graph nodes used by ModelUpdater.

        The graph has a placeholder input for each replica weight (the weight
        should be the normalized weight of the sub-batch being run by that
        replica). The placeholders are exposed to ModelUpdater via the
        self.replica_weights property.

        At various points, ModelUpdater.update() will run different parts of
        the graph depending on whether it is accumulating, applying, or
        resetting the gradients. Operations for each are exposed to
        ModelUpdater via the following properties:

            self.accum_ops
            self.apply_ops
            self.reset_ops

        The self.summary_ops property is provided for summary writing.

        Args:
            config: the model config (an argparse.Namespace)
            num_gpus: the number of available GPUs.
            replicas: a list of RNNModel or Transformer objects.
            optimizer: a TensorFlow optimizer.
            global_step: a tf.Variable to be updated by optimizer.
        """
        self._config = config
        self._num_gpus = num_gpus
        self._replicas = replicas
        self._optimizer = optimizer
        self._global_step = global_step

        # Create the placeholders for the replica weights.
        self._replica_weights = []
        for i in range(len(self._replicas)):
            name = 'replica_weight_{}'.format(i)
            placeholder = tf.placeholder(name=name, shape=(), dtype=tf.float32)
            self._replica_weights.append(placeholder)

        # Define the (non-trainable) variables for accumulating gradients and
        # losses. These need to be variables because their values must be
        # preserved over multiple runs.

        self._accumulated_loss = tf.get_variable(
            name='accumulated_loss',
            shape=[],
            initializer=tf.zeros_initializer(dtype=tf.float32),
            trainable=False)

        self._trainables, self._accumulated_gradients = {}, {}
        for i, v in enumerate(tf.trainable_variables()):
            self._trainables[v.name] = v
            g = tf.get_variable(
                name='accum'+str(i),  # FIXME better name. Variable scope?
                initializer=tf.zeros_like(v),
                trainable=False)
            self._accumulated_gradients[v.name] = g

        self._define_accum_ops()
        self._define_apply_ops()
        self._define_reset_ops()
        self._define_summary_ops()

    @property
    def replica_weights(self):
        return self._replica_weights

    @property
    def accum_ops(self):
        return self._accum_ops

    @property
    def apply_ops(self):
        return self._apply_ops

    @property
    def reset_ops(self):
        return self._reset_ops

    @property
    def summary_ops(self):
        return self._summary_ops

    def _define_reset_ops(self):
        """Defines a set of ops to reset the accumulated values to zero."""
        self._reset_ops = [v.assign(tf.zeros_like(v))
                           for v in [self._accumulated_loss] +
                                    list(self._accumulated_gradients.values())]

    def _define_accum_ops(self):
        """Defines the graph nodes used for a single accumulation step."""

        weighted_losses = []
        all_grad_vars = []

        for i in range(len(self._replicas)):
            device_type = "GPU" if self._num_gpus > 0 else "CPU"
            device_spec = tf.DeviceSpec(device_type=device_type,
                                        device_index=i)
            with tf.device(device_spec):
                with tf.variable_scope(tf.get_variable_scope(), reuse=(i>0)):
                    if self._config.loss_function == "cross-entropy":
                        loss = self._replicas[i].loss
                    elif self._config.loss_function == \
                            "per-token-cross-entropy":
                        ce_per_sent = self._replicas[i].loss_per_sentence
                        ce_total = tf.reduce_sum(ce_per_sent)
                        num_tokens = tf.reduce_sum(
                            self._replicas[i].inputs.y_mask)
                        loss = ce_total / tf.cast(num_tokens, tf.float32)
                    else:
                        assert False
                    loss = self._regularize(loss, self._config.decay_c,
                                            self._config.map_decay_c)
                    grad_vars = self._optimizer.compute_gradients(loss)
                    all_grad_vars.append(grad_vars)
                    weight = self._replica_weights[i]
                    weighted_losses.append(loss*weight)

        summed_loss = sum(weighted_losses)

        summed_grad_vars = self._sum_gradients(all_grad_vars,
                                               self._replica_weights)

        self._accum_ops = [tf.assign_add(self._accumulated_loss, summed_loss)]

        self._accum_ops += [tf.assign_add(self._accumulated_gradients[v.name],
                                          g) for g, v in summed_grad_vars]

    def _define_apply_ops(self):
        """Defines the graph nodes for applying the accumulated gradients."""

        final_loss = self._accumulated_loss

        final_grad_vars = [(self._accumulated_gradients[key],
                            self._trainables[key])
                           for key in self._trainables.keys()]

        if self._config.clip_c > 0.0:
            grads, varss = list(zip(*final_grad_vars))
            clipped_grads, global_norm = tf.clip_by_global_norm(
                grads, clip_norm=self._config.clip_c)
            # Might be interesting to see how the global norm changes over
            # time, attach a summary?
            final_grad_vars = list(zip(clipped_grads, varss))

        apply_grads = self._optimizer.apply_gradients(
            final_grad_vars,
            global_step=self._global_step)

        self._apply_ops = [self._global_step, apply_grads, final_loss]

    def _define_summary_ops(self):
        """Defines the summary ops."""
        tf.summary.scalar(name='mean_cost', tensor=self._accumulated_loss)
        tf.summary.scalar(name='t', tensor=self._global_step)
        self._summary_ops = [tf.summary.merge_all()]

    def _regularize(self, loss, decay_c, map_decay_c):
        """Optionally, adds L2 and MAP-L2 regularization terms to the loss."""
        with tf.variable_scope("loss"):
            # Optionally, add an L2 loss term.
            if decay_c > 0.0:
                l2_sum = tf.add_n([tf.nn.l2_loss(v)
                                   for v in tf.trainable_variables()])
                l2_loss = l2_sum * tf.constant(decay_c, dtype=tf.float32)
                loss += l2_loss
            # Optionally, add an L2 loss term based on a prior model.
            if map_decay_c > 0.0:
                map_l2_loss = tf.constant(0.0, dtype=tf.float32)
                map_l2_acc = []
                for v in tf.trainable_variables():
                    prior_name = 'prior/'+v.name.split(':')[0]
                    prior_v = tf.get_variable(
                        prior_name, initializer=v.initialized_value(),
                        trainable=False, collections=['prior_variables'],
                        dtype=v.initialized_value().dtype)
                    map_l2_acc.append(tf.nn.l2_loss(v - prior_v))
                map_l2_loss = (tf.add_n(map_l2_acc)
                               * tf.constant(map_decay_c, dtype=tf.float32))
                loss += map_l2_loss
        return loss

    def _sum_gradients(self, all_grad_vars, weights):
        """Computes the weighted sums of gradients from multiple sub-batches.

        Args:
            all_grad_vars: a list of lists of (gradient, variable) pairs. The
                outer list should contain one entry for each sub-batch. Each
                inner list should contain the optimizer's (gradient, variable)
                list for that sub-batch.
            weights: a list containing the normalized weight of each sub-batch.

        Returns:
            A list of (gradient, variable) pairs.
        """
        # Create a dictionary mapping each variable name to a list of
        # (gradient, variable) pairs (one pair from each sub-batch).
        d = {}
        for grad_vars in all_grad_vars:
            for g, v in grad_vars:
                if v.name not in d:
                    d[v.name] = []
                d[v.name].append((g, v))

        # For each variable, sum the gradients from all sub-batches and store
        # the result in avg_grad_vars.
        avg_grad_vars = []
        for var_name, gv_list in list(d.items()):
            var = gv_list[0][1]
            found_none_value = False
            for g, v in gv_list:
                if g is None:
                    found_none_value = True
                    break
            if found_none_value:
                avg_grad_vars.append((None, var))
            else:
                weighted_grads = []
                for i, (g, v) in enumerate(gv_list):
                    assert v == var
                    expanded = tf.expand_dims(g * weights[i], 0)
                    weighted_grads.append(expanded)
                tmp = tf.concat(axis=0, values=weighted_grads)
                avg_grad = tf.reduce_sum(tmp, 0)
                avg_grad_vars.append((avg_grad, var))

        return avg_grad_vars
