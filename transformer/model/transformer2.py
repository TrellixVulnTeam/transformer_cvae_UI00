# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Defines the Transformer model, and its encoder and decoder stacks.

Model paper: https://arxiv.org/pdf/1706.03762.pdf
Transformer model code source: https://github.com/tensorflow/tensor2tensor
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf  # pylint: disable=g-bad-import-order

from official.transformer.model import attention_layer
from official.transformer.model import beam_search
from official.transformer.model import embedding_layer
from official.transformer.model import ffn_layer
from official.transformer.model import model_utils
from official.transformer.utils.tokenizer import EOS_ID

_NEG_INF = -1e9


class Transformer(object):
  """Transformer model for sequence to sequence data.

  Implemented as described in: https://arxiv.org/pdf/1706.03762.pdf

  The Transformer model consists of an encoder and decoder. The input is an int
  sequence (or a batch of sequences). The encoder produces a continous
  representation, and the decoder uses the encoder output to generate
  probabilities for the output sequence.
  """

  def __init__(self, params, train):
    """Initialize layers to build Transformer model.

    Args:
      params: hyperparameter object defining layer sizes, dropout values, etc.
      train: boolean indicating whether the model is in training mode. Used to
        determine if dropout layers should be added.
    """
    self.train = train
    self.params = params

    self.embedding_softmax_layer = embedding_layer.EmbeddingSharedWeights(
        params["vocab_size"], params["hidden_size"],
        method="matmul" if params["tpu"] else "gather")

    self.src_encoder_input_layer = EncoderInputLayer(params, train)
    self.src_encoder_stack = EncoderStack(params, train) # encode source sentence
    self.src_sent_emb_layer = SentenceEmbeddingLayer(params, train) # embed source sentence

    # debug
    if self.train:
      self.tgt_encoder_input_layer = EncoderInputLayer(params, train)
      self.tgt_encoder_stack = EncoderStack(params, train) # encode target sentence
      self.tgt_sent_emb_layer = SentenceEmbeddingLayer(params, train) # embed target sentence

    self.latent_variable_layer = LatentVariableLayer(params, train) # generate latent variable
    #self.encoder_output_layer = EncoderOutputLayer(params, train) # generate encoder output

    self.decoder_stack = DecoderStack(params, train) # decode

  def __call__(self, inputs, targets=None):
    """Calculate target logits or inferred target sequences.

    Args:
      inputs: int tensor with shape [batch_size, input_length].
      targets: None or int tensor with shape [batch_size, target_length].

    Returns:
      If targets is defined, then return logits for each word in the target
      sequence. float tensor with shape [batch_size, target_length, vocab_size]
      If target is none, then generate output sequence one token at a time.
        returns a dictionary {
          output: [batch_size, decoded length]
          score: [batch_size, float]}
    """
    # Variance scaling is used here because it seems to work in many problems.
    # Other reasonable initializers may also work just as well.
    initializer = tf.variance_scaling_initializer(
        self.params["initializer_gain"], mode="fan_avg", distribution="uniform")
    with tf.variable_scope("Transformer", initializer=initializer):
      # Calculate attention bias for encoder self-attention and decoder
      # multi-headed attention layers.
      src_attention_bias = model_utils.get_padding_bias(inputs) # used for 1.src_encode self-att; 2.decode en-de att.
      if targets is not None: # tc modified
        tgt_attention_bias = model_utils.get_padding_bias(targets) # only used for tgt_encode self-att.
      else: tgt_attention_bias = None

      # Run the inputs through the encoder layer to map the symbol
      # representations to continuous representations.
      (encoder_outputs, latent_sample, 
       prior_mu, prior_logvar, 
       recog_mu, recog_logvar) = self.encode(inputs, src_attention_bias, 
                                             targets, tgt_attention_bias) # tc modified

      # Generate output sequence if targets is None, or return logits if target
      # sequence is known.
      if targets is None:
        logits = self.predict(encoder_outputs, src_attention_bias, latent_sample)
      else:
        logits = self.decode(targets, encoder_outputs, src_attention_bias, latent_sample)

      return logits, latent_sample, prior_mu, prior_logvar, recog_mu, recog_logvar

  def encode(self, inputs, src_attention_bias, targets=None, tgt_attention_bias=None):
    """Generate continuous representation for inputs.

    Args:
      inputs: int tensor with shape [batch_size, input_length].
      attention_bias: float tensor with shape [batch_size, 1, 1, input_length]

    Returns:
      float tensor with shape [batch_size, input_length, hidden_size]
    """
    with tf.name_scope("encode"):
      # get source sentence embedding with size [batch_size, hidden_size]
      #with tf.name_scope("src_sentence_embedding"): # tc modified
      with tf.variable_scope("src_sentence_embedding"): # tc modified
        src_embedded_inputs = self.embedding_softmax_layer(inputs)
        src_encoder_inputs, src_inputs_padding = self.src_encoder_input_layer(inputs, src_embedded_inputs) # 1.get padding; 2.add position encoding.
        src_encoder_outputs = self.src_encoder_stack(src_encoder_inputs, src_attention_bias, src_inputs_padding) # get size [batch_size, length, hidden_size]
        src_embedding  = self.src_sent_emb_layer(src_encoder_outputs, src_inputs_padding) # get size [batch_size, hidden_size]
    
      # get target sentence embedding with size [batch_size, hidden_size]
      #with tf.name_scope("tgt_sentence_embedding"):
      with tf.variable_scope("tgt_sentence_embedding"):
        if self.train:
          tgt_embedded_inputs = self.embedding_softmax_layer(targets)
          tgt_encoder_inputs, tgt_inputs_padding = self.tgt_encoder_input_layer(targets, tgt_embedded_inputs)
          tgt_encoder_outputs = self.tgt_encoder_stack(tgt_encoder_inputs, tgt_attention_bias, tgt_inputs_padding)
          tgt_embedding = self.tgt_sent_emb_layer(tgt_encoder_outputs, tgt_inputs_padding) # size same with src_embedding

      # get latent variable
      #with tf.name_scope("latent_variable"):
      with tf.variable_scope("latent_variable"):
        if not self.train: tgt_embedding = None
        latent_sample, prior_mu, prior_logvar, recog_mu, recog_logvar = self.latent_variable_layer(src_embedding, tgt_embedding) # get size [batch_size, hidden_size]

      # no longer contact latent_sample to src_encoder_outputs
      # encoder provide two things to decoder: 1.src_encoder_outputs, 2.latent_sample.
      encoder_outputs = src_encoder_outputs

      """ 
      # contact latent variable to src_encoder outputs
      #with tf.name_scope("contact_latent_varible_to_source"):
      with tf.variable_scope("contact_latent_varible_to_source"):
        latent_sample_tiled = tf.expand_dims(latent_sample, axis=1) # get size [batch_size, 1, hidden_size]
        latent_sample_tiled = tf.tile(latent_sample_tiled, [1, tf.shape(src_encoder_outputs)[1], 1]) # get size [batch_size, length, hidden_size]
        encoder_outputs = tf.concat([src_encoder_outputs, latent_sample_tiled], axis=-1) # get size [batch_size, length, 2*hidden_size]
        encoder_outputs = self.encoder_output_layer(encoder_outputs) # get size [batch_size, length, hidden_size]
      """

      return encoder_outputs, latent_sample, prior_mu, prior_logvar, recog_mu, recog_logvar

  #def decode(self, targets, encoder_outputs, attention_bias):
  def decode(self, targets, encoder_outputs, attention_bias, latent_sample):
    """Generate logits for each value in the target sequence.

    Args:
      targets: target values for the output sequence.
        int tensor with shape [batch_size, target_length]
      encoder_outputs: continuous representation of input sequence.
        float tensor with shape [batch_size, input_length, hidden_size]
      attention_bias: float tensor with shape [batch_size, 1, 1, input_length]

    Returns:
      float32 tensor with shape [batch_size, target_length, vocab_size]
    """
    with tf.name_scope("decode"):
      # Prepare inputs to decoder layers by shifting targets, adding positional
      # encoding and applying dropout.
      decoder_inputs = self.embedding_softmax_layer(targets)
      with tf.name_scope("shift_targets"):
        # Shift targets to the right, and remove the last element
        decoder_inputs = tf.pad(
            decoder_inputs, [[0, 0], [1, 0], [0, 0]])[:, :-1, :]
      with tf.name_scope("add_pos_encoding"):
        with tf.name_scope("length"):
          length = tf.shape(decoder_inputs)[1]
        decoder_inputs += model_utils.get_position_encoding(
            length, self.params["hidden_size"])
      if self.train:
        decoder_inputs = tf.nn.dropout(
            decoder_inputs, 1 - self.params["layer_postprocess_dropout"])

      # Run values
      decoder_self_attention_bias = model_utils.get_decoder_self_attention_bias(
          length)
      outputs = self.decoder_stack(
          decoder_inputs, encoder_outputs, decoder_self_attention_bias,
          attention_bias, latent_sample=latent_sample)
      logits = self.embedding_softmax_layer.linear(outputs) # get [batch_size, length, vocab_size]
      return logits

  def _get_symbols_to_logits_fn(self, max_decode_length):
    """Returns a decoding function that calculates logits of the next tokens."""

    timing_signal = model_utils.get_position_encoding(
        max_decode_length + 1, self.params["hidden_size"])
    decoder_self_attention_bias = model_utils.get_decoder_self_attention_bias(
        max_decode_length)

    def symbols_to_logits_fn(ids, i, cache):
      """Generate logits for next potential IDs.

      Args:
        ids: Current decoded sequences.
          int tensor with shape [batch_size * beam_size, i + 1]
        i: Loop index
        cache: dictionary of values storing the encoder output, encoder-decoder
          attention bias, and previous decoder attention values.

      Returns:
        Tuple of
          (logits with shape [batch_size * beam_size, vocab_size],
           updated cache values)
      """
      # Set decoder input to the last generated IDs
      decoder_input = ids[:, -1:]

      # Preprocess decoder input by getting embeddings and adding timing signal.
      decoder_input = self.embedding_softmax_layer(decoder_input)
      decoder_input += timing_signal[i:i + 1]

      self_attention_bias = decoder_self_attention_bias[:, :, i:i + 1, :i + 1]
      decoder_outputs = self.decoder_stack(
          decoder_input, cache.get("encoder_outputs"), self_attention_bias,
          cache.get("encoder_decoder_attention_bias"),
          cache.get("latent_sample"), cache)
      logits = self.embedding_softmax_layer.linear(decoder_outputs)
      logits = tf.squeeze(logits, axis=[1])
      return logits, cache
    return symbols_to_logits_fn

  def predict(self, encoder_outputs, encoder_decoder_attention_bias, latent_sample):
    """Return predicted sequence."""
    batch_size = tf.shape(encoder_outputs)[0]
    input_length = tf.shape(encoder_outputs)[1]
    max_decode_length = input_length + self.params["extra_decode_length"]

    symbols_to_logits_fn = self._get_symbols_to_logits_fn(max_decode_length)

    # Create initial set of IDs that will be passed into symbols_to_logits_fn.
    initial_ids = tf.zeros([batch_size], dtype=tf.int32)

    # Create cache storing decoder attention values for each layer.
    cache = {
        "layer_%d" % layer: {
            "k": tf.zeros([batch_size, 0, self.params["hidden_size"]]),
            "v": tf.zeros([batch_size, 0, self.params["hidden_size"]]),
        } for layer in range(self.params["num_hidden_layers"])}

    # Add encoder output and attention bias to the cache.
    cache["encoder_outputs"] = encoder_outputs
    cache["encoder_decoder_attention_bias"] = encoder_decoder_attention_bias
    cache["latent_sample"] = latent_sample

    # Use beam search to find the top beam_size sequences and scores.
    decoded_ids, scores = beam_search.sequence_beam_search(
        symbols_to_logits_fn=symbols_to_logits_fn,
        initial_ids=initial_ids,
        initial_cache=cache,
        vocab_size=self.params["vocab_size"],
        beam_size=self.params["beam_size"],
        alpha=self.params["alpha"],
        max_decode_length=max_decode_length,
        eos_id=EOS_ID)

    # Get the top sequence for each batch element
    top_decoded_ids = decoded_ids[:, 0, 1:]
    top_scores = scores[:, 0]

    return {"outputs": top_decoded_ids, "scores": top_scores}


