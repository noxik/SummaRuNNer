#!/usr/bin/env python3

import json
import models
import utils
import argparse,random,logging,numpy,os
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from torch.autograd import Variable
from torch.utils.data import DataLoader
from torch.nn.utils import clip_grad_norm
from time import time
from tqdm import tqdm

logging.basicConfig(level=logging.INFO, format='%(asctime)s [INFO] %(message)s')
parser = argparse.ArgumentParser(description='extractive summary')
# model
parser.add_argument('-save_dir',type=str,default='checkpoints/')
parser.add_argument('-embed_dim',type=int,default=100)
parser.add_argument('-embed_num',type=int,default=100)
parser.add_argument('-pos_dim',type=int,default=50)
parser.add_argument('-pos_num',type=int,default=100)
parser.add_argument('-seg_num',type=int,default=10)
parser.add_argument('-kernel_num',type=int,default=100)
parser.add_argument('-kernel_sizes',type=str,default='3,4,5')
parser.add_argument('-model',type=str,default='RNN_RNN')
parser.add_argument('-hidden_size',type=int,default=200)
# train
parser.add_argument('-logfile', type=str)
parser.add_argument('-lr',type=float,default=1e-3)
parser.add_argument('-batch_size',type=int,default=32)
parser.add_argument('-epochs',type=int,default=5)
parser.add_argument('-seed',type=int,default=1)
parser.add_argument('-train_dir',type=str,default='data/train.json')
parser.add_argument('-val_dir',type=str,default='data/val.json')
parser.add_argument('-embedding',type=str,default='data/embedding.npz')
parser.add_argument('-word2id',type=str,default='data/word2id.json')
parser.add_argument('-report_every',type=int,default=1500)
parser.add_argument('-seq_trunc',type=int,default=50)
parser.add_argument('-max_norm',type=float,default=1.0)
# test
parser.add_argument('-load_dir',type=str,default='checkpoints/RNN_RNN_seed_1.pt')
parser.add_argument('-test_dir',type=str,default='data/test.json')
parser.add_argument('-ref',type=str,default='outputs/ref')
parser.add_argument('-hyp',type=str,default='outputs/hyp')
parser.add_argument('-topk',type=int,default=3)
# device
parser.add_argument('-device',type=int)
# option
parser.add_argument('-test',action='store_true')
parser.add_argument('-debug',action='store_true')
parser.add_argument('-predict',action='store_true')
args = parser.parse_args()
use_gpu = args.device is not None

if torch.cuda.is_available() and not use_gpu:
    print("WARNING: You have a CUDA device, should run with -device 0")

# set cuda device and seed
if use_gpu:
    torch.cuda.set_device(args.device)
torch.cuda.manual_seed(args.seed)
torch.manual_seed(args.seed)
random.seed(args.seed)
numpy.random.seed(args.seed) 
    
def eval(net,vocab,data_iter,criterion):
    # function calculates aggregate loss on validation set
    net.eval()
    total_loss = 0
    batch_num = 0
    for batch in data_iter:
        features,targets,_,doc_lens = vocab.make_features(batch)
        features,targets = Variable(features), Variable(targets.float())
        if use_gpu:
            features = features.cuda()
            targets = targets.cuda()
        probs = net(features,doc_lens)
        loss = criterion(probs,targets)
        total_loss += loss.data.item()
        batch_num += 1
    loss = total_loss / batch_num
    net.train()
    return loss

