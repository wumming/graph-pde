import torch
import numpy as np
import scipy.io
import h5py
import sklearn.metrics

import torch.nn as nn

#################################################
#
# Utilities
#
#################################################
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

class MatReader(object):
    def __init__(self, file_path, to_torch=True, to_cuda=False, to_float=True):
        super(MatReader, self).__init__()

        self.to_torch = to_torch
        self.to_cuda = to_cuda
        self.to_float = to_float

        self.file_path = file_path

        self.data = None
        self.old_mat = None
        self._load_file()

    def _load_file(self):
        try:
            self.data = scipy.io.loadmat(self.file_path)
            self.old_mat = True
        except:
            self.data = h5py.File(self.file_path)
            self.old_mat = False

    def load_file(self, file_path):
        self.file_path = file_path
        self._load_file()

    def read_field(self, field):
        x = self.data[field]

        if not self.old_mat:
            x = x[()]
            x = np.transpose(x, axes=range(len(x.shape) - 1, -1, -1))

        if self.to_float:
            x = x.astype(np.float32)

        if self.to_torch:
            x = torch.from_numpy(x)

            if self.to_cuda:
                x = x.cuda()

        return x

    def set_cuda(self, to_cuda):
        self.to_cuda = to_cuda

    def set_torch(self, to_torch):
        self.to_torch = to_torch

    def set_float(self, to_float):
        self.to_float = to_float


class UnitGaussianNormalizer(object):
    def __init__(self, x):
        super(UnitGaussianNormalizer, self).__init__()

        self.mean = torch.mean(x, 0).view(-1)
        self.std = torch.std(x, 0).view(-1)

    def encode(self, x):
        s = x.size()
        x = x.view(s[0], -1)
        x = (x - self.mean) / self.std
        x = x.view(s)
        return x

    def decode(self, x):
        s = x.size()
        x = x.view(s[0], -1)
        x = (x * self.std) + self.mean
        x = x.view(s)
        return x

    def cuda(self):
        self.mean = self.mean.cuda()
        self.std = self.std.cuda()

class RangeNormalizer(object):
    def __init__(self, x, low=0.0, high=1.0):
        super(RangeNormalizer, self).__init__()
        mymin = torch.min(x, 0)[0].view(-1)
        mymax = torch.max(x, 0)[0].view(-1)

        self.a = (high - low)/(mymax - mymin)
        self.b = -a*mymax + high

    def encode(self, x):
        s = x.size()
        x = x.view(s[0], -1)
        x = self.a*x + self.b
        x = x.view(s)
        return x

    def decode(self, x):
        s = x.size()
        x = x.view(s[0], -1)
        x = (x - self.b)/self.a
        x = x.view(s)
        return x

class LpLoss(object):
    def __init__(self, d=2, p=2, size_average=True, reduction=True):
        super(LpLoss, self).__init__()

        #Dimension and Lp-norm type are postive
        assert d > 0 and p > 0

        self.d = d
        self.p = p
        self.reduction = reduction
        self.size_average = size_average

    def abs(self, x, y):
        num_examples = x.size()[0]

        #Assume uniform mesh
        h = 1.0 / (x_size.size()[1] - 1.0)

        all_norms = (h**(self.d/self.p))*torch.norm(x.view(num_examples,-1) - y.view(num_examples,-1), self.p, 1)

        if self.reduction:
            if self.size_average:
                return torch.mean(all_norms)
            else:
                return torch.sum(all_norms)

        return all_norms

    def rel(self, x, y):
        num_examples = x.size()[0]

        diff_norms = torch.norm(x.view(num_examples,-1) - y.view(num_examples,-1), self.p, 1)
        y_norms = torch.norm(y.view(num_examples,-1), self.p, 1)

        if self.reduction:
            if self.size_average:
                return torch.mean(diff_norms/y_norms)
            else:
                return torch.sum(diff_norms/y_norms)

        return diff_norms/y_norms

    def __call__(self, x, y):
        return self.rel(x, y)