class LayerNormalization(tf.layers.Layer):
  """Applies layer normalization."""

  def __init__(self, hidden_size):
    super(LayerNormalization, self).__init__()
    self.hidden_size = hidden_size

  def build(self, _):
    self.scale = tf.get_variable("layer_norm_scale", [self.hidden_size],
                                 initializer=tf.ones_initializer())
    self.bias = tf.get_variable("layer_norm_bias", [self.hidden_size],
                                initializer=tf.zeros_initializer())
    self.built = True

  def call(self, x, epsilon=1e-6):
    #with tf.name_scope("layer_normalization"): # debug
    mean = tf.reduce_mean(x, axis=[-1], keepdims=True)
    variance = tf.reduce_mean(tf.square(x - mean), axis=[-1], keepdims=True)
    #with tf.name_scope("norm"): # debug
    norm_x = (x - mean) * tf.rsqrt(variance + epsilon)
    return norm_x * self.scale + self.bias

class EncoderInputLayer(object):
  """"""
  def __init__(self, params, train):
    #super(EncoderInputLayer, self).__init__()
    self.params = params
    self.train = train

  def __call__(self, inputs, embedded_inputs):
    """1.get padding; 2.add position encoding.
      Args:
        inputs:          size with [batch_size, length]
        embedded_inputs: size with [batch_size, length, hidden_size]
      return: 
        encoder_inputs:  size with [batch_size, length, hidden_size]
        inputs_padding:  size with [batch_size, length]
    """
    with tf.name_scope("stack_input"):
      inputs_padding = model_utils.get_padding(inputs)
      length = tf.shape(inputs)[1]
      pos_encoding = model_utils.get_position_encoding(length, self.params["hidden_size"])
      encoder_inputs = embedded_inputs + pos_encoding
      if self.train:
        encoder_inputs = tf.nn.dropout(encoder_inputs, 1-self.params["layer_postprocess_dropout"])
    return encoder_inputs, inputs_padding


