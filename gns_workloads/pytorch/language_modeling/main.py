# coding: utf-8
import argparse
import time
import math
import os
import torch
import torch.distributed as dist
import torch.nn as nn
import torch.onnx
import socket
import sys
import multiprocessing as mp

from pynvml import *

import data
import model

# TODO: Figure out a cleaner way of including gavel_iterator.
lm_dir = os.path.dirname(os.path.realpath(__file__))
pytorch_dir = os.path.dirname(lm_dir)
workloads_dir = os.path.dirname(pytorch_dir)
gpusched_dir = os.path.dirname(workloads_dir)
scheduler_dir = os.path.join(gpusched_dir, 'scheduler')
sys.path.append(scheduler_dir)
from gavel_iterator import GavelIterator

parser = argparse.ArgumentParser(description='PyTorch Wikitext-2 RNN/LSTM Language Model')
parser.add_argument('--data', type=str, required=True,
                    help='location of the data corpus')
parser.add_argument('--model', type=str, default='LSTM',
                    help='type of recurrent net (RNN_TANH, RNN_RELU, LSTM, GRU)')
parser.add_argument('--emsize', type=int, default=200,
                    help='size of word embeddings')
parser.add_argument('--nhid', type=int, default=200,
                    help='number of hidden units per layer')
parser.add_argument('--nlayers', type=int, default=2,
                    help='number of layers')
parser.add_argument('--lr', type=float, default=20,
                    help='initial learning rate')
parser.add_argument('--clip', type=float, default=0.25,
                    help='gradient clipping')
parser.add_argument('--epochs', type=int, default=None,
                    help='upper epoch limit')
parser.add_argument('--steps', type=int, default=None,
                    help='upper steps limit')
parser.add_argument('--batch_size', type=int, default=20, metavar='N',
                    help='batch size')
parser.add_argument('--bptt', type=int, default=35,
                    help='sequence length')
parser.add_argument('--dropout', type=float, default=0.2,
                    help='dropout applied to layers (0 = no dropout)')
parser.add_argument('--tied', action='store_true',
                    help='tie the word embedding and softmax weights')
parser.add_argument('--seed', type=int, default=1111,
                    help='random seed')
parser.add_argument('--cuda', action='store_true',
                    help='use CUDA')
parser.add_argument('--log-interval', type=int, default=200, metavar='N',
                    help='report interval')
parser.add_argument('--checkpoint_dir', type=str,
                    default='/lfs/1/keshav2/checkpoints',
                    help='Checkpoint dir')
parser.add_argument('--onnx-export', type=str, default='',
                    help='path to export the final model in onnx format')
parser.add_argument('--throughput_estimation_interval', type=int, default=None,
                    help='Steps between logging steps completed')

parser.add_argument('--dist-url', default='env://', type=str,
                            help='url used to set up distributed training')
parser.add_argument('--dist-backend', default='nccl', type=str,
                            help='Distributed backend')
parser.add_argument('--local_rank', default=0, type=int,
                            help='Local rank')
parser.add_argument('--rank', default=None, type=int,
                            help='Rank')
parser.add_argument('--world_size', default=None, type=int,
                            help='World size')
parser.add_argument('--master_addr', default=None, type=str,
                            help='Master address to use for distributed run')
parser.add_argument('--master_port', default=None, type=int,
                            help='Master port to use for distributed run')
parser.add_argument('--max_duration', type=int, default=None,
                    help='Maximum duration in seconds')
parser.add_argument('--enable_gavel_iterator', action='store_true',
                    default=False, help='If set, use Gavel iterator')

args = parser.parse_args()
if args.batch_size > 80:
    args.batch_size = 80
print("batch size at start of model is ", args.batch_size)
torch.cuda.set_device(args.local_rank)

torch.manual_seed(0)
torch.cuda.manual_seed(0)
#random.seed(0)
#np.random.seed(0)
os.environ["PYTHONHASHSEED"] = str(0)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False

start_epoch = 0
new_lr = args.lr
#print("arg lr and new lr is ", args.lr, new_lr)

if args.epochs is not None and args.steps is not None:
    raise ValueError('Only one of epochs and steps may be set')
