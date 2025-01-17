from pydantic import json
from torch_geometric.data import Batch, Data
from torch.utils.data import Dataset, DataLoader
import pandas as pd
from sklearn.model_selection import StratifiedShuffleSplit
import pickle
from data_pre import CustomData
import torch
import pickle

import torch.utils.data
import time
import os
import numpy as np

import csv

import dgl

def read_pickle(filename):
    with open(filename, 'rb') as f:
        obj = pickle.load(f)
    return obj

class DrugDataset(Dataset):
    def __init__(self, data_df, drug_graph, drug_graph_dgl):
        self.data_df = data_df
        self.drug_graph = drug_graph
        self.drug_graph_dgl = drug_graph_dgl

    def __len__(self):
        return len(self.data_df)

    def __getitem__(self, index):
        return self.data_df.iloc[index]

    def collate_fn(self, batch):
        head_list = []
        head_list_dgl = []
        tail_list = []
        tail_list_dgl = []
        label_list = []
        rel_list = []
        head_finger = []
        tail_finger = []
        for row in batch:
            Drug1_ID, Drug2_ID, Y, Neg_samples,p_h_finger,p_t_finger,n_finger = (
                row['Drug1_ID'], row['Drug2_ID'], row['Y'], row['Neg samples'],row['p_finger1'],row['p_finger2'],row['Neg_finger'])
            Neg_ID, Ntype = Neg_samples.split('$')
            #Neg_ID = Neg_samples
            #Ntype = 'h'
            h_graph = self.drug_graph.get(Drug1_ID)
            t_graph = self.drug_graph.get(Drug2_ID)
            n_graph = self.drug_graph.get(Neg_ID)
            h_graph_dgl = self.drug_graph_dgl.get(Drug1_ID)
            t_graph_dgl = self.drug_graph_dgl.get(Drug2_ID)
            n_graph_dgl = self.drug_graph_dgl.get(Neg_ID)

            pos_pair_h = h_graph
            pos_pair_t = t_graph
            pos_pair_h_dgl = h_graph_dgl
            pos_pair_t_dgl = t_graph_dgl

            if Ntype == 'h':
                n_h_finger = n_finger
                n_t_finger = p_t_finger
                neg_pair_h = n_graph
                neg_pair_t = t_graph
                neg_pair_h_dgl = n_graph_dgl
                neg_pair_t_dgl = t_graph_dgl
            else:
                n_h_finger = p_h_finger
                n_t_finger = n_finger
                neg_pair_h = h_graph
                neg_pair_t = n_graph
                neg_pair_h_dgl = h_graph_dgl
                neg_pair_t_dgl = n_graph_dgl

            head_list.append(pos_pair_h)
            head_list.append(neg_pair_h)
            tail_list.append(pos_pair_t)
            tail_list.append(neg_pair_t)

            head_list_dgl.append(pos_pair_h_dgl)
            head_list_dgl.append(neg_pair_h_dgl)
            tail_list_dgl.append(pos_pair_t_dgl)
            tail_list_dgl.append(neg_pair_t_dgl)

            head_finger.append(p_h_finger)
            head_finger.append(n_h_finger)
            tail_finger.append(p_t_finger)
            tail_finger.append(n_t_finger)

            rel_list.append(torch.LongTensor([Y]))
            rel_list.append(torch.LongTensor([Y]))

            label_list.append(torch.FloatTensor([1]))
            label_list.append(torch.FloatTensor([0]))

        head_pairs = Batch.from_data_list(head_list, follow_batch=['edge_index'])
        tail_pairs = Batch.from_data_list(tail_list, follow_batch=['edge_index'])
        head_pairs_dgl = dgl.batch(head_list_dgl)
        tail_pairs_dgl = dgl.batch(tail_list_dgl)
        rel = torch.cat(rel_list, dim=0)
        label = torch.cat(label_list, dim=0)

        head_finger = [eval(item)[0] for item in head_finger]
        tail_finger = [eval(item)[0] for item in tail_finger]

        head_finger = torch.tensor(head_finger, dtype=torch.float32)
        tail_finger = torch.tensor(tail_finger, dtype=torch.float32)
        return head_pairs, tail_pairs, head_pairs_dgl, tail_pairs_dgl, head_finger, tail_finger, rel, label