#class SentenceEmbeddingLayer(object):
class SentenceEmbeddingLayer(tf.layers.Layer):
  """"""
  def __init__(self, params, train):
    super(SentenceEmbeddingLayer, self).__init__()
    self.sent_attention_layer = ffn_layer.FeedFowardNetwork( # 2 sub-layers, one is feedfoward with activation, another is linear
        params["hidden_size"], params["hidden_size"],
        params["relu_dropout"], train, params["allow_ffn_pad"],
        output_size = 1, activation=tf.nn.relu)
        #output_size = 1, activation=tf.nn.tanh)
    self.sent_attention_layer = PrePostProcessingWrapper(
        self.sent_attention_layer, params, train,
        input_hidden_size = params["hidden_size"],
        output_hidden_size = 1, norm=False) # encoder_stack do nomarlization, do not re-normarlize

    #self.output_norm_layer = LayerNormalization(params["hidden_size"]) # latent layer will do normalization

  def __call__(self, inputs, inputs_padding):
    """
        Args:
            inputs: size with [batch_size, input_length,  hidden_size]
            inputs_padding: size with [batch_size, input_length], 1 for padding, 0 for non-padding
        return: size with [batch_size, hidden_size]
    """
    with tf.name_scope("sentence_embedding"):
      with tf.name_scope("attention"):
        logits = self.sent_attention_layer(inputs, inputs_padding) # get size [batch_size, length, 1]

      # do masking before softmax
      with tf.name_scope("mask"):
        mask_condition = inputs_padding < 1e-9 # get size [batch_size, lenght] False for padding, True for non-padding
        mask_condition = tf.expand_dims(mask_condition, axis = -1) 
        mask_values = tf.ones_like(logits) * _NEG_INF # get size [batch_size, length, 1]
        logits = tf.where(mask_condition, logits, mask_values)

      with tf.name_scope("weighted_sum"):
        sent_attention = tf.nn.softmax(logits, axis=1) # softmax, still with size [batch_size, length, 1]
        sent_embedding = tf.reduce_sum(inputs * sent_attention, axis=1) # use attention, get size with [batch_size, hidden_size]

    #return self.output_norm_layer(sent_embedding) # do normalization before outputing
    return sent_embedding