elif args.epochs is None and args.steps is None:
    raise ValueError('One of epochs and steps must be set')
#print("args dara path is ",args.data)
# Set the random seed manually for reproducibility.
torch.manual_seed(args.seed)
if torch.cuda.is_available():
    if not args.cuda:
        print("WARNING: You have a CUDA device, so you should probably run with --cuda")

device = torch.device("cuda" if args.cuda else "cpu")

###############################################################################
# Load data
###############################################################################

corpus = data.Corpus(args.data)

# Starting from sequential data, batchify arranges the dataset into columns.
# For instance, with the alphabet as the sequence and batch size 4, we'd get
# ┌ a g m s ┐
# │ b h n t │
# │ c i o u │
# │ d j p v │
# │ e k q w │
# └ f l r x ┘.
# These columns are treated as independent by the model, which means that the
# dependence of e. g. 'g' on 'f' can not be learned, but allows more efficient
# batch processing.

class CorpusDataset(torch.utils.data.Dataset):
    def __init__(self, data, batch_size, bptt):
        self._data = data.narrow(0, 0, (data.size(0) // batch_size) * batch_size)
        # Evenly divide the data across the bsz batches.
        self._data = self._data.view(batch_size, -1).t().contiguous().to(device)
        self._data_length = data.size(0)
        self._batch_size = batch_size
        self._bptt = bptt

    # get_input subdivides the source data into chunks of length args.bptt.
    # If source is equal to the example output of the batchify function, with
    # a bptt-limit of 2, we'd get the following two Variables for i = 0:
    # ┌ a g m s ┐ ┌ b h n t ┐
    # └ b h n t ┘ └ c i o u ┘
    # Note that despite the name of the function, the subdivison of data is not
    # done along the batch dimension (i.e. dimension 1), since that was handled
    # by the batchify function. The chunks are along dimension 0, corresponding
    # to the seq_len dimension in the LSTM.
    def get_input(self, row_idx, col_idx):
        row_idx = row_idx % len(self._data)
        seq_len = min(self._bptt, len(self._data) - 1 - row_idx)
        data = self._data[row_idx: row_idx+seq_len, col_idx]
        target = self._data[row_idx+1: row_idx+1+seq_len, col_idx].view(data.size())
        data = torch.cat([data, data.new_zeros(self._bptt - data.size(0))])
        target = torch.cat([target, target.new_zeros(self._bptt - target.size(0))])
        return data, target

    def __len__(self):
        return self._data_length // self._bptt

    def __getitem__(self, idx):
        return self.get_input((idx // self._batch_size) * self._bptt,
                              idx % self._batch_size)

eval_batch_size = 10
train_dataset = CorpusDataset(corpus.train,
                              args.batch_size,
                              args.bptt)
val_dataset = CorpusDataset(corpus.valid,
                            eval_batch_size,
                            args.bptt)
test_dataset = CorpusDataset(corpus.test,
                             eval_batch_size,
                             args.bptt)

###############################################################################
# Handle checkpoints
###############################################################################

def load_checkpoint(args, checkpoint_path):
    try:
        print('Loading checkpoint from %s...' % (checkpoint_path))
        with open(checkpoint_path, 'rb') as f:
            state = torch.load(f, map_location='cuda:{}'.format(args.local_rank))
            return state
    except Exception as e:
        print('Could not load from checkpoint: %s' % (e))
        return None

def save_checkpoint(state, checkpoint_path):
    with open(checkpoint_path, 'wb') as f:
        print('Saving checkpoint at %s...' % (checkpoint_path))
        torch.save(state, f)

###############################################################################
# Build the model
###############################################################################

args.distributed = False
if args.master_addr is not None:
    args.distributed = True
    os.environ['MASTER_ADDR'] = args.master_addr
    os.environ['MASTER_PORT'] = str(args.master_port)
    os.environ['NCCL_DEBUG'] = 'INFO'
    # hostname = socket.gethostname()
    # if "node" in hostname: # wisr cluster
    #     socket_ifname = 'enps0f0'# if hostname != 'node1' else 'enp94s0f0'  # rdma nics
    #     # socket_ifname = 'eno4'
    # else:  # ec2
    #     socket_ifname = "ens3"
    # os.environ['NCCL_SOCKET_IFNAME'] = socket_ifname
    dist.init_process_group(backend=args.dist_backend,
                            init_method=args.dist_url,
                            world_size=args.world_size,
                            rank=args.rank)
if args.distributed:
    train_sampler = torch.utils.data.distributed.DistributedSampler(
        train_dataset, shuffle=False)
else:
    train_sampler = None

ntokens = len(corpus.dictionary)

train_loader = torch.utils.data.DataLoader(train_dataset,
                                           batch_size=args.batch_size,
                                           shuffle=False,
                                           sampler=train_sampler,
                                           drop_last=True)
val_loader = torch.utils.data.DataLoader(val_dataset,
                                         batch_size=eval_batch_size,
                                         shuffle=False,
                                         drop_last=True)
test_loader = torch.utils.data.DataLoader(test_dataset,
                                          batch_size=eval_batch_size,
                                          shuffle=False,
                                          drop_last=True)

if args.enable_gavel_iterator:
    train_loader = GavelIterator(train_loader, args.checkpoint_dir,
                                 load_checkpoint, save_checkpoint)
#new_params_for_GNS
grad_norm_arr = []
S_arr = []
temp_grad_norm_queue = []
if args.distributed:
    window_size = args.world_size
else:
    window_size = 2
sliding_grad_array = []
noisePercentage = 10
gnsPrev = 0
gns_arr = []
grad_calc_dict = dict()
original_batch_size = None
bs_last_interval = None
state = None
if args.checkpoint_dir is not None:
    if not os.path.isdir(args.checkpoint_dir):
        os.mkdir(args.checkpoint_dir)
    else:
        checkpoint_path = os.path.join(args.checkpoint_dir, 'model.chkpt')
        if os.path.exists(checkpoint_path):
            if args.enable_gavel_iterator:
                state = train_loader.load_checkpoint(args, checkpoint_path)
                #print("in gavel iterator is state ", state['S_arr'])
            else:
                state = load_checkpoint(args, checkpoint_path)
                #print("in gavel iteratro false ", state['S_arr'])
if state is not None:
    if state['model'] is None:
        raise RuntimeError('Failed to get model from checkpoint!')
    model = state['model'].to(device)
    #model.load_state_dict(state['model'])
    #optimizer.load_state_dict(state['optimizer'])
    start_epoch = state['epoch']
    new_lr = state['new_lr']
    grad_calc_dict = state['grad_calc_dict']
    original_batch_size = state['original_batch_size']
    grad_norm_arr = state['grad_norm_arr']
    S_arr = state['S_arr']
    temp_grad_norm_queue = state['temp_grad_norm_queue']
    gnsPrev = state['gnsPrev']
    sliding_grad_array = state['sliding_grad_array']
    window_size = state['window_size']
    noisePercentage = state['noisePercentage']
    gns_arr = state['gns_arr']
    print("learning rate fter checkpointing is", new_lr)
else:
    model = model.RNNModel(args.model, ntokens, args.emsize, args.nhid,
                           args.nlayers, args.dropout, args.tied).to(device)
    original_batch_size = args.batch_size

if args.distributed:
    model = torch.nn.parallel.DistributedDataParallel(
                 model, device_ids=[args.local_rank],
                 output_device=args.local_rank)
    print("args.distributed is ", args.distributed)
    #model = torch.nn.DataParallel(model, device_ids=[args.local_rank],output_device=args.local_rank)

cumulative_steps = 0
cumulative_time = 0

criterion = nn.CrossEntropyLoss()
optimizer = torch.optim.SGD(model.parameters(), args.lr)

###############################################################################
# Training code
###############################################################################

def repackage_hidden(h):
    """Wraps hidden states in new Tensors, to detach them from their history."""
    if isinstance(h, torch.Tensor):
        return h.detach()
    else:
        return tuple(repackage_hidden(v) for v in h)

def evaluate(loader):
    # Turn on evaluation mode which disables dropout.
    model.eval()
    total_loss = 0.
    ntokens = len(corpus.dictionary)
    hidden = model.init_hidden(eval_batch_size)
    with torch.no_grad():
        for i, batch in enumerate(loader):
            (data, targets) = batch
            data = data.t()
            targets = targets.t()

            output, hidden = model(data, hidden)
            output_flat = output.view(-1, ntokens)
            total_loss += len(data) * criterion(output_flat, targets.flatten()).item()
            hidden = repackage_hidden(hidden)
    return total_loss / (len(loader) - 1)


def train(full_rank_accum, cumulative_steps=None, cumulative_time=None, grad_norm_arr=None, S_arr=None, temp_grad_norm_queue=None, sliding_grad_array=None):
    # Turn on training mode which enables dropout.
    model.train()
    num_completed_iters = 0
    total_loss = 0.
    start_time = time.time()
    ntokens = len(corpus.dictionary)
    if args.distributed:
        hidden = model.module.init_hidden(args.batch_size)
    else:
        hidden = model.init_hidden(args.batch_size)
    done = False
    for i, batch in enumerate(train_loader):
        total_duration_tracker_start = time.time()

        # Batch size should be the second dimension, not first.
        (data, targets) = batch
        data = data.t()
        targets = targets.t()

        # Starting each batch, we detach the hidden state from how it was previously produced.
        # If we didn't, the model would try backpropagating all the way to start of the dataset.
        hidden = repackage_hidden(hidden)
        model.zero_grad()
        output, hidden = model(data, hidden)

        # Shape of output and targets need to align.
        loss = criterion(output.view(-1, ntokens), targets.flatten())
        loss.backward()
        # `clip_grad_norm` helps prevent the exploding gradient problem in RNNs / LSTMs.
        torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip)

        # if args.distributed == True:
        #     temp_grad_norm_queue = []
        #     grad_array_temp = [param.grad.data for param in model.parameters()]
        #     grad_norm_temp_arr = [torch.norm(pval).item() for pval in grad_array_temp]
        #     temp_grad_norm_queue.append(sum(grad_norm_temp_arr))
        #     average_gradients(model)
        
        optimizer.step()

        total_loss += loss.item()
        #if args.distributed and args.world_size > 4:
            #print("args is distributed")
        grad_norm_arr, S_arr, temp_grad_norm_queue, sliding_grad_array  = append_GNS_params(model,grad_norm_arr, S_arr, temp_grad_norm_queue, sliding_grad_array)

        if i % args.log_interval == 0 and i > 0:
            cur_loss = total_loss / args.log_interval
            elapsed = time.time() - start_time
            print('| epoch {:3d} | {:5d}/{:5d} batches | lr {:02.2f} | ms/batch {:5.2f} | '
                    'loss {:5.2f} | ppl {:8.2f}'.format(
                epoch, i, len(train_loader), lr,
                elapsed * 1000 / args.log_interval, cur_loss, math.exp(cur_loss)))
            total_loss = 0
            start_time = time.time()
        if cumulative_steps is not None:
          cumulative_steps += 1
          if (args.throughput_estimation_interval is not None and
              cumulative_steps % args.throughput_estimation_interval == 0):
              print('[THROUGHPUT_ESTIMATION]\t%s\t%d' % (time.time(),
                                                         cumulative_steps))

          if args.steps is not None and cumulative_steps >= args.steps:
            done = True
            break
        if args.max_duration is not None:
          cumulative_time += time.time() - total_duration_tracker_start
          total_duration_tracker_start = time.time()
          if cumulative_time >= args.max_duration:
            done = True
            break
        # grad_array = [param.grad.data for param in model.parameters()]
        # yield grad_array
        num_completed_iters += 1

    return (cumulative_steps, cumulative_time, done, full_rank_accum, num_completed_iters, grad_norm_arr, S_arr, temp_grad_norm_queue, sliding_grad_array)

def average_gradients(model):
    size = float(dist.get_world_size())
    for param in model.parameters():
        dist.all_reduce(param.grad.data, op=dist.ReduceOp.SUM)
        param.grad.data /= size

def gather_grad_array(model, full_rank_accum):
    # grad_array = [param.grad.data for param in model.parameters() if param.ndim == 4]
    grad_array = [param.grad.data for param in model.parameters()]
    for idx, grad_val in enumerate(grad_array):
        full_rank_accum[idx].add_(grad_val.data)

def get_grad_norm_big(model, sliding_grad_array):
    grad_array1 = [param.grad.data for param in model.parameters()]
    sliding_grad_array.append(grad_array1)
    #print("sliding grad array is", sliding_grad_array)
    sliding_tensor_sum = [torch.zeros_like(copy_l) for copy_l in model.parameters()]
    grad_norm_big= 0
    if window_size is not None and len(sliding_grad_array)>=window_size:
        if len(sliding_grad_array)> window_size:
            sliding_grad_array.pop(0)
        for tensor in sliding_grad_array:
            for idx, grad_val in enumerate(tensor):
                sliding_tensor_sum[idx].add_(grad_val.data/float(window_size))
        grad_norm_big_arr = [torch.norm(pval).item() for pval in sliding_tensor_sum]
        grad_norm_big = sum(grad_norm_big_arr)
    return (grad_norm_big, sliding_grad_array)

def append_GNS_params(model,grad_norm_arr, S_arr, temp_grad_norm_queue, sliding_grad_array):
    # if args.distributed == True: 
    #     #print("inside append_gns_param distributed")
    #     grad_norm_small = temp_grad_norm_queue[0]
    #     grad_array_temp = [param.grad.data for param in model.parameters()]
    #     grad_norm_temp_arr = [torch.norm(pval).item() for pval in grad_array_temp]
    #     grad_norm_big = sum(grad_norm_temp_arr)
    #     size = float(dist.get_world_size())
    #     grad_norm_val = (float(size)*grad_norm_big-grad_norm_small)/float(size-1)
    #     S_val = ((float(size)*args.batch_size)*(grad_norm_small-grad_norm_big))/float(size-1)
    #     grad_norm_arr.append(grad_norm_val)
    #     S_arr.append(S_val)
    #     #print("grad norm array and sarray size is ", len(grad_norm_arr), len(S_arr))
    #     return (grad_norm_arr,S_arr, temp_grad_norm_queue, sliding_grad_array)
    
    (grad_norm_big, sliding_grad_array) = get_grad_norm_big(model,sliding_grad_array)
    grad_array_temp = [param.grad.data for param in model.parameters()]
    grad_norm_temp_arr = [torch.norm(pval).item() for pval in grad_array_temp]
    norm_sum_temp = sum(grad_norm_temp_arr)
    temp_grad_norm_queue.append(norm_sum_temp)
    if window_size is not None and len(temp_grad_norm_queue)>=window_size:
        if len(temp_grad_norm_queue)> window_size:
            temp_grad_norm_queue.pop(0) 
        grad_norm_small = temp_grad_norm_queue[0]
        grad_norm_val = (float(window_size)*grad_norm_big-grad_norm_small)/float(window_size-1)
        S_val = ((float(window_size)*args.batch_size)*(grad_norm_small-grad_norm_big))/float(window_size-1)
        grad_norm_arr.append(grad_norm_val)
        S_arr.append(S_val)
    return (grad_norm_arr,S_arr, temp_grad_norm_queue,sliding_grad_array)

def get_GNS(grad_norm_arr,S_arr):
    if len(S_arr)==0:
         print("S_array is 0")
         return 0
    S_avg = sum(S_arr)/float(len(S_arr))
    grad_norm_avg = sum(grad_norm_arr)/float(len(grad_norm_arr))
    gns = S_avg/grad_norm_avg
    return gns

def export_onnx(path, batch_size, seq_len):
    print('The model is also exported in ONNX format at {}'.
          format(os.path.realpath(args.onnx_export)))
    model.eval()
    dummy_input = torch.LongTensor(seq_len * batch_size).zero_().view(-1, batch_size).to(device)
    hidden = model.init_hidden(batch_size)
    torch.onnx.export(model, (dummy_input, hidden), path)

# def out_of_critical_regime(cumulative_steps):
#     """switch out of cr after 5 epochs
#     """
#     return (
#         (args.batch_size == 5 and cumulative_steps == 59675) or  # 59675/5=11935, 11935*5=59675
#         (args.batch_size == 10 and cumulative_steps == 29840) or  # 59675/10=5968, 5968*5=29840
#         (args.batch_size == 20 and cumulative_steps == 14920) or  # 59675/20=2984, 2984*5=14920
#         (args.batch_size == 40 and cumulative_steps == 7460) or  # 59675/40=1492, 1492*5=7460
#         (args.batch_size == 80 and cumulative_steps == 3730)  # 59675/80=746, 746*5=3730
#     )


# Loop over epochs.
lr = args.lr
best_val_loss = None

print(f"len of dataloader is {len(train_loader)}")


def check_critical_regime(grad_calc_dict, epoch, args, original_batch_size):
    check_freq = 10
    threshold = 0.5

    out_of_critical_regime = False
    status_changed = False
    if epoch % check_freq == 0:
        current_grad_norms = grad_calc_dict[epoch]        
        old_grad_norms = grad_calc_dict[epoch - check_freq] if epoch != 0 else [None] * len(current_grad_norms)
        if epoch != 0:
            # take the sum of gradient norms of all layers
            new_norm_sum = sum(current_grad_norms)
            prev_norm_sum = sum(old_grad_norms)
            # print(f"new_norm_sum is {new_norm_sum}, prev_norm_sum is {prev_norm_sum}")
            ratio = (abs(prev_norm_sum - new_norm_sum))/(prev_norm_sum)
            out_of_critical_regime = ratio < threshold
            if out_of_critical_regime and args.batch_size == original_batch_size or \
                not out_of_critical_regime and args.batch_size != original_batch_size:
                status_changed = True
            print(f"[Epoch {epoch}], ratio: {round(ratio,5)}, out_of_critical_regime: {out_of_critical_regime}, status_changed: {status_changed}")
    return out_of_critical_regime, status_changed

def getMemoryInfo():
    nvmlInit()
    h = nvmlDeviceGetHandleByIndex(0)
    info = nvmlDeviceGetMemoryInfo(h)
    print(f'total    : {info.total}')
    print(f'free     : {info.free}')
    print(f'used     : {info.used}')
    return (info.free, info.used)

def linear_learning_rate(optimizer, new_lr):
    for group in optimizer.param_groups:
        group['lr'] = group['lr']*2
        new_lr = group['lr']
    return (optimizer, new_lr)

def sqRoot_learning_rate(optimizer, new_lr):
    for group in optimizer.param_groups:
        group['lr'] = group['lr']*1.414
        new_lr = group['lr']
    return (optimizer, new_lr)

def adascale_learning_rate(optimizer, gns, batch_size, new_lr):
    thetaT = batch_size*gns
    rate = ((thetaT/batch_size)+1)*((thetaT/(2*batch_size))+1)
    for group in optimizer.param_groups:
        group['lr'] = group['lr']*rate
        new_lr = group['lr']
    return (optimizer, new_lr)

# At any point you can hit Ctrl + C to break out of training early.
try:
    if args.steps is not None:
        # args.epochs = math.ceil(args.steps *
        #                         args.batch_size / len(train_loader))
        args.epochs = math.ceil(args.steps / len(train_loader))
    if args.epochs is None:
        args.epochs = args.steps
    print(f"epochs: {args.epochs}, steps: {args.steps}, len(train_loader): {len(train_loader)}")
    for epoch in range(start_epoch, args.epochs):
        print("epoch ,rank local and rank is", epoch, args.local_rank, args.rank)
        epoch_start_time = time.time()
        for group in optimizer.param_groups:
            group['lr'] = new_lr
            #print("group lr in epoch is ", group['lr']
        grad_norm_arr = []
        S_arr = []
        temp_grad_norm_queue = []
        sliding_grad_array = []
        full_rank_accum = [torch.zeros_like(copy_l) for copy_l in model.parameters()]
        cumulative_steps, cumulative_time, done, full_rank_accum, num_completed_iters, grad_norm_arr, S_arr, temp_grad_norm_queue, sliding_grad_array= train(full_rank_accum, cumulative_steps,
                                                        cumulative_time,  grad_norm_arr, S_arr, temp_grad_norm_queue, sliding_grad_array)
        # grad_calc_dict[epoch] = [torch.norm(pval).item() for pval in full_rank_accum]
        # print(f"[Epoch {epoch}] Sum of the gradient norms: {sum(grad_calc_dict[epoch])}")
        #print("S_arr after append_GNS is",len(S_arr))
        done = False

        print('-' * 89)
        print('| end of epoch {:3d} | time: {:5.2f}s'.format(epoch, (time.time() - epoch_start_time)))

        # check for critical regime
        # out_of_critical_regime, status_changed = check_critical_regime(grad_calc_dict, epoch, args, original_batch_size)
        if args.enable_gavel_iterator:
            # (memFree, memUsed) = getMemoryInfo()
            if args.distributed and args.world_size > 4:
                gns = get_GNS(grad_norm_arr,S_arr)
                gns_arr.append(gns)
                print('For epoch, batch_size, gns and gnsprev lr is ',epoch, args.batch_size, gns, gnsPrev, optimizer.param_groups[0]['lr'])
                #(memFree, memUsed) = getMemoryInfo()
                if epoch!=0 and args.batch_size!=80 and epoch%10==0:
                    mk_data = gns_arr[-10:]
                    #mk_data.pop()
                    avg_ws = sum(mk_data)/10
                    print("avg window gns is", avg_ws)
                    # ws_per = 0
                    # if avg_ws != 0:
                    #     ws_per = ((gns-avg_ws)/abs(avg_ws))*100
                    #print("window noise is ", ws_per )
                    # if args.distributed == True:
                    #     checkBsList = [None] * args.world_size
                    #     updateBatchSize = 0
                    #     if gns>avg_ws:
                    #         updateBatchSize = 1
                    #     dist.barrier()
                    #     dist.all_gather_object(checkBsList,updateBatchSize)
                    #     print("checktensor rank and worldsize is ",checkBsList, args.rank, args.world_size)
                    #     if float(sum(checkBsList)) >= float(len(checkBsList)/2.0):
                    #         (optimizer, new_lr) = linear_learning_rate(optimizer, new_lr)
                    #         train_loader.update_resource_requirement(big_bs=True, small_bs=False)
                    #if args.distributed == False and gns>avg_ws:
                    if gns>avg_ws:
                        (optimizer, new_lr) = linear_learning_rate(optimizer, new_lr)
                        #(optimizer, new_lr) = sqRoot_learning_rate(optimizer, new_lr)
                        #(optimizer, new_lr) = adascale_learning_rate(optimizer, gns, args.batch_size, new_lr)
                        train_loader.update_resource_requirement(big_bs=True, small_bs=False)
                gnsPrev = gns
                print("gnsPrev is for the next epoch",gnsPrev)
            elif epoch!=0 and args.batch_size!=80 and epoch%10==0:
                print("in gpus metric")
                # batch_size_switch_set = set([(5,30),(10,40),(20,60),(40,70),(10,10),(20,20),(40,40),(20,10),(40,10)])
                #batch_size_switch_set_2gpu = set([(5,30),(10, 50),(20, 60),(40, 70),(10,10),(20, 30),(40, 40),(40,10)])
                # batch_size_switch_set_4gpu = set([(5,10),(10,30),(20, 70),(40, 90),(10,10),(20, 30),(20,10),(40, 60)])
                print('For epoch, batch_size lr metric is ',epoch, args.batch_size, optimizer.param_groups[0]['lr'])
                print("original_batch_size and world size is", original_batch_size, args.world_size)
                if not args.distributed:
                    if original_batch_size == 5:
                        batch_size_switch_set = set([(5,30),(10,40),(20,60),(40,70)])
                    elif original_batch_size == 10:
                        batch_size_switch_set = set([(10,10),(20,20),(40,40)])
                    elif original_batch_size == 20:
                        batch_size_switch_set = set([(20,10),(40,40)])
                    elif original_batch_size == 40:
                        batch_size_switch_set = set([(40,10)])
                    else:
                        batch_size_switch_set = set([])
                    if (args.batch_size,epoch) in batch_size_switch_set:
                        print("batch size and lr updated", args.batch_size, epoch, optimizer.param_groups[0]['lr'])
                        (optimizer, new_lr) = linear_learning_rate(optimizer, new_lr)
                        train_loader.update_resource_requirement(big_bs=True, small_bs=False)
                elif args.world_size == 2:
                    if original_batch_size == 5:
                        batch_size_switch_set_2gpu = set([(5,30),(10,50),(20,60),(40,70)])
                    elif original_batch_size == 10:
                        batch_size_switch_set_2gpu = set([(10,10),(20,30),(40,40)])
                    elif original_batch_size == 20:
                        batch_size_switch_set_2gpu = set([(20,30),(40,40)])
                    elif original_batch_size == 40: 
                        batch_size_switch_set_2gpu = set([(40,10)])
                    else:
                        batch_size_switch_set_2gpu = set([])
                    if (args.batch_size,epoch) in batch_size_switch_set_2gpu:
                        print("batch size and lr updated2", args.batch_size, epoch, optimizer.param_groups[0]['lr'])
                        (optimizer, new_lr) = linear_learning_rate(optimizer, new_lr)
                        train_loader.update_resource_requirement(big_bs=True, small_bs=False)
                elif args.world_size == 4:
                    if original_batch_size == 5:
                        batch_size_switch_set_4gpu = set([(5,10),(10,30),(20,70),(40,90)])
                    elif original_batch_size == 10: 
                        batch_size_switch_set_4gpu = set([(10,10),(20,30),(40,60)])
                    elif original_batch_size == 20:
                        batch_size_switch_set_4gpu = set([(20,10),(40,60)])
                    elif original_batch_size == 40:
                        batch_size_switch_set_4gpu = set([(40,60)])
                    else:
                        batch_size_switch_set_4gpu = set([])
                    if (args.batch_size,epoch) in batch_size_switch_set_4gpu:
                        print("batch size and lr update tktk", args.batch_size, epoch, optimizer.param_groups[0]['lr'])
                        (optimizer, new_lr) = linear_learning_rate(optimizer, new_lr)
                        train_loader.update_resource_requirement(big_bs=True, small_bs=False)
            # if status_changed and out_of_critical_regime and args.batch_size != 80:
            #     train_loader.update_resource_requirement(big_bs=True, small_bs=False)
            if train_loader.done:
                break
            elif done:
                train_loader.complete()
                break
        elif done:
          break
        print('-' * 89)
    checkpoint_path = os.path.join(args.checkpoint_dir, 'model.chkpt')
    if not args.distributed or args.rank == 0:
        if args.distributed:
            state = {
                    'model': model.module, 
                    'gnsPrev': gnsPrev,
                    'noisePercentage': noisePercentage,
                    'new_lr': new_lr,
                    'epoch': epoch + (1 if num_completed_iters == len(train_loader) else 0),
                    'grad_calc_dict': grad_calc_dict,
                    'grad_norm_arr': grad_norm_arr,
                    'S_arr': S_arr,
                    'temp_grad_norm_queue': temp_grad_norm_queue,
                    'sliding_grad_array': sliding_grad_array,
                    'original_batch_size': original_batch_size,
                    'bs_last_interval': args.batch_size,
                    'window_size': window_size,
                    'gns_arr': gns_arr
                    }
        else:
            state = {
                #'model': model.state_dict(),
                'model': model,
                'optimizer': optimizer.state_dict(),
                'epoch': epoch + (1 if num_completed_iters == len(train_loader) else 0),
                'grad_calc_dict': grad_calc_dict,
                'original_batch_size': original_batch_size,
                'bs_last_interval': args.batch_size,
                'grad_norm_arr': grad_norm_arr,
                'S_arr': S_arr,
                'temp_grad_norm_queue': temp_grad_norm_queue,
                'sliding_grad_array': sliding_grad_array,
                'gnsPrev': gnsPrev,
                'window_size': window_size,
                'noisePercentage': noisePercentage,
                'new_lr': new_lr,
                'gns_arr': gns_arr
            }
        if args.enable_gavel_iterator:
            train_loader.save_checkpoint(state, checkpoint_path)
        else:
            save_checkpoint(state, checkpoint_path)
except KeyboardInterrupt:
    print('-' * 89)
    print('Exiting from training early')