class DenseNet(torch.nn.Module):
    def __init__(self, layers, nonlinearity, out_nonlinearity=None, normalize=False):
        super(DenseNet, self).__init__()

        self.n_layers = len(layers) - 1

        assert self.n_layers >= 1

        self.layers = nn.ModuleList()

        for j in range(self.n_layers):
            self.layers.append(nn.Linear(layers[j], layers[j+1]))

            if j != self.n_layers - 1:
                if normalize:
                    self.layers.append(nn.BatchNorm1d(layers[j+1]))

                self.layers.append(nonlinearity())

        if out_nonlinearity is not None:
            self.layers.append(out_nonlinearity())

    def forward(self, x):
        for _, l in enumerate(self.layers):
            x = l(x)

        return x

class SquareMeshGenerator(object):
    def __init__(self, real_space, mesh_size):
        super(SquareMeshGenerator, self).__init__()

        self.d = len(real_space)

        assert len(mesh_size) == self.d

        if self.d == 1:
            self.n = mesh_size[0]
            self.grid = np.linspace(real_space[0][0], real_space[0][1], self.n).reshape((self.n, 1))
        else:
            self.n = 1
            grids = []
            for j in range(self.d):
                grids.append(np.linspace(real_space[j][0], real_space[j][1], mesh_size[j]))
                self.n *= mesh_size[j]

            self.grid = np.vstack([xx.ravel() for xx in np.meshgrid(*grids)]).T
    
    def ball_connectivity(self, r):
        pwd = sklearn.metrics.pairwise_distances(self.grid)
        self.edge_index = np.vstack(np.where(pwd <= r))
        self.n_edges = self.edge_index.shape[1]

        return torch.tensor(self.edge_index, dtype=torch.long)

    def attributes(self, f=None, theta=None):
        if f is None:
            if theta is None:
                edge_attr = self.grid[self.edge_index.T].reshape((self.n_edges,-1))
            else:
                edge_attr = np.zeros((self.n_edges, 2*self.d + 1))
                edge_attr[:,0:2*self.d] = self.grid[self.edge_index.T].reshape((self.n_edges,-1))
                edge_attr[:,2*self.d] = theta[self.edge_index[1]]
        else:
            xy = self.grid[self.edge_index.T].reshape((self.n_edges,-1))
            if theta is None:
                edge_attr = f(xy[:,0:self.d], xy[:,self.d:])
            else:
                edge_attr = f(xy[:,0:self.d], xy[:,self.d:], theta[self.edge_index[0]], theta[self.edge_index[1]])

        return torch.tensor(edge_attr, dtype=torch.float)