def train():
    logging.info('Loading vocab,train and val dataset.Wait a second,please')
    # load word embeddings
    embed = torch.Tensor(np.load(args.embedding)['embedding'])
    # load word2id dictionary
    with open(args.word2id) as f:
        word2id = json.load(f)
    vocab = utils.Vocab(embed, word2id)

    # load train dataset
    with open(args.train_dir) as f:
        examples = [json.loads(line) for line in f]
    train_dataset = utils.Dataset(examples)

    # load validation dataset
    with open(args.val_dir) as f:
        examples = [json.loads(line) for line in f]
    val_dataset = utils.Dataset(examples)

    logbatch = logepoch = None
    if args.logfile:
        logbatch = open(args.logfile + '.log', 'w', buffering=1)
        logepoch = open(args.logfile + '.2.log', 'w', buffering=1)

    # update args
    args.embed_num = embed.size(0)
    args.embed_dim = embed.size(1)
    args.kernel_sizes = [int(ks) for ks in args.kernel_sizes.split(',')]
    # instantiate model
    net = getattr(models,args.model)(args,embed)
    if use_gpu:
        net.cuda()
    # instantiate dataset batchers
    train_iter = DataLoader(dataset=train_dataset,
            batch_size=args.batch_size,
            shuffle=True)
    val_iter = DataLoader(dataset=val_dataset,
            batch_size=args.batch_size,
            shuffle=False)
    # loss function
    criterion = nn.BCELoss()
    # model info
    print(net)
    params = sum(p.numel() for p in list(net.parameters())) / 1e6
    print('#Params: %.1fM' % (params))
    
    min_loss = float('inf')
    optimizer = torch.optim.Adam(net.parameters(),lr=args.lr)
    net.train()
    
    t1 = time() 
    for epoch in range(1,args.epochs+1):
        for i,batch in tqdm(enumerate(train_iter)):
            features,targets,_,doc_lens = vocab.make_features(batch)
            features,targets = Variable(features), Variable(targets.float())
            if use_gpu:
                features = features.cuda()
                targets = targets.cuda()
            # make forward propogation
            probs = net(features,doc_lens)
            # calculate loss
            loss = criterion(probs,targets)
            # clear gradients
            optimizer.zero_grad()
            # back propogation
            loss.backward()
            # clip the gradient
            clip_grad_norm(net.parameters(), args.max_norm)
            # perform a single optimization step
            optimizer.step()
            if args.debug:
                if logbatch:
                    logbatch.write('{}:{}\n'.format(i, loss.data.item()))
                print('Batch ID:%d Loss:%f' %(i,loss.data.item()))
            if i % args.report_every == 0:
                cur_loss = eval(net,vocab,val_iter,criterion)
                if cur_loss < min_loss:
                    min_loss = cur_loss
                    best_path = net.save()
                if logepoch:
                    logepoch.write('{}:{}:{}\n'.format(epoch, min_loss, cur_loss))
                logging.info('Epoch: %2d Min_Val_Loss: %f Cur_Val_Loss: %f' % (epoch,min_loss,cur_loss))
    t2 = time()
    logging.info('Total time:%f h'%((t2-t1)/3600))
    if args.logfile:
        logbatch.close()
        logepoch.close()

def test():
    # load word embeddings
    embed = torch.Tensor(np.load(args.embedding)['embedding'])
    # load word2id dictionary
    with open(args.word2id) as f:
        word2id = json.load(f)
    vocab = utils.Vocab(embed, word2id)

    # load test dataset
    with open(args.test_dir) as f:
        examples = [json.loads(line) for line in f]
    test_dataset = utils.Dataset(examples)

    # instantiate batcher
    test_iter = DataLoader(dataset=test_dataset,
                            batch_size=args.batch_size,
                            shuffle=False)
    if use_gpu:
        checkpoint = torch.load(args.load_dir, map_location='cuda:0')
    else:
        checkpoint = torch.load(args.load_dir, map_location=lambda storage, loc: storage)

    # checkpoint['args']['device'] saves the device used as train time
    # if at test time, we are using a CPU, we must override device to None
    if not use_gpu:
        checkpoint['args'].device = None
    # load the model and instantiate it
    net = getattr(models,checkpoint['args'].model)(checkpoint['args'])
    # load pretrained states
    net.load_state_dict(checkpoint['model'])
    if use_gpu:
        net.cuda()
    net.eval()
    
    doc_num = len(test_dataset)
    time_cost = 0
    file_id = 1
    for batch in tqdm(test_iter):
        features,_,summaries,doc_lens = vocab.make_features(batch)
        t1 = time()
        # run the model over all the sentences of the batch
        if use_gpu:
            probs = net(Variable(features).cuda(), doc_lens)
        else:
            probs = net(Variable(features), doc_lens)
        # probs: probabilities of all sentences of all the documents in the batch
        t2 = time()
        time_cost += t2 - t1
        start = 0
        for doc_id,doc_len in enumerate(doc_lens):
            stop = start + doc_len  # index of the last sentencse doc with doc_id
            # probabilities of sentences of doc with doc_id
            prob = probs[start:stop] if probs.dim() == 1 else torch.Tensor([probs])
            # how many top sentences to pick ?
            topk = min(args.topk,doc_len)
            # indices of k sentences with highest probabilities
            topk_indices = prob.topk(topk)[1].cpu().data.numpy()
            # sort the indices
            topk_indices.sort()
            # get full doc splitted by sentences
            doc = batch['doc'][doc_id].split('\n')[:doc_len]
            # get topk sentences from the doc
            hyp = [doc[index] for index in topk_indices]
            # get golden summary for the doc
            ref = summaries[doc_id]
            # save machine and golden summary for the doc
            with open(os.path.join(args.ref,str(file_id)+'.txt'), 'w') as f:
                f.write(ref)
            with open(os.path.join(args.hyp,str(file_id)+'.txt'), 'w') as f:
                f.write('\n'.join(hyp))
            start = stop
            file_id = file_id + 1
    print('Speed: %.2f docs / s' % (doc_num / time_cost))
def predict():
    # TODO
    pass
if __name__=='__main__':
    if args.test:
        test()
    else:
        train()
