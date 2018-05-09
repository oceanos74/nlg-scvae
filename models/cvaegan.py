import sys
import numpy as np
from sklearn.manifold import TSNE
import matplotlib.pyplot as plt
import torch
import torch.nn as nn
from torch.autograd import Variable
import torch.nn.functional as F

from .layers.encoder import Encoder
from .layers.decoder import Decoder
from utils.util import sample_gaussian
from models.masked_cross_entropy import *
USE_CUDA = True

class CVAEGAN(nn.Module):
	def __init__(self, dec_type, hidden_size, vocab_size, latent_size, d_size, da_size, sv_size, std, n_layers=1, dropout=0.5, word_dropout=0.0, use_prior=False, lr=0.001, D_lr=0.002, G_lr=0.002, overgen=1):
		super(CVAEGAN, self).__init__()
		self.dec_type = dec_type
		self.hidden_size = hidden_size
		self.vocab_size = vocab_size
		self.latent_size = latent_size
		self.d_size = d_size
		self.n_layers = n_layers
		self.use_prior = use_prior
		self.word_dropout = word_dropout
		self.std = std

		# model
		self.enc = Encoder( vocab_size, hidden_size, n_layers, dropout=dropout )
		self.dec = Decoder(dec_type, hidden_size, vocab_size, d_size=d_size, dropout=dropout)

		# cond feat transform
		self.feat = nn.Linear(latent_size, d_size)

		# recognition network
		self.recog = nn.Linear(hidden_size*n_layers*2+d_size, latent_size*2) # first 2 for bi-directional encoder, second 2 for mean and logvar

		# prior network
		self.fc = nn.Linear(d_size, latent_size*2)
		self.prior = nn.Linear(latent_size*2, latent_size*2)
#		self.prior = nn.Linear(d_size, latent_size*2)

		# da/slot network
		self.pred_da = nn.Linear(latent_size, da_size)
		self.pred_sv = nn.Linear(latent_size, sv_size)

		# linear transform from cat(c, z) to s0 to decoder
#		self.linears = [nn.Linear(cond_len+latent_size, hidden_size).cuda() for _ in range(n_layers)]
		# note: has to manually move layers in the list to .cuda for some reasons

#		self.z2init = nn.Linear(latent_size+d_size, hidden_size)

		# Discriminator
		self.D1 = nn.Linear(hidden_size*n_layers*2, 1)
#		self.D1 = nn.Linear(hidden_size*n_layers*2, hidden_size)
#		self.D2 = nn.Linear(hidden_size, 1)

		self.global_t = 0
#		self.random_sample = False
		self.random_sample = False if overgen == 1 else True

		self.set_solver(lr, D_lr, G_lr)
		self.criterion = {'xent': torch.nn.CrossEntropyLoss(), 'multilabel': torch.nn.MultiLabelSoftMarginLoss()}

		# t-sne
		self.do_idx2feat = {}
		self.da_idx2feat = {}
		for i in range(4):
			self.do_idx2feat[i] = [[], []]
		for i in range(da_size):
			self.da_idx2feat[i] = [[], []]


	def plot_z(self):
		color = ['b', 'g', 'r', 'y', 'm', 'c', 'k']
#		shape = ['.', 'x']
		
		for do_idx in self.do_idx2feat:
			x = np.array(self.do_idx2feat[do_idx][0])
			y = np.array(self.do_idx2feat[do_idx][1])
			plt.plot(x, y, color[do_idx] + '.')
			print('do {}: {}'.format(do_idx, len(x)))
#		plt.show() # didnt work
		plt.savefig('./z_png/do8.png')
		plt.gcf().clear()

#		for da_idx in self.da_idx2feat:
		for c_idx, da_idx in enumerate([7, 13, 9, 6, 3, 11, 12]): # plot top 7 freq da