class DrugDataLoader(DataLoader):
    def __init__(self, data, **kwargs):
        super().__init__(data, collate_fn=data.collate_fn, **kwargs)


def split_train_valid(data_df, fold, val_ratio=0.2):
        cv_split = StratifiedShuffleSplit(n_splits=2, test_size=val_ratio, random_state=fold)
        train_index, val_index = next(iter(cv_split.split(X=range(len(data_df)), y = data_df['Y'])))

        train_df = data_df.iloc[train_index]
        val_df = data_df.iloc[val_index]

        return train_df, val_df

def load_ddi_dataset(root, batch_size, fold=0,cold_or_hot='hot',nn_or_no='nn'):
    ###返回每个原子特征x（33，70）、边对edge_index（2，74）,有边相连矩阵中坐标line_graph_edge_index（2，104）、边特征edge_attr=[74,6]、相似矩阵（1，544）,id='DB09053'
    if cold_or_hot == 'hot':
        drug_graph = read_pickle(os.path.join(root, 'drug_data_pyg.pkl'))#以字典的形式呈现{'ID'：特征}
        drug_graph_dgl = read_pickle(os.path.join(root, 'drug_data_dgl.pkl'))

        train_df = pd.read_csv(os.path.join(root, f'pair_pos_neg_triplets_train_fold{fold}.csv'))
        test_df = pd.read_csv(os.path.join(root, f'pair_pos_neg_triplets_test_fold{fold}.csv'))
        train_df, val_df = split_train_valid(train_df, fold=fold)
        val_set = DrugDataset(val_df, drug_graph, drug_graph_dgl)
        train_set = DrugDataset(train_df, drug_graph, drug_graph_dgl)
        test_set = DrugDataset(test_df, drug_graph, drug_graph_dgl)

        train_loader = DrugDataLoader(train_set, batch_size=batch_size, shuffle=False, num_workers=8, drop_last=True)
        val_loader = DrugDataLoader(val_set, batch_size=batch_size, shuffle=False, num_workers=8)
        test_loader = DrugDataLoader(test_set, batch_size=batch_size, shuffle=False, num_workers=8)
        # print("Number of samples in the train set: ", len(train_set))
        print("Number of samples in the validation set: ", len(val_set))
        print("Number of samples in the test set: ", len(test_set))

        return train_loader, val_loader, test_loader
    else:
        drug_graph = read_pickle(os.path.join(root, 'drug_data_coldstart_pyg.pkl'))  # 以字典的形式呈现{'ID'：特征}
        drug_graph_dgl = read_pickle(os.path.join(root, 'drug_data_coldstart_dgl.pkl'))
        train_df = pd.read_csv(os.path.join(root, f'pair_pos_neg_triples-fold{fold}-train.csv'))
        if nn_or_no == 'nn':
        #new-new
            val_df = pd.read_csv(os.path.join(root, f'pair_pos_neg_triples-fold{fold}-s1.csv'))
        #new-old
        else:
            val_df = pd.read_csv(os.path.join(root, f'pair_pos_neg_triples-fold{fold}-s2.csv'))
        train_set = DrugDataset(train_df, drug_graph, drug_graph_dgl)
        val_set = DrugDataset(val_df, drug_graph, drug_graph_dgl)

        train_loader = DrugDataLoader(train_set, batch_size=batch_size, shuffle=True, num_workers=8, drop_last=True)
        val_loader = DrugDataLoader(val_set, batch_size=batch_size, shuffle=False, num_workers=8)


        print("Number of samples in the train set: ", len(train_set))
        print("Number of samples in the validation set: ", len(val_set))
        # print("Number of samples in the test set: ", len(test_set))

        # return train_loader, val_loader, test_loader
        return train_loader, val_loader
    # csv_data = pd.concat([train_df,test_df,val_df],axis=0)
    # csv_data.to_csv('merged_data.csv', index=False)
    # val1 = list(val_df["Drug1_ID"])
    # train1 = list(train_df["Drug1_ID"])
    # test1 = list(test_df["Drug1_ID"])
    # comm1 = train1.intersection(test1)
    # comm2 = train1.intersection(val1)
    # print(comm1)
    # print(comm2)