def downsample(data, grid_size, l):
    data = data.reshape(-1, grid_size, grid_size)
    data = data[:, ::l, ::l]
    data = data.reshape(-1, (grid_size // l) ** 2)
    return data



def grid(n_x, n_y):
    xs = np.linspace(0.0, 1.0, n_x)
    ys = np.linspace(0.0, 1.0, n_y)
    # xs = np.array(range(n_x))
    # ys = np.array(range(n_y))
    grid = np.vstack([xx.ravel() for xx in np.meshgrid(xs, ys)]).T

    edge_index = []
    edge_attr = []
    for y in range(n_y):
        for x in range(n_x):
            i = y * n_x + x
            if (x != n_x - 1):
                edge_index.append((i, i + 1))
                edge_attr.append((1, 0, 0))
                edge_index.append((i + 1, i))
                edge_attr.append((-1, 0, 0))

            if (y != n_y - 1):
                edge_index.append((i, i + n_x))
                edge_attr.append((0, 1, 0))
                edge_index.append((i + n_x, i))
                edge_attr.append((0, -1, 0))

    X = torch.tensor(grid, dtype=torch.float)
    # Exact = torch.tensor(Exact, dtype=torch.float).view(-1)
    edge_index = torch.tensor(edge_index, dtype=torch.long).transpose(0, 1)
    edge_attr = torch.tensor(edge_attr, dtype=torch.float)

    return X, edge_index, edge_attr


def grid_edge(n_x, n_y, a):
    a = a.reshape(n_x, n_y)
    xs = np.linspace(0.0, 1.0, n_x)
    ys = np.linspace(0.0, 1.0, n_y)
    # xs = np.array(range(n_x))
    # ys = np.array(range(n_y))
    grid = np.vstack([xx.ravel() for xx in np.meshgrid(xs, ys)]).T

    edge_index = []
    edge_attr = []
    for y in range(n_y):
        for x in range(n_x):
            i = y * n_x + x
            if (x != n_x - 1):
                d = 1 / n_x
                a1 = a[x, y]
                a2 = a[x + 1, y]
                edge_index.append((i, i + 1))
                edge_attr.append((d, a1, a2))
                edge_index.append((i + 1, i ))
                edge_attr.append((d, a2, a1))

            if (y != n_y - 1):
                d = 1 / n_y
                a1 = a[x, y]
                a2 = a[x, y+1]
                edge_index.append((i, i + n_x))
                edge_attr.append((d, a1, a2))
                edge_index.append((i + n_x, i))
                edge_attr.append((d, a2, a1))

    X = torch.tensor(grid, dtype=torch.float)
    # Exact = torch.tensor(Exact, dtype=torch.float).view(-1)
    edge_index = torch.tensor(edge_index, dtype=torch.long).transpose(0, 1)
    edge_attr = torch.tensor(edge_attr, dtype=torch.float)

    return X, edge_index, edge_attr

def grid_edge_aug(n_x, n_y, a):
    a = a.reshape(n_x, n_y)
    xs = np.linspace(0.0, 1.0, n_x)
    ys = np.linspace(0.0, 1.0, n_y)
    # xs = np.array(range(n_x))
    # ys = np.array(range(n_y))
    grid = np.vstack([xx.ravel() for xx in np.meshgrid(xs, ys)]).T

    edge_index = []
    edge_attr = []
    for y in range(n_y):
        for x in range(n_x):
            i = y * n_x + x
            if (x != n_x - 1):
                d = 1 / n_x
                a1 = a[x, y]
                a2 = a[x + 1, y]
                edge_index.append((i, i + 1))
                edge_attr.append((d, a1, a2, 1 / np.sqrt(np.abs(a1 * a2)),
                                 np.exp(-(d) ** 2), np.exp(-(d / 0.1) ** 2), np.exp(-(d / 0.01) ** 2)))
                edge_index.append((i + 1, i))
                edge_attr.append((d, a2, a1, 1 / np.sqrt(np.abs(a1 * a2)),
                                  np.exp(-(d) ** 2), np.exp(-(d / 0.1) ** 2), np.exp(-(d / 0.01) ** 2)))

            if (y != n_y - 1):
                d = 1 / n_y
                a1 = a[x, y]
                a2 = a[x, y+1]
                edge_index.append((i, i + n_x))
                edge_attr.append((d, a1, a2, 1 / np.sqrt(np.abs(a1 * a2)),
                                  np.exp(-(d) ** 2), np.exp(-(d / 0.1) ** 2), np.exp(-(d / 0.01) ** 2)))
                edge_index.append((i + n_x, i))
                edge_attr.append((d, a2, a1, 1 / np.sqrt(np.abs(a1 * a2)),
                                  np.exp(-(d) ** 2), np.exp(-(d / 0.1) ** 2), np.exp(-(d / 0.01) ** 2)))

    X = torch.tensor(grid, dtype=torch.float)
    # Exact = torch.tensor(Exact, dtype=torch.float).view(-1)
    edge_index = torch.tensor(edge_index, dtype=torch.long).transpose(0, 1)
    edge_attr = torch.tensor(edge_attr, dtype=torch.float)

    return X, edge_index, edge_attr

def grid_edge_aug_full(n_x, n_y, r, a):
    n = n_x * n_y

    xs = np.linspace(0.0, 1.0, n_x)
    ys = np.linspace(0.0, 1.0, n_y)

    grid = np.vstack([xx.ravel() for xx in np.meshgrid(xs, ys)]).T

    edge_index = []
    edge_attr = []

    for i1 in range(n):
        x1 = grid[i1]
        for i2 in range(n):
            x2 = grid[i2]

            d = np.linalg.norm(x1-x2)

            if(d<=r):
                a1 = a[i1]
                a2 = a[i2]
                edge_index.append((i1, i2))
                edge_attr.append((d, a1, a2, 1 / np.sqrt(np.abs(a1 * a2)),
                                 np.exp(-(d) ** 2), np.exp(-(d / 0.1) ** 2), np.exp(-(d / 0.01) ** 2)))
                edge_index.append((i2, i1))
                edge_attr.append((d, a2, a1, 1 / np.sqrt(np.abs(a1 * a2)),
                                  np.exp(-(d) ** 2), np.exp(-(d / 0.1) ** 2), np.exp(-(d / 0.01) ** 2)))

    X = torch.tensor(grid, dtype=torch.float)
    # Exact = torch.tensor(Exact, dtype=torch.float).view(-1)
    edge_index = torch.tensor(edge_index, dtype=torch.long).transpose(0, 1)
    edge_attr = torch.tensor(edge_attr, dtype=torch.float)

    return X, edge_index, edge_attr

def multi_grid(depth, n_x, n_y, grid, params):

    edge_index_global = []
    edge_attr_global = []
    X_global = []
    num_nodes = 0

    # build connected graph
    for l in range(depth):
        h_x_l = n_x // (2 ** l)
        h_y_l = n_y // (2 ** l)
        n_l = h_x_l * h_y_l

        a = downsample(params, n_x, (2 ** l))
        if grid == 'grid':
            X, edge_index_inner, edge_attr_inner = grid(h_y_l, h_x_l)
        elif grid == 'grid_edge':
            X, edge_index_inner, edge_attr_inner = grid_edge(h_y_l, h_x_l, a)
        elif grid == 'grid_edge_aug':
            X, edge_index_inner, edge_attr_inner = grid_edge(h_y_l, h_x_l, a)

        # update index
        edge_index_inner = edge_index_inner + num_nodes
        edge_index_global.append(edge_index_inner)
        edge_attr_global.append(edge_attr_inner)

        # construct X
        # if (is_high):
        #     X = torch.cat([torch.zeros(n_l, l * 2), X, torch.zeros(n_l, (depth - 1 - l) * 2)], dim=1)
        # else:
        #     X_l = torch.tensor(l, dtype=torch.float).repeat(n_l, 1)
        #     X = torch.cat([X, X_l], dim=1)
        X_global.append(X)

        # construct edges
        index1 = torch.tensor(range(n_l), dtype=torch.long)
        index1 = index1 + num_nodes
        num_nodes += n_l

        # #construct inter-graph edge
        if l != depth-1:
            index2 = np.array(range(n_l//4)).reshape(h_x_l//2, h_y_l//2)  # torch.repeat is different from numpy
            index2 = index2.repeat(2, axis = 0).repeat(2, axis = 1)
            index2 = torch.tensor(index2).reshape(-1)
            index2 = index2 + num_nodes
            index2 = torch.tensor(index2, dtype=torch.long)

            edge_index_inter1 = torch.cat([index1,index2], dim=-1).reshape(2,-1)
            edge_index_inter2 = torch.cat([index2,index1], dim=-1).reshape(2,-1)
            edge_index_inter = torch.cat([edge_index_inter1, edge_index_inter2], dim=1)

            edge_attr_inter1 = torch.tensor((0, 0, 1), dtype=torch.float).repeat(n_l, 1)
            edge_attr_inter2 = torch.tensor((0, 0,-1), dtype=torch.float).repeat(n_l, 1)
            edge_attr_inter = torch.cat([edge_attr_inter1, edge_attr_inter2], dim=0)

            edge_index_global.append(edge_index_inter)
            edge_attr_global.append(edge_attr_inter)



    X = torch.cat(X_global, dim=0)
    edge_index = torch.cat(edge_index_global, dim=1)
    edge_attr = torch.cat(edge_attr_global, dim=0)
    mask_index = torch.tensor(range(n_x * n_y), dtype=torch.long)
    # print('create multi_grid with size:', X.shape,  edge_index.shape, edge_attr.shape, mask_index.shape)

    return (X, edge_index, edge_attr, mask_index, num_nodes)