#			if da_idx == 14:
#				break
#			if da_idx == 7:
#				plt.savefig('./z_png/da5-1.png')
#				plt.gcf().clear()

			x = np.array(self.da_idx2feat[da_idx][0])
			y = np.array(self.da_idx2feat[da_idx][1])
			print('da {}: {}'.format(da_idx, len(x)))
			plt.plot(x, y, color[c_idx] + 'x')

#			plt.plot(x, y, color[int(da_idx/4)] + shape[da_idx%4] )
#			plt.plot(x, y, color[int(da_idx%7)] + shape[int(da_idx/7)] )
#		plt.show()
#		plt.savefig('./z_png/da5-2.png')
		plt.savefig('./z_png/da8.png')
		plt.gcf().clear()
			

#	def dim_reduce(self, do_indexes, da_indexes):
	def dim_reduce(self, do_indexes, da_indexes, pca):
		batch_size = self.z.size(0)
#		print('bs:', batch_size)
#		self.z_reduce = TSNE(n_components=2).fit_transform(self.z.cpu().data.numpy()) # (batch_size, 2)
#		self.z_reduce = TSNE(n_components=2).fit_transform(self.recog_mu.cpu().data.numpy()) # (batch_size, 2)
#		self.z_reduce = pca.fit_transform(self.recog_mu.cpu().data.numpy()) # works better
		self.z_reduce = pca.fit_transform(self.prior_mu.cpu().data.numpy()) # works better

		assert self.z_reduce.shape == (batch_size, 2)
		for b in range(batch_size):
			do_idx, da_idx = do_indexes[b], da_indexes[b]
			x, y = self.z_reduce[b][0], self.z_reduce[b][1]

			self.do_idx2feat[do_idx][0].append(x)
			self.do_idx2feat[do_idx][1].append(y)

			self.da_idx2feat[da_idx][0].append(x)
			self.da_idx2feat[da_idx][1].append(y)


	def set_prior(self, use_prior):
		self.use_prior = use_prior


	def set_solver(self, lr, D_lr, G_lr):
		self.params = [{'params': self.enc.parameters()}, {'params': self.dec.parameters()}, \
				{'params': self.feat.parameters()}, {'params': self.recog.parameters()}, \
				{'params': self.fc.parameters()}, {'params': self.prior.parameters()}, \
				{'params': self.pred_da.parameters()}, {'params': self.pred_sv.parameters()}] #, \
#				{'params': self.z2init.parameters()}]

		self.D_params = [{'params': self.D1.parameters()}]#, {'params': self.D2.parameters()}]
		self.G_params = [{'params': self.dec.parameters()}, {'params': self.fc.parameters()}, {'params': self.prior.parameters()}]

		# TODO: learning for those three components
		self.solver = torch.optim.Adam(self.params, lr=lr)
		self.D_solver = torch.optim.Adam(self.D_params, lr=D_lr)
		self.G_solver = torch.optim.Adam(self.G_params, lr=G_lr)


	def gaussian_kld(self):
		kld = -0.5 * torch.sum(1 + (self.recog_logvar - self.prior_logvar) 
									- torch.pow(self.prior_mu - self.recog_mu, 2) / torch.exp(self.prior_logvar)
									- torch.exp(self.recog_logvar) / torch.exp(self.prior_logvar), dim=1)
		return kld


	def get_G_loss(self, ones_label):
		self.G_loss = F.binary_cross_entropy(self.D_fake, ones_label)
		return self.G_loss


	def get_D_loss(self, ones_label, zeros_label):
		D_loss_real = F.binary_cross_entropy(self.D_real, ones_label)
		D_loss_fake = F.binary_cross_entropy(self.D_fake, zeros_label)
		self.D_loss = D_loss_real + D_loss_fake
		return self.D_loss


	def get_loss(self, target_label, target_lengths, full_kl_step, da_label, sv_label):
		rc_loss = masked_cross_entropy(
			self.output_all.contiguous(), # -> batch x seq
			target_label.contiguous(), # -> batch x seq
			target_lengths)

		# kl cost annealing
		kl_weight = min(self.global_t/full_kl_step, 1.0)
		kl_loss = torch.mean(self.gaussian_kld())

		# da/slots loss
		da_loss = self.criterion['xent'](self.da_output, da_label)
		sv_loss = self.criterion['multilabel'](self.sv_output, sv_label)

		self.loss = rc_loss + kl_weight * kl_loss + da_loss + sv_loss
		return {'rc': rc_loss, 'kl': kl_loss, 'da': da_loss, 'sv': sv_loss}, kl_weight


	def update(self, clip): # update cvae
		# Back prop
		self.loss.backward()

		# Clip gradient norms
		for p in self.params:
			_ = torch.nn.utils.clip_grad_norm(p['params'], clip)

		# Update
		self.solver.step()

		# Zero grad
		self.solver.zero_grad()


	def update_D(self, clip): # update D
		self.D_loss.backward()
		for p in self.D_params:
			_ = torch.nn.utils.clip_grad_norm(p['params'], clip)
		self.D_solver.step()
		self.D_solver.zero_grad()


	def update_G(self, clip): # update G
		self.G_loss.backward()
		for p in self.G_params:
			_ = torch.nn.utils.clip_grad_norm(p['params'], clip)
		self.G_solver.step()
		self.G_solver.zero_grad()


