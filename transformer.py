"""
Transformer Seq2seq.
Put 7x7x1280 as the encoder input, and output HTML+CSS text as the decoder output

See html_SXN_parser/parser.py's comment to see more explaination related to parsing and more implementation strategy

Author: Samuel Koesnadi 2019

Attention weights naming:
decoder_layer4_block2 means 4th layer (from maximum num_layers) and second block (from the two blocks that decoder has)
"""

from common_definitions import *
from utils import *
from dataset import *
from html_SXN_parser.parser import decode_2_html

### POSITIONAL ENCODING
def get_angles(pos, i, d_model):
  angle_rates = 1 / np.power(10000, (2 * (i//2)) / np.float32(d_model))
  return pos * angle_rates

def raw_positional_encoding(position, d_model):
	# there is no new dimension added here
	angle_rads = get_angles(np.arange(position)[:, np.newaxis],
	                        np.arange(d_model)[np.newaxis, :],
	                        d_model)

	# apply sin to even indices in the array; 2i
	angle_rads[:, 0::2] = np.sin(angle_rads[:, 0::2])

	# apply cos to odd indices in the array; 2i+1
	angle_rads[:, 1::2] = np.cos(angle_rads[:, 1::2])

	return tf.cast(angle_rads, dtype=tf.float32)

def positional_encoding(position, d_model):
	return raw_positional_encoding(position, d_model)[np.newaxis, ...]


def create_padding_mask(seq):
	seq = tf.cast(tf.math.equal(seq, 0), tf.float32)

	# add extra dimensions to add the padding
	# to the attention logits.
	return seq[:, tf.newaxis, tf.newaxis, :]  # (batch_size, 1, 1, seq_len)

def create_look_ahead_mask(size):  # smart thing going on here I should say
  mask = 1 - tf.linalg.band_part(tf.ones((size, size)), -1, 0)
  return mask  # (seq_len, seq_len)


def scaled_dot_product_attention(q, k, v, mask):
	"""Calculate the attention weights.
	q, k, v must have matching leading dimensions.
	k, v must have matching penultimate dimension, i.e.: seq_len_k = seq_len_v.
	The mask has different shapes depending on its type(padding or look ahead)
	but it must be broadcastable for addition.

	Args:
	  q: query shape == (..., seq_len_q, depth)
	  k: key shape == (..., seq_len_k, depth)
	  v: value shape == (..., seq_len_v, depth_v)
	  mask: Float tensor with shape broadcastable
			to (..., seq_len_q, seq_len_k). Defaults to None.

	Returns:
	  output, attention_weights
	"""

	matmul_qk = tf.matmul(q, k, transpose_b=True)  # (..., seq_len_q, seq_len_k)

	# scale matmul_qk
	dk = tf.cast(tf.shape(k)[-1], tf.float32)
	scaled_attention_logits = matmul_qk / tf.math.sqrt(dk)

	# add the mask to the scaled tensor.
	if mask is not None:
		scaled_attention_logits += (mask * -1e9)

	# softmax is normalized on the last axis (seq_len_k) so that the scores
	# add up to 1.
	attention_weights = tf.nn.softmax(scaled_attention_logits, axis=-1)  # (..., seq_len_q, seq_len_k)

	output = tf.matmul(attention_weights, v)  # (..., seq_len_q, depth_v)

	return output, attention_weights


class MultiHeadAttention(tf.keras.layers.Layer):
	def __init__(self, d_model, num_heads):
		super(MultiHeadAttention, self).__init__()
		self.num_heads = num_heads
		self.d_model = d_model

		assert d_model % self.num_heads == 0

		self.depth = d_model // self.num_heads

		self.wq = tf.keras.layers.Dense(d_model)
		self.wk = tf.keras.layers.Dense(d_model)
		self.wv = tf.keras.layers.Dense(d_model)

		self.dense = tf.keras.layers.Dense(d_model)

	def split_heads(self, x, batch_size):
		"""Split the last dimension into (num_heads, depth).
		Transpose the result such that the shape is (batch_size, num_heads, seq_len, depth)
		"""
		x = tf.reshape(x, (batch_size, -1, self.num_heads, self.depth))
		return tf.transpose(x, perm=[0, 2, 1, 3])

	def call(self, v, k, q, mask):
		batch_size = tf.shape(q)[0]

		q = self.wq(q)  # (batch_size, seq_len, d_model)
		k = self.wk(k)  # (batch_size, seq_len, d_model)
		v = self.wv(v)  # (batch_size, seq_len, d_model)

		q = self.split_heads(q, batch_size)  # (batch_size, num_heads, seq_len_q, depth)
		k = self.split_heads(k, batch_size)  # (batch_size, num_heads, seq_len_k, depth)
		v = self.split_heads(v, batch_size)  # (batch_size, num_heads, seq_len_v, depth)

		# scaled_attention.shape == (batch_size, num_heads, seq_len_q, depth)
		# attention_weights.shape == (batch_size, num_heads, seq_len_q, seq_len_k)
		scaled_attention, attention_weights = scaled_dot_product_attention(
			q, k, v, mask)

		scaled_attention = tf.transpose(scaled_attention,
		                                perm=[0, 2, 1, 3])  # (batch_size, seq_len_q, num_heads, depth)

		concat_attention = tf.reshape(scaled_attention,
		                              (batch_size, -1, self.d_model))  # (batch_size, seq_len_q, d_model)

		output = self.dense(concat_attention)  # (batch_size, seq_len_q, d_model)

		return output, attention_weights

### Point wise feed forward network
def point_wise_feed_forward_network(d_model, dff):
  return tf.keras.Sequential([
      tf.keras.layers.Dense(dff, activation=ACTIVATION, kernel_initializer=KERNEL_INITIALIZER),  # (batch_size, seq_len, dff)
      tf.keras.layers.Dense(d_model)  # (batch_size, seq_len, d_model)
  ])


class EncoderLayer(tf.keras.layers.Layer):
	def __init__(self, d_model, num_heads, dff, rate=0.1):
		super(EncoderLayer, self).__init__()

		self.mha = MultiHeadAttention(d_model, num_heads)
		self.ffn = point_wise_feed_forward_network(d_model, dff)

		self.layernorm1 = tf.keras.layers.LayerNormalization(epsilon=1e-6)
		self.layernorm2 = tf.keras.layers.LayerNormalization(epsilon=1e-6)

		self.dropout1 = tf.keras.layers.Dropout(rate)
		self.dropout2 = tf.keras.layers.Dropout(rate)

	def call(self, x, training, mask):
		attn_output, _ = self.mha(x, x, x, mask)  # (batch_size, input_seq_len, d_model)
		attn_output = self.dropout1(attn_output, training=training)
		out1 = self.layernorm1(x + attn_output)  # (batch_size, input_seq_len, d_model)

		ffn_output = self.ffn(out1)  # (batch_size, input_seq_len, d_model)
		ffn_output = self.dropout2(ffn_output, training=training)
		out2 = self.layernorm2(out1 + ffn_output)  # (batch_size, input_seq_len, d_model)

		return out2


class DecoderLayer(tf.keras.layers.Layer):
	def __init__(self, d_model, num_heads, dff, rate=0.1):
		super(DecoderLayer, self).__init__()

		self.mha1 = MultiHeadAttention(d_model, num_heads)
		self.mha2 = MultiHeadAttention(d_model, num_heads)

		self.ffn = point_wise_feed_forward_network(d_model, dff)

		self.layernorm1 = tf.keras.layers.LayerNormalization(epsilon=1e-6)
		self.layernorm2 = tf.keras.layers.LayerNormalization(epsilon=1e-6)
		self.layernorm3 = tf.keras.layers.LayerNormalization(epsilon=1e-6)

		self.dropout1 = tf.keras.layers.Dropout(rate)
		self.dropout2 = tf.keras.layers.Dropout(rate)
		self.dropout3 = tf.keras.layers.Dropout(rate)

	def call(self, x, enc_output, training,
	         look_ahead_mask, padding_mask):
		# enc_output.shape == (batch_size, input_seq_len, d_model)

		attn1, attn_weights_block1 = self.mha1(x, x, x, look_ahead_mask)  # (batch_size, target_seq_len, d_model)
		attn1 = self.dropout1(attn1, training=training)
		out1 = self.layernorm1(attn1 + x)

		attn2, attn_weights_block2 = self.mha2(
			enc_output, enc_output, out1, padding_mask)  # (batch_size, target_seq_len, d_model)
		attn2 = self.dropout2(attn2, training=training)
		out2 = self.layernorm2(attn2 + out1)  # (batch_size, target_seq_len, d_model)

		ffn_output = self.ffn(out2)  # (batch_size, target_seq_len, d_model)
		ffn_output = self.dropout3(ffn_output, training=training)
		out3 = self.layernorm3(ffn_output + out2)  # (batch_size, target_seq_len, d_model)

		return out3, attn_weights_block1, attn_weights_block2


class Encoder(tf.keras.layers.Layer):
	def __init__(self, num_layers, d_model, num_heads, dff, input_vocab_size,
	             rate=0.1):
		super(Encoder, self).__init__()

		self.d_model = d_model
		self.num_layers = num_layers

		self.dense1 = tf.keras.layers.Dense(d_model + (1280-d_model) // 2, activation=ACTIVATION,
		                                       kernel_initializer=KERNEL_INITIALIZER)
		self.embedding = tf.keras.layers.Dense(d_model, activation=ACTIVATION,
		                                       kernel_initializer=KERNEL_INITIALIZER)  # TODO: this might be able to be improved
		self.pos_encoding = positional_encoding(input_vocab_size, self.d_model)

		self.enc_layers = [EncoderLayer(d_model, num_heads, dff, rate)
		                   for _ in range(num_layers)]

		self.dropout1 = tf.keras.layers.Dropout(rate)
		self.dropout2 = tf.keras.layers.Dropout(rate)
		self.dropout3 = tf.keras.layers.Dropout(rate)

	def call(self, x, training, mask):
		seq_len = tf.shape(x)[1]

		# adding embedding and position encoding.
		x = self.dense1(x)
		x = self.dropout1(x, training=training)
		x = self.embedding(x)  # (batch_size, input_seq_len, d_model)
		x = self.dropout2(x, training=training)

		x *= tf.math.sqrt(self.d_model)
		x += self.pos_encoding[:, :seq_len, :]

		x = self.dropout3(x, training=training)

		for i in range(self.num_layers):
			x = self.enc_layers[i](x, training, mask)

		return x  # (batch_size, input_seq_len, d_model)


class Decoder(tf.keras.layers.Layer):
	def __init__(self, num_layers, d_model, num_heads, dff, target_vocab_size,
	             rate=0.1, max_position=0):
		super(Decoder, self).__init__()

		self.d_model = d_model
		self.num_layers = num_layers

		self.embedding = tf.keras.layers.Embedding(target_vocab_size, d_model)
		self.pos_encoding = raw_positional_encoding(MAX_SEQ_LEN + max_position, d_model)

		self.dec_layers = [DecoderLayer(d_model, num_heads, dff, rate)
		                   for _ in range(num_layers)]
		self.dropout = tf.keras.layers.Dropout(rate)

	def call(self, x, enc_output, training,
	         look_ahead_mask, padding_mask, decode_pos):
		seq_len = tf.shape(x)[1]
		attention_weights = {}

		x = self.embedding(x)  # (batch_size, target_seq_len, d_model)
		x *= tf.math.sqrt(tf.cast(self.d_model, tf.float32))

		if decode_pos is None:
			x += self.pos_encoding[np.newaxis, :seq_len, :]
		else:
			x += tf.map_fn(lambda pos_x: self.pos_encoding[pos_x:seq_len+pos_x, :], decode_pos, dtype=tf.float32)

		x = self.dropout(x, training=training)

		for i in range(self.num_layers):
			x, block1, block2 = self.dec_layers[i](x, enc_output, training,
			                                       look_ahead_mask, padding_mask)

			attention_weights['decoder_layer{}_block1'.format(i + 1)] = block1
			attention_weights['decoder_layer{}_block2'.format(i + 1)] = block2

		# x.shape == (batch_size, target_seq_len, d_model)
		return x, attention_weights


class Transformer(tf.keras.Model):
	def __init__(self, num_layers, d_model, num_heads, dff, input_vocab_size,
	             target_vocab_size, rate=0.1, max_position=0):
		super(Transformer, self).__init__()

		# preprocessing base model
		self.preprocessing_base = tf.keras.applications.mobilenet_v2.MobileNetV2(include_top=False, weights=None)  # preprocessing with MobileNet V2

		# input
		self.preprocessing_base_input = self.preprocessing_base.input

		# network
		self.preprocessing_base_first_hidden_layer = self.preprocessing_base.layers[0].output
		self.preprocessing_base_final_hidden_layer = self.preprocessing_base.layers[-1].output
		self.reshape = tf.keras.layers.Reshape((49, 1280), input_shape=(7, 7, 1280))
		self.preprocessing = self.reshape(self.preprocessing_base_final_hidden_layer)

		self.encoder = Encoder(num_layers, d_model, num_heads, dff,
		                       input_vocab_size, rate)

		self.decoder = Decoder(num_layers, d_model, num_heads, dff,
		                       target_vocab_size, rate, max_position)

		self.final_layer = tf.keras.layers.Dense(target_vocab_size)

	def call(self, inp, tar, training, look_ahead_mask, decode_pos):
		if training:  # IMPORTANT: if training, then preprocess the image multiple time (because of the sequence length), otherwise please preprocess the image before calling this Transformer model
			preprocessing_output = self.preprocessing_base(inp)
			inp = self.reshape(preprocessing_output)

		enc_output = self.encoder(inp, training, None)  # (batch_size, inp_seq_len, d_model)

		# dec_output.shape == (batch_size, tar_seq_len, d_model)
		dec_output, attention_weights = self.decoder(
			tar, enc_output, training, look_ahead_mask, None, decode_pos)

		final_output = self.final_layer(dec_output)  # (batch_size, tar_seq_len, target_vocab_size)

		return final_output, attention_weights


class Pipeline():
	"""
	The main class that runs shit
	"""
	def __init__(self, tokenizer_filename, additional_filename, checkpoint_path):
		# load tokenizer
		self.tokenizer = load_tokenizer_from_path(tokenizer_filename)

		# load additional info
		additional_info = load_additional_info(additional_filename)
		self.max_position = additional_info["max_pos"]

		target_vocab_size = len(self.tokenizer.index_word)  # the total length of index
		input_vocab_size = 1280  # the input vocab size is the last dimension from MobileNet V2

		# instance of Transformer
		self.transformer = Transformer(num_layers, d_model, num_heads, dff,
		                          input_vocab_size, target_vocab_size, DROPOUT_RATE, self.max_position)

		# model
		self.preprocessing_model = tf.keras.Model(self.transformer.preprocessing_base_input, self.transformer.preprocessing)

		# define optimizer and loss
		learning_rate = CustomSchedule(d_model)
		self.optimizer = tf.keras.optimizers.Adam(learning_rate, beta_1=0.9, beta_2=0.98,
		                                     epsilon=1e-9)
		self.loss_object = tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True, reduction='none')

		# define train loss and accuracy
		self.train_loss = tf.keras.metrics.Mean(name='train_loss')
		self.train_accuracy = tf.keras.metrics.SparseCategoricalAccuracy(name='train_accuracy')

		# checkpoint
		self.ckpt = tf.train.Checkpoint(transformer=self.transformer,
		                           optimizer=self.optimizer)

		self.ckpt_manager = tf.train.CheckpointManager(self.ckpt, checkpoint_path, max_to_keep=5)

		# if a checkpoint exists, restore the latest checkpoint.
		if self.ckpt_manager.latest_checkpoint:
			self.ckpt.restore(self.ckpt_manager.latest_checkpoint)
			print('Latest checkpoint restored!!')


	def loss(self, real, pred):
		mask = tf.math.logical_not(tf.math.equal(real, 0))
		loss_ = self.loss_object(real, pred)

		mask = tf.cast(mask, dtype=loss_.dtype)
		loss_ *= mask

		return tf.reduce_mean(loss_)

	# The @tf.function trace-compiles train_step into a TF graph for faster
	# execution. The function specializes to the precise shape of the argument
	# tensors. To avoid re-tracing due to the variable sequence lengths or variable
	# batch sizes (the last batch is smaller), use input_signature to specify
	# more generic shapes.
	@tf.function
	def train_step(self, img, sxn_token, decode_pos):
		tar_inp = sxn_token[:, :-1]
		tar_real = sxn_token[:, 1:]

		look_ahead_mask = create_look_ahead_mask(tf.shape(tar_inp)[1])

		with tf.GradientTape() as tape:
			predictions, _ = self.transformer(img, tar_inp,
			                             True,
			                             look_ahead_mask,
			                             decode_pos)
			loss = self.loss(tar_real, predictions)

		gradients = tape.gradient(loss, self.transformer.trainable_variables)
		self.optimizer.apply_gradients(zip(gradients, self.transformer.trainable_variables))

		self.train_loss(loss)
		self.train_accuracy(tar_real, predictions)


	def evaluate(self, img):
		"""

		:param img: (height, width, 3)
		:return:
		"""
		start_token = self.tokenizer.word_index['<start>']
		end_token = self.tokenizer.word_index['<end>']

		# preprocessing
		img_expand_dims = tf.expand_dims(img, 0)
		encoder_input = self.preprocessing_model(img_expand_dims)  # preprocessing_model needs to come in batch

		# as the target is english, the first word to the transformer should be the
		# english start token.
		decoder_input = [start_token]
		output = tf.expand_dims(decoder_input, 0)

		for i in range(MAX_SEQ_LEN + self.max_position):
			look_ahead_mask = create_look_ahead_mask(tf.shape(output)[1])

			# predictions.shape == (batch_size, seq_len, vocab_size)
			predictions, attention_weights = self.transformer(encoder_input,
			                                             output,
			                                             False,
			                                             look_ahead_mask,
			                                             None)

			# select the last word from the seq_len dimension
			predictions = predictions[:, -1:, :]  # (batch_size, 1, vocab_size)

			predicted_id = tf.cast(tf.argmax(predictions, axis=-1), tf.int32)

			# return the result if the predicted_id is equal to the end token
			if tf.squeeze(predicted_id) == end_token:
				return tf.squeeze(output, axis=0), attention_weights

			# concatentate the predicted_id to the output which is given to the decoder
			# as its input.
			output = tf.concat([output, predicted_id], axis=-1)

		return tf.squeeze(output, axis=0), attention_weights


	def plot_attention_weights(self, attention, input, sxn_token, layer, filename, max_len=5):
		"""

		:param max_len: maximum length for sequence of input and sxn_result. Keep this to small value
		:param attention:
		:param input: (49)
		:param result: sxn token (seq_len_of_sxn_token)
		:param layer:
		:return:
		"""
		fig = plt.figure(figsize=(16, 8))

		attention = tf.squeeze(attention[layer], axis=0)

		# Truncate length to max_len
		attention = tf.slice(attention, [0, 0, 0], [-1, max_len, max_len])  # slice the tensor
		input = input[:max_len]
		sxn_token = sxn_token[:max_len]

		# temp var
		row = math.ceil(attention.shape[0] ** .5)

		for head in range(attention.shape[0]):
			ax = fig.add_subplot(row, row, head + 1)

			# plot the attention weights
			ax.matshow(attention[head][:-1, :], cmap='viridis')

			fontdict = {'fontsize': 10}

			ax.set_xticks(range(len(input)))
			ax.set_yticks(range(len(sxn_token)))

			ax.set_ylim(len(sxn_token) - 1.5, -0.5)

			ax.set_xticklabels(
				list(map(str, input)),
				fontdict=fontdict, rotation=90)

			ax.set_yticklabels(
				list(map(lambda i: self.tokenizer.index_word[i], sxn_token)),
				fontdict=fontdict)

			ax.set_xlabel('Head {}'.format(head + 1))

		plt.tight_layout()
		plt.savefig(filename)
		plt.close()


	def translate(self, img, plot=''):
		"""

		:param img: (height, width, 3)
		:param plot:
		:return:
		"""
		result, attention_weights = self.evaluate(img)

		# [1:] key is to remove the <start> token
		result = result.numpy()[1:]

		predicted_sxn = self.tokenizer.sequences_to_texts([result])[0]  # translate to predicted_sxn
		predicted_html = decode_2_html(predicted_sxn)  # translate to predicted html

		# print img evaluation
		plt.imshow(img)
		plt.savefig('evaluate_transformer_input_img.png', bbox_inches='tight')
		plt.close()

		print('Predicted html: {}'.format(predicted_html))

		if plot:
			self.plot_attention_weights(attention_weights, [i for i in range(49)], result, plot, "layers_figure/transformer/last_attention_weights.png")
			print("Plot attention weight is generated.")

		return predicted_html

### Main training loop
if __name__ == "__main__":
	checkpoint_path = "./checkpoints/train/transformer"

	# initialize train dataset
	train_dataset = get_all_datasets(TFRECORD_FILENAME)  # TODO: seperate training and evaluation

	master = Pipeline(TOKENIZER_FILENAME, ADDITIONAL_FILENAME, checkpoint_path)  # master pipeline

	if IS_TRAINING:
		### Train loop
		start_epoch = 0
		if master.ckpt_manager.latest_checkpoint:
			start_epoch = int(master.ckpt_manager.latest_checkpoint.split('-')[-1]) * 5

		# load MobileNetV2 weight if epoch is equal to 0
		if start_epoch == 0:
			print('Loading MobileNetV2 weights for epoch {}'.format(start_epoch + 1))
			master.transformer.preprocessing_base.load_weights(MOBILENETV2_WEIGHT_PATH)

		for epoch in range(start_epoch, EPOCHS):
			start = time.time()

			master.train_loss.reset_states()
			master.train_accuracy.reset_states()

			# inp -> image, tar -> html
			for (batch, (img, sxn_token, decode_pos)) in enumerate(train_dataset):
				master.train_step(img, sxn_token, decode_pos)

				if batch % 100 == 0:
					print('Epoch {} Batch {} Loss {:.4f} Accuracy {:.4f}'.format(
						epoch + 1, batch, master.train_loss.result(), master.train_accuracy.result()))

			if (epoch + 1) % 5 == 0:
				ckpt_save_path = master.ckpt_manager.save()
				print('Saving checkpoint for epoch {} at {}'.format(epoch + 1,
				                                                    ckpt_save_path))

			print('Epoch {} Loss {:.4f} Accuracy {:.4f}'.format(epoch + 1,
			                                                    master.train_loss.result(),
			                                                    master.train_accuracy.result()))

			print('Time taken for 1 epoch: {} secs\n'.format(time.time() - start))

	# evaluate
	print ("Start evaluation...")
	eval_dataset = next(iter(train_dataset))  # TODO: this definitely needs to be changed with more proper pipeline

	# translate image to html
	html = master.translate(eval_dataset[0][0], "decoder_layer4_block2")

	# write the html to file
	with open("generated/generated.html", "w") as f:
		f.write(html)