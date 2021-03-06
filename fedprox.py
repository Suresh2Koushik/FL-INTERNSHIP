# -*- coding: utf-8 -*-
"""FEDPROX.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1sYMGh4Vd6S9E0XfUdLDNwi93tuX9u__H
"""

from google.colab import drive
drive.mount('/content/drive')

import sys 
sys.path.append("/content/drive/MyDrive/Colab Notebooks")

!pip install torch==1.4

!pip install syft==0.2.9

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torchvision import datasets, transforms
from torch.utils.data import DataLoader, Dataset
import syft as sy
import copy
import numpy as np

import importlib
importlib.import_module('FLDataset')
from FLDataset import load_dataset, getActualImgs
from utils import averageModels, averageGradients

class Arguments():
    def __init__(self):
        self.images = 60000
        self.clients = 4
        self.rounds = 4
        self.epochs = 4
        self.local_batches = 1
        self.lr = 0.01
        self.C = 0.9
        self.drop_rate = 0.1
        self.mu = 0.1
        self.torch_seed = 0
        self.log_interval = 10
        self.iid = 'iid'
        self.split_size = int(self.images / self.clients)
        self.samples = self.split_size / self.images 
        self.use_cuda = False
        self.save_model = False

args = Arguments()

use_cuda = args.use_cuda and torch.cuda.is_available()
torch.manual_seed(1)
device = torch.device("cuda" if use_cuda else "cpu")
kwargs = {'num_workers': 1, 'pin_memory': True} if use_cuda else {}

hook = sy.TorchHook(torch)
clients = []

for i in range(args.clients):
    clients.append({'hook': sy.VirtualWorker(hook, id="client{}".format(i+1))})

# Download MNIST manually using 'wget' then uncompress the file
!wget www.di.ens.fr/~lelarge/MNIST.tar.gz
!tar -zxvf MNIST.tar.gz

globa_train, global_test, train_group, test_group = load_dataset(args.clients, args.iid)

for inx, client in enumerate(clients):
    trainset_ind_list = list(train_group[inx])
    client['trainset'] = getActualImgs(globa_train, trainset_ind_list, args.local_batches)
    client['testset'] = getActualImgs(global_test, list(test_group[inx]), args.local_batches)
    client['samples'] = len(trainset_ind_list) / args.images

transform = transforms.Compose([transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))])
global_test_dataset = datasets.MNIST('./data/', train=False, download=True, transform=transform)
global_test_loader = DataLoader(global_test_dataset, batch_size=args.local_batches, shuffle=True)

class Net(nn.Module):
    def __init__(self):
        super(Net, self).__init__()
        self.conv1 = nn.Conv2d(1, 20, 5, 1)
        self.conv2 = nn.Conv2d(20, 50, 5, 1)
        self.fc1 = nn.Linear(4*4*50, 500)
        self.fc2 = nn.Linear(500, 10)

    def forward(self, x):
        x = F.relu(self.conv1(x))
        x = F.max_pool2d(x, 2, 2)
        x = F.relu(self.conv2(x))
        x = F.max_pool2d(x, 2, 2)
        x = x.view(-1, 4*4*50)
        x = F.relu(self.fc1(x))
        x = self.fc2(x)
        return F.log_softmax(x, dim=1)

def ClientUpdate(args, device, client, global_model, rclients=False):
    client['model'].train()
    Epochs = args.epochs + 1
    if rclients:
        Epochs = np.random.randint(low=1, high=Epochs)
        Epochs = 2

    for epoch in range(1, Epochs):
        for batch_idx, (data, target) in enumerate(client['trainset']):
            data = data.send(client['hook'])
            target = target.send(client['hook'])
            client['model'].send(data.location)
            
            data, target = data.to(device), target.to(device)
            client['optim'].zero_grad()
            output = client['model'](data)
            loss = F.nll_loss(output, target)
            loss.backward()
            client['optim'].step(global_model.send(client['hook']))
            client['model'].get() 
            global_model.get()

            if batch_idx % args.log_interval == 0:
                loss = loss.get() 
                print('Model {} Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
                    client['hook'].id,
                    epoch, batch_idx * args.local_batches, len(client['trainset']) * args.local_batches, 
                    100. * batch_idx / len(client['trainset']), loss.item()))

def test(args, model, device, test_loader, name):
    model.eval()   
    test_loss = 0
    correct = 0
    with torch.no_grad():
        for data, target in test_loader:
            data, target = data.to(device), target.to(device)
            output = model(data)
            test_loss += F.nll_loss(output, target, reduction='sum').item() # sum up batch loss
            pred = output.argmax(1, keepdim=True) # get the index of the max log-probability 
            correct += pred.eq(target.view_as(pred)).sum().item()

    test_loss /= len(test_loader.dataset)
    print('\nTest set: Average loss for {} model: {:.4f}, Accuracy: {}/{} ({:.0f}%)\n'.format(
        name, test_loss, correct, len(test_loader.dataset),
        100. * correct / len(test_loader.dataset)))

class FedProxOptim(optim.Optimizer):
    def __init__(self, params, lr=args.lr, mu=args.mu):
        defaults = dict(lr=lr, mu=mu)
        super(FedProxOptim, self).__init__(params, defaults)
    
    def step(self, global_model=None, closure = None):
        loss = None
        if closure is not None:
            loss = closure()
        for group in self.param_groups:
            lr, mu = group['lr'], group['mu']
            for p in zip(group['params'], list(global_model.parameters())):
                if p[0].grad is None:
                    continue
                d_p = p[0].grad.data # local model grads
                #formula weight of n
                p[0].data.sub_(group['lr'], (d_p + mu * (p[0].data.clone() - p[1].data.clone())))
                
        return loss

torch.manual_seed(args.torch_seed)
#creating the global  model
global_model = Net().to(device)

#iterating through the clients
for client in clients:
    torch.manual_seed(args.torch_seed)
    client['model'] = Net().to(device)
    #creating the optimizer for clients
    client['optim'] = FedProxOptim(client['model'].parameters(), lr=args.lr, mu=args.mu)

for fed_round in range(args.rounds):
    
#     uncomment if you want a randome fraction for C every round
#     args.C = float(format(np.random.random(), '.1f'))
    
    # m-number of devices will be included in this round
    #C-random function
    m = int(max(args.C * args.clients, 1))
    
    # Selected devices
    np.random.seed(fed_round)
    selected_clients_inds = np.random.choice(range(len(clients)), m, replace=False)
    selected_clients = [clients[i] for i in selected_clients_inds]
    
    # Active devices
    #1-drop rate * M- num of devices included in the round
    active_clients_inds = np.random.choice(selected_clients_inds, int((1-args.drop_rate) * m), replace=False)
    active_clients = [clients[i] for i in active_clients_inds]
    
    # The rest of the active devices (selected but dropped)
    #serdiff1d fun is used to return the selected decive
    rest_clients_inds = np.setdiff1d(selected_clients_inds, active_clients_inds)
    rest_clients = [clients[i] for i in rest_clients_inds]
    

    # Training the active devices
    for client in active_clients:
        ClientUpdate(args, device, client, global_model)
    

    # Training the rest with less number of epochs
    for client in rest_clients:
        ClientUpdate(args, device, client, global_model, True)


    global_model = averageModels(global_model, selected_clients)
    
    test(args, global_model, device, global_test_loader, 'Global')
    
    for client in clients:
        client['model'].load_state_dict(global_model.state_dict())

if (args.save_model):
    torch.save(global_model.state_dict(), "FedProx.pt")