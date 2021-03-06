import torch
import argparse
import dgl
import torch.multiprocessing as mp
import os
import random
import time
import numpy as np

from reading_data import DeepwalkDataset
from model import SkipGramModel
from utils import thread_wrapped_func, shuffle_walks

class DeepwalkTrainer:
    def __init__(self, args):
        """ Initializing the trainer with the input arguments """
        self.args = args
        self.dataset = DeepwalkDataset(
            net_file=args.net_file,
            walk_length=args.walk_length,
            window_size=args.window_size,
            num_walks=args.num_walks,
            batch_size=args.batch_size,
            negative=args.negative,
            fast_neg=args.fast_neg,
            )
        self.emb_size = len(self.dataset.net)
        self.emb_model = None

    def init_device_emb(self):
        """ set the device before training 
        will be called once in fast_train_mp / fast_train
        """
        choices = sum([self.args.only_gpu, self.args.only_cpu, self.args.mix])
        assert choices == 1, "Must choose only *one* training mode in [only_cpu, only_gpu, mix]"
        assert self.args.num_procs >= 1, "The number of process must be larger than 1"
        choices = sum([self.args.sgd, self.args.adam, self.args.avg_sgd])
        assert choices == 1, "Must choose only *one* gradient descent strategy in [sgd, avg_sgd, adam]"
        
        # initializing embedding on CPU
        self.emb_model = SkipGramModel(
            emb_size=self.emb_size, 
            emb_dimension=self.args.dim,
            walk_length=self.args.walk_length,
            window_size=self.args.window_size,
            batch_size=self.args.batch_size,
            only_cpu=self.args.only_cpu,
            only_gpu=self.args.only_gpu,
            mix=self.args.mix,
            neg_weight=self.args.neg_weight,
            negative=self.args.negative,
            lr=self.args.lr,
            lap_norm=self.args.lap_norm,
            adam=self.args.adam,
            sgd=self.args.sgd,
            avg_sgd=self.args.avg_sgd,
            fast_neg=self.args.fast_neg
            )
        
        torch.set_num_threads(self.args.num_threads)
        if self.args.only_gpu:
            print("Run in 1 GPU")
            self.emb_model.all_to_device(0)
        elif self.args.mix:
            print("Mix CPU with %d GPU" % self.args.num_procs)
            if self.args.num_procs == 1:
                self.emb_model.set_device(0)
        else:
            print("Run in %d CPU process" % self.args.num_procs)

    def train(self):
        """ train the embedding """
        if self.args.num_procs > 1:
            self.fast_train_mp()
        else:
            self.fast_train()

    def fast_train_mp(self):
        """ multi-cpu-core or mix cpu & multi-gpu """
        self.init_device_emb()
        self.emb_model.share_memory()
        self.dataset.walks = shuffle_walks(self.dataset.walks)

        start_all = time.time()
        ps = []

        l = len(self.dataset.walks)
        np_ = self.args.num_procs
        for i in range(np_):
            walks = self.dataset.walks[int(i * l / np_): int((i + 1) * l / np_)]
            p = mp.Process(target=self.fast_train_sp, args=(walks, i))
            ps.append(p)
            p.start()

        for p in ps:
            p.join()
        
        print("Used time: %.2fs" % (time.time()-start_all))
        self.emb_model.save_embedding(self.dataset, self.args.emb_file)

    @thread_wrapped_func
    def fast_train_sp(self, walks, gpu_id):
        """ a subprocess for fast_train_mp """
        # number of batches in this process
        num_batches = int(np.ceil(len(walks) / self.args.batch_size))
        # number of positive node pairs in a sequence
        num_pos = int(2 * self.args.walk_length * self.args.window_size\
            - self.args.window_size * (self.args.window_size + 1))
        print("num batchs: %d in subprocess [%d]" % (num_batches, gpu_id))
        self.emb_model.set_device(gpu_id)
        torch.set_num_threads(self.args.num_threads)

        start = time.time()
        with torch.no_grad():
            i = 0
            max_i = self.args.iterations * num_batches
            
            while True:
                # decay learning rate for SGD
                lr = self.args.lr * (max_i - i) / max_i
                if lr < 0.00001:
                    lr = 0.00001

                # multi-sequence input
                i_ = int(i % num_batches)
                walks_ = list(walks[i_ * self.args.batch_size: \
                        (1+i_) * self.args.batch_size])
                if len(walks_) == 0:
                    break

                if self.args.fast_neg:
                    self.emb_model.fast_learn_super(walks_, lr)
                else:
                    # do negative sampling
                    bs = len(walks_)
                    neg_nodes = torch.LongTensor(
                        np.random.choice(self.dataset.neg_table, 
                            bs * num_pos * self.args.negative, 
                            replace=True))
                    self.emb_model.fast_learn_super(walks_, lr, neg_nodes=neg_nodes)

                i += 1
                if i > 0 and i % self.args.print_interval == 0:
                    print("Solver [%d] batch %d tt: %.2fs" % (gpu_id, i, time.time()-start))
                    start = time.time()
                if i_ == num_batches - 1:
                    break

    def fast_train(self):
        """ one process """
        # the number of postive node pairs of a node sequence
        num_pos = 2 * self.args.walk_length * self.args.window_size\
            - self.args.window_size * (self.args.window_size + 1)
        num_pos = int(num_pos)
        num_batches = len(self.dataset.net) * self.args.num_walks / self.args.batch_size
        num_batches = int(np.ceil(num_batches))
        print("num batchs: %d" % num_batches)

        self.init_device_emb()

        start_all = time.time()
        start = time.time()
        with torch.no_grad():
            i = 0
            max_i = self.args.iterations * num_batches
            for iteration in range(self.args.iterations):
                print("\nIteration: " + str(iteration + 1))
                self.dataset.walks = shuffle_walks(self.dataset.walks)

                while True:
                    # decay learning rate for SGD
                    lr = self.args.lr * (max_i - i) / max_i
                    if lr < 0.00001:
                        lr = 0.00001

                    # multi-sequence input
                    i_ = int(i % num_batches)
                    walks = list(self.dataset.walks[i_ * self.args.batch_size: \
                            (1+i_) * self.args.batch_size])
                    if len(walks) == 0:
                        break

                    if self.args.fast_neg:
                        self.emb_model.fast_learn_super(walks, lr)
                    else:
                        # do negative sampling
                        bs = len(walks)
                        neg_nodes = torch.LongTensor(
                            np.random.choice(self.dataset.neg_table, 
                                bs * num_pos * self.args.negative, 
                                replace=True))
                        self.emb_model.fast_learn_super(walks, lr, neg_nodes=neg_nodes)

                    i += 1
                    if i > 0 and i % self.args.print_interval == 0:
                        print("Batch %d, training time: %.2fs" % (i, time.time()-start))
                        start = time.time()
                    if i_ == num_batches - 1:
                        break

        print("Training used time: %.2fs" % (time.time()-start_all))
        self.emb_model.save_embedding(self.dataset, self.args.emb_file)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="DeepWalk")
    parser.add_argument('--net_file', type=str, 
            help="path of the txt network file")
    parser.add_argument('--emb_file', type=str, default="emb.txt",
            help='path of the txt embedding file')
    parser.add_argument('--dim', default=128, type=int, 
            help="embedding dimensions")
    parser.add_argument('--window_size', default=5, type=int, 
            help="context window size")
    parser.add_argument('--num_walks', default=10, type=int, 
            help="number of walks for each node")
    parser.add_argument('--negative', default=5, type=int, 
            help="negative samples for each positve node pair")
    parser.add_argument('--iterations', default=1, type=int, 
            help="iterations")
    parser.add_argument('--batch_size', default=10, type=int, 
            help="number of node sequences in each batch")
    parser.add_argument('--print_interval', default=1000, type=int, 
            help="number of batches between printing")
    parser.add_argument('--walk_length', default=80, type=int, 
            help="number of nodes in a sequence")
    parser.add_argument('--lr', default=0.2, type=float, 
            help="learning rate")
    parser.add_argument('--neg_weight', default=1., type=float, 
            help="negative weight")
    parser.add_argument('--lap_norm', default=0.01, type=float, 
            help="weight of laplacian normalization")
    parser.add_argument('--mix', default=False, action="store_true", 
            help="mixed training with CPU and GPU")
    parser.add_argument('--only_cpu', default=False, action="store_true", 
            help="training with CPU")
    parser.add_argument('--only_gpu', default=False, action="store_true", 
            help="training with GPU")
    parser.add_argument('--fast_neg', default=True, action="store_true", 
            help="do negative sampling inside a batch")
    parser.add_argument('--adam', default=False, action="store_true", 
            help="use adam for embedding updation")
    parser.add_argument('--sgd', default=False, action="store_true", 
            help="use sgd for embedding updation")
    parser.add_argument('--avg_sgd', default=False, action="store_true", 
            help="average gradients of sgd for embedding updation")
    parser.add_argument('--num_threads', default=8, type=int, 
            help="number of threads used on CPU")
    parser.add_argument('--num_procs', default=1, type=int, 
            help="number of GPUs/CPUs when mixed training")
    args = parser.parse_args()

    start_time = time.time()
    trainer = DeepwalkTrainer(args)
    trainer.train()
    print("Total used time: %.2f" % (time.time() - start_time))