#	def linear_weights_init(self, l):
#		for m in l.parameters():
#			m.data.normal_(0.0, 0.1)


#	def dropout_on_word(self, decoder_input, dataset, batch_size):
#		decoder_input = decoder_input.view(batch_size, self.vocab_size)
#		unk = []
#		for _ in range(batch_size):
#			hot = [0]*self.vocab_size
#			hot[dataset.word2index['UNK_token']] = 1.0
#			unk.append(hot)
#		unk = np.array(unk) # (batch_size, vocab_size)
##		unk = np.resize(np.array([dataset.word2index['UNK_token']]*batch_size), (batch_size, 1))
#		condition = np.random.uniform(size=(batch_size, 1)) < self.word_dropout
#		res = np.where(condition, unk, decoder_input.data.cpu().numpy())
#		res = Variable(torch.from_numpy(res.astype(np.float32))).view(batch_size, 1, self.vocab_size)
#		if USE_CUDA:
#			res = res.cuda()
#		return res


	def pass_D(self, all_decoder_outputs, decoded_words):
		'''
		when preparing input for enc from generated sentences, we need to count len of each example but no need to pad zero
		'''
		input_seq, input_lengths = self.get_enc_input(all_decoder_outputs, decoded_words)

		# Run words through encoder
		_, encoder_hidden = self.enc(input_seq, input_lengths) # (n_layers*n_directions, batch_size, hidden_size)
		l = torch.split(encoder_hidden, 1, dim=0) # a list of tensor (1, batch_size, hidden_size) with len=n_layers*n_directions
		encoder_hidden = torch.cat(l, dim=2).squeeze() # (batch_size, hidden_size*n_layers*n_directions)

		# Get D Loss
#		self.D_fake = F.sigmoid(self.D2(F.relu(self.D1(encoder_hidden))))
		self.D_fake = F.sigmoid(self.D1(encoder_hidden))
		

	def forward(self, input_seq, input_lengths, target_seq, target_lengths, conds_seq, dataset, gen=False):
		'''
		conds_seq: (batch_size, feat_size)
		'''
#		self.global_t += 1
		batch_size = input_seq.size(0)
		max_len_enc = input_seq.size(1)

		# Run words through encoder
		_, encoder_hidden = self.enc(input_seq, input_lengths) # (n_layers*n_directions, batch_size, hidden_size)
		l = torch.split(encoder_hidden, 1, dim=0) # a list of tensor (1, batch_size, hidden_size) with len=n_layers*n_directions
		encoder_hidden = torch.cat(l, dim=2).squeeze() # (batch_size, hidden_size*n_layers*n_directions)

		# Get D Loss