#class EncoderOutputLayer(object):
class EncoderOutputLayer(tf.layers.Layer):
  """ encoder output layer
  """
  def __init__(self, params, train):
    super(EncoderOutputLayer, self).__init__()
    input_hidden_size = 2 * params["hidden_size"]
    output_hidden_size = params["hidden_size"]

    self.feed_foward_layer = ffn_layer.FeedFowardNetwork(
        #input_hidden_size, params["hidden_size"],
        #input_hidden_size, params["filter_size"],
        input_hidden_size, output_hidden_size,
        params["relu_dropout"], train, params["allow_ffn_pad"],
        output_size = output_hidden_size,
        activation=tf.nn.relu)
        #activation=tf.nn.tanh)

    self.feed_foward_layer = PrePostProcessingWrapper(
        self.feed_foward_layer, params, train, 
        input_hidden_size = input_hidden_size,
        output_hidden_size = output_hidden_size)

    self.output_norm_layer = LayerNormalization(output_hidden_size)

  def __call__(self, inputs):
    """
        inputs: size with [batch_size, length, 2*hidden_size]
        return: size with [batch_size, length, hidden_size]
    """
    with tf.name_scope("encoder_output_layer"):
      inputs_padding = model_utils.get_padding(inputs)
      outputs = self.feed_foward_layer(inputs, padding=inputs_padding)
      return self.output_norm_layer(outputs)

class LatentVariableLayer(tf.layers.Layer):
  """"""
  def __init__(self, params, train):
    super(LatentVariableLayer, self).__init__()
    self.train = train
    #output_hidden_size = 2 * params["hidden_size"] # use hidden_size as latent_size
    output_hidden_size = 2 * params["latent_size"]

    self.prior_ffl = ffn_layer.FeedFowardNetwork(
        params["hidden_size"], params["filter_size"],
        params["relu_dropout"], train, params["allow_ffn_pad"],
        output_size = output_hidden_size,
        activation=tf.nn.relu)
    self.prior_ffl = PrePostProcessingWrapper(
        self.prior_ffl, params, train,
        input_hidden_size = params["hidden_size"],
        output_hidden_size = output_hidden_size,
        norm=True, drop=True, residual=False) 
        #norm=True, drop=False, residual=False) 

    if self.train:
      input_hidden_size =  2 * params["hidden_size"]
      self.recog_ffl = ffn_layer.FeedFowardNetwork(
          input_hidden_size, params["filter_size"],
          params["relu_dropout"], train, params["allow_ffn_pad"],
          output_size = output_hidden_size,
          activation=tf.nn.relu)
      self.recog_ffl = PrePostProcessingWrapper(
          self.recog_ffl, params, train,
          input_hidden_size = input_hidden_size,
          output_hidden_size = output_hidden_size,
          norm=True, drop=False, residual=False) 

  def __call__(self, src_embedding, tgt_embedding):

    with tf.variable_scope("prior"):
      src_embedding_expd = tf.expand_dims(src_embedding, axis=1) # get size [batch_size, 1, hidden_size]
      prior_mu_logvar = self.prior_ffl(src_embedding_expd, padding=None) # get size [batch_size, 1, 2*latent_size]
      prior_mu_logvar = tf.squeeze(prior_mu_logvar, axis = 1) # get size [batch_size, 2 * latent_size]
      prior_mu, prior_logvar = tf.split(prior_mu_logvar, 2, axis = -1) # both with size [batch_size, latent_size]
      latent_sample = self._sample_gaussian(prior_mu, prior_logvar) # get size [batch_size, latent_size]
      recog_mu, recog_logvar = None, None

    with tf.variable_scope("recog"):
      if self.train:
        src_tgt_embedding = tf.concat([src_embedding, tgt_embedding], -1) # get size [batch_size, 2*hidden_size]
        src_tgt_embedding = tf.expand_dims(src_tgt_embedding, axis=1) # get size [batch_size, 1, 2*hidden_size]
        recog_mu_logvar = self.recog_ffl(src_tgt_embedding, padding=None) # get size [batch_size, 1, 2*latent_size]
        recog_mu_logvar = tf.squeeze(recog_mu_logvar, axis = 1) # get size [batch_size, 2*latent_size]
        recog_mu, recog_logvar = tf.split(recog_mu_logvar, 2, axis = -1) # both with size [batch_size, latent_size]
        latent_sample = self._sample_gaussian(recog_mu, recog_logvar) # get size [batch_size, latent_size]

    return latent_sample, prior_mu, prior_logvar, recog_mu, recog_logvar

  def _sample_gaussian(self, mu, logvar):
    """
      Args: 
        mu:     size [batch_size, latent_size]
        logvar: size [batch_size, latent_size]
      Return:
        z: latent_variable, size [batch_size, latent_size]
    """
    epsilon = tf.random_normal(tf.shape(logvar), name="epsilon")
    std = tf.exp(0.5 * logvar)
    z = mu + tf.multiply(std, epsilon)
    return z