#		self.D_real = F.sigmoid(self.D2(F.relu(self.D1(encoder_hidden))))
		self.D_real = F.sigmoid(self.D1(encoder_hidden))

		# recognition network
		recog_input = torch.cat((encoder_hidden, conds_seq), dim=1)
		recog_mulogvar = self.recog(recog_input) # (batch_size, latent_size*2)
		self.recog_mu, self.recog_logvar = torch.split(recog_mulogvar, self.latent_size, dim=1)

		# prior network
		prior_fc = F.tanh(self.fc(conds_seq))
		prior_mulogvar = self.prior(prior_fc)
#		prior_mulogvar = self.prior(conds_seq)
		self.prior_mu, self.prior_logvar = torch.split(prior_mulogvar, self.latent_size, dim=1)

		# draw latent sample
		z = sample_gaussian(self.prior_mu, self.prior_logvar, self.std) if self.use_prior else sample_gaussian(self.recog_mu, self.recog_logvar, self.std) # (batch_size, latent_size)
		self.z = z
#		if self.use_prior:
#			print('Using prior')
#			z = sample_gaussian(prior_mu, prior_logvar)
#			z = Variable(torch.zeros((batch_size, self.hidden_size))).cuda()
#		else:
#			print('Using posterior')
#			z = sample_gaussian(recog_mu, recog_logvar)
#			z = Variable(torch.zeros((batch_size, self.hidden_size))).cuda()

		# predict da/slots
		self.da_output = self.pred_da(z)
		self.sv_output = self.pred_sv(z)

		# prepare decoder s0 = wi*[c,z]+bi
#		last_hidden = self.z2init(torch.cat((z, conds_seq), dim=1)) # (batch_size, hidden_size)
		last_hidden = z

		# decoder
		if self.dec_type == 'sclstm':
			self.output_all, decoded_words = self.dec(target_seq, dataset, last_hidden=last_hidden, last_dt=conds_seq, gen=gen, random_sample=self.random_sample)
		else:
			self.output_all, decoded_words = self.dec(input_seq, dataset, last_hidden=last_hidden, gen=gen, random_sample=self.random_sample)

		return self.output_all, decoded_words

		# TODO: n_layers > 1
#		decoder_hidden = []
#		for i in range(self.n_layers):
#			decoder_hidden.append(self.linears[i](init_input).unsqueeze(0)) # (1, batch_size, hidden_size)
#		decoder_hidden = torch.cat(decoder_hidden, dim=0) # (n_layers, batch_size, hidden_size)
#		decoder_hidden = z.view(1, batch_size, self.hidden_size) # need to fix for n_layers > 1

#		# word dropout
#		if self.word_dropout > 0 and self.global_t > 500:
#			decoder_input = self.dropout_on_word(decoder_input, dataset, batch_size)

#		return [recog_mu, recog_logvar, prior_mu, prior_logvar], [da_output, sv_output], all_decoder_outputs, decoded_words


	def get_enc_input(self, all_decoder_outputs, decoded_words):
		'''
		* detect first EOS in decoded words, and take words before it *
		Input:
			all_decoder_outputs: (batch_size, max_len_old, vocab_size)
			decoded_words: (batch_size)
		Output:
			lengths: (batch_size)
		'''
		all_decoder_outputs = F.softmax(all_decoder_outputs, dim=2)
		lengths = [ len(s.split()) for s in decoded_words ]
		lengths = [ _len if _len!= 0 else 1 for _len in lengths] # 0 length cuases error in rnn

		# sort batch
		split = torch.split(all_decoder_outputs, 1, dim=0)
		pairs = sorted(zip(split, lengths, decoded_words), key=lambda p:p[1], reverse=True)
		all_decoder_outputs, lengths, decoded_words = zip(*pairs)
		all_decoder_outputs = torch.cat(all_decoder_outputs, dim=0) # a tensor

		max_len = max(lengths)
#		print('max_len', max_len)
		return Variable(all_decoder_outputs.cpu().data[:, :max_len, :]).cuda(), list(lengths)