class PrePostProcessingWrapper(object):
  """Wrapper class that applies layer pre-processing and post-processing."""

  # must tell i/o sizes, because it has to construct sub-layer
  def __init__(self, layer, params, train, input_hidden_size=None, 
        output_hidden_size=None, norm=True, drop=True, residual=True):
    self.layer = layer
    self.postprocess_dropout = params["layer_postprocess_dropout"]
    self.train = train
    if (input_hidden_size is not None and output_hidden_size is not None):
      self.input_hidden_size = input_hidden_size
      self.output_hidden_size = output_hidden_size
    else:
      self.input_hidden_size = params["hidden_size"]
      self.output_hidden_size = params["hidden_size"]
    self.norm = norm
    self.drop = drop
    self.residual = residual
    assert (self.norm or self.drop or self.residual)

    # Create normalization layer
    if self.norm:
      self.layer_norm = LayerNormalization(self.input_hidden_size)

  def __call__(self, x, *args, **kwargs):
    # Preprocessing: apply layer normalization
    if self.norm:
      y = self.layer_norm(x)
    else:
      y = x

    # Get layer output
    y = self.layer(y, *args, **kwargs)

    # Postprocessing: apply dropout and residual connection
    if self.train and self.drop:
      y = tf.nn.dropout(y, 1 - self.postprocess_dropout)

    # if i/o sizes are equal, add them, else only o
    if self.input_hidden_size == self.output_hidden_size and self.residual:
      return x + y 
    else:
      return y


class EncoderStack(tf.layers.Layer):
  """Transformer encoder stack.

  The encoder stack is made up of N identical layers. Each layer is composed
  of the sublayers:
    1. Self-attention layer
    2. Feedforward network (which is 2 fully-connected layers)
  """

  def __init__(self, params, train):
    super(EncoderStack, self).__init__()
    self.layers = []
    for _ in range(params["num_hidden_layers"]):
      # Create sublayers for each layer.
      self_attention_layer = attention_layer.SelfAttention(
          params["hidden_size"], params["num_heads"],
          params["attention_dropout"], train)
      feed_forward_network = ffn_layer.FeedFowardNetwork(
          params["hidden_size"], params["filter_size"],
          params["relu_dropout"], train, params["allow_ffn_pad"])

      self.layers.append([
          PrePostProcessingWrapper(self_attention_layer, params, train),
          PrePostProcessingWrapper(feed_forward_network, params, train)])

    # Create final layer normalization layer.
    self.output_normalization = LayerNormalization(params["hidden_size"]) # sentence embedding layer will do normalization

  def call(self, encoder_inputs, attention_bias, inputs_padding):
    """Return the output of the encoder layer stacks.

    Args:
      encoder_inputs: tensor with shape [batch_size, input_length, hidden_size]
      attention_bias: bias for the encoder self-attention layer.
        [batch_size, 1, 1, input_length]
      inputs_padding: P

    Returns:
      Output of encoder layer stack.
      float32 tensor with shape [batch_size, input_length, hidden_size]
    """
    with tf.name_scope("stack"):
      for n, layer in enumerate(self.layers):
        # Run inputs through the sublayers.
        self_attention_layer = layer[0]
        feed_forward_network = layer[1]

        with tf.variable_scope("layer_%d" % n):
          with tf.variable_scope("self_attention"):
            encoder_inputs = self_attention_layer(encoder_inputs, attention_bias)
          with tf.variable_scope("ffn"):
            encoder_inputs = feed_forward_network(encoder_inputs, inputs_padding)

    return self.output_normalization(encoder_inputs)
    #return encoder_inputs


class DecoderStack(tf.layers.Layer):
  """Transformer decoder stack.

  Like the encoder stack, the decoder stack is made up of N identical layers.
  Each layer is composed of the sublayers:
    1. Self-attention layer
    2. Multi-headed attention layer combining encoder outputs with results from
       the previous self-attention layer.
    3. Feedforward network (2 fully-connected layers)
  """

  def __init__(self, params, train):
    super(DecoderStack, self).__init__()
    self.layers = []
    for _ in range(params["num_hidden_layers"]):
      self_attention_layer = attention_layer.SelfAttention(
          params["hidden_size"], params["num_heads"],
          params["attention_dropout"], train)
      enc_dec_attention_layer = attention_layer.Attention(
          params["hidden_size"], params["num_heads"],
          params["attention_dropout"], train)
      feed_forward_network = ffn_layer.FeedFowardNetwork(
          #params["hidden_size"], params["filter_size"],
          #params["hidden_size"] * 2, params["filter_size"],
          params["hidden_size"] + params["latent_size"], params["filter_size"],
          params["relu_dropout"], train, params["allow_ffn_pad"],
          output_size=params["hidden_size"])

      self.layers.append([
          PrePostProcessingWrapper(self_attention_layer, params, train),
          PrePostProcessingWrapper(enc_dec_attention_layer, params, train),
          PrePostProcessingWrapper(feed_forward_network, params, train, 
            input_hidden_size=params["hidden_size"] + params["latent_size"], 
            output_hidden_size=params["hidden_size"])])

    self.output_normalization = LayerNormalization(params["hidden_size"])

  def call(self, decoder_inputs, encoder_outputs, decoder_self_attention_bias,
           attention_bias, latent_sample, cache=None):
           #attention_bias, cache=None):
    """Return the output of the decoder layer stacks.

    Args:
      decoder_inputs: tensor with shape [batch_size, target_length, hidden_size]
      encoder_outputs: tensor with shape [batch_size, input_length, hidden_size]
      decoder_self_attention_bias: bias for decoder self-attention layer.
        [1, 1, target_len, target_length]
      attention_bias: bias for encoder-decoder attention layer.
        [batch_size, 1, 1, input_length]
      cache: (Used for fast decoding) A nested dictionary storing previous
        decoder self-attention values. The items are:
          {layer_n: {"k": tensor with shape [batch_size, i, key_channels],
                     "v": tensor with shape [batch_size, i, value_channels]},
           ...}
      latent_sample: tensor with shape [batch_size, hidden_size]

    Returns:
      Output of decoder layer stack.
      float32 tensor with shape [batch_size, target_length, hidden_size]
    """
    for n, layer in enumerate(self.layers):
      self_attention_layer = layer[0]
      enc_dec_attention_layer = layer[1]
      feed_forward_network = layer[2]

      # Run inputs through the sublayers.
      layer_name = "layer_%d" % n
      layer_cache = cache[layer_name] if cache is not None else None
      with tf.variable_scope(layer_name):
        with tf.variable_scope("self_attention"):
          decoder_inputs = self_attention_layer(
              decoder_inputs, decoder_self_attention_bias, cache=layer_cache)
        with tf.variable_scope("encdec_attention"):
          decoder_inputs = enc_dec_attention_layer(
              decoder_inputs, encoder_outputs, attention_bias)

        # contact latent_sample
        with tf.variable_scope("concat_latent_sample"):
          latent_sample_tiled = tf.expand_dims(latent_sample, axis=1) # get size [batch_size, 1, latent_size]
          latent_sample_tiled = tf.tile(latent_sample_tiled, [1, tf.shape(decoder_inputs)[1], 1]) # get size [batch_size, length, latent_size]
          decoder_inputs = tf.concat([decoder_inputs, latent_sample_tiled], axis=-1) # get size [batch_size, length, hidden_size+latent_size]

        with tf.variable_scope("ffn"):
          decoder_inputs = feed_forward_network(decoder_inputs)

    return self.output_normalization(decoder_inputs